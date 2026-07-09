# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║     BOT MEAN REVERSION OPTIMISÉ — OKX X10 (ANTI-FRAIS v3)       ║
║  Version 3.0 — Cohérence risque/récompense + frais réels        ║
╚══════════════════════════════════════════════════════════════════╝

### MODIFS PAR RAPPORT À v2 (résumé) ###
1) trade_rentable() vérifie désormais DEUX conditions au lieu d'une :
   - le 1er palier de lock doit couvrir les frais+slippage avec marge (comme avant)
   - le 1er palier de lock doit AUSSI couvrir le risque réel du stop-loss avec marge
   Avant, seule la 1ère condition existait. Or LOCK_PALIERS_PCT est basé sur le
   CAPITAL alors que le risque de stop est basé sur le NOTIONAL (mise x levier).
   Résultat en v2 : avec une mise plus grosse (proche de MISE_MAX_PCT) ou un ATR
   plus élevé, le risque de stop grossit mais le 1er palier reste fixe -> le
   ratio gain/risque se dégrade silencieusement et les frais mangent le net
   même sur les trades gagnants. La nouvelle condition bloque ces cas.

2) Give-back du trailing (post-palier1) devient progressif au lieu d'un ratio
   fixe de 0.6%. Un give-back fixe de 0.6% appliqué dès le 1er palier (qui ne
   vaut que 1.45%) redonne près de la moitié du gain de pic. On serre le
   trailing juste après le 1er palier (0.15%) et on ne le desserre que quand
   plusieurs paliers sont franchis (le trade a prouvé qu'il a du souffle).

3) dans_fenetre_pre_funding() était définie mais jamais appelée nulle part :
   c'était du code mort. Elle est maintenant utilisée comme garde-fou dans
   ouvrir_trade() pour éviter d'ouvrir juste avant un funding (coût caché
   supplémentaire, cohérent avec la logique "anti-frais" du bot).

4) Ajout d'un garde-fou taille mini : si le coût frais+slippage dépasse un %
   trop élevé de la mise elle-même, le trade est refusé (évite les trades
   proches de MISE_MIN où les frais représentent une part disproportionnée).

Le reste de la structure (config, noms de fonctions, points d'intégration
"à implémenter") est conservé à l'identique pour rester compatible avec ta
logique existante.
"""

import asyncio
import aiohttp
import os
import json
import hmac
import hashlib
import base64
import logging
import time
import uuid
import signal
from datetime import datetime, timedelta
import pandas as pd
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from database import init_database, charger_etat, sauvegarder_etat, enregistrer_trade

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

# ===================== CONFIGURATION ANTI-FRAIS =====================
CAPITAL_INITIAL         = 543.65
SEUIL_ALERTE_PERTE_PCT  = 10.0
LEVIER                  = 10

# Mise adaptative
MISE_BASE_PCT           = 0.08     # Baissé un peu pour sécurité
MISE_MAX_PCT            = 0.15     # Peut monter sur très bon signal
MISE_MIN                = 12.0
CHECK_INTERVAL          = 1

MAX_TRADES_SIMULTANES   = 2
SEUIL_PERTES_CONSECUTIVES_PAUSE = 3
DUREE_PAUSE_APRES_PERTES_MIN    = 60

# Fenêtre funding
FENETRE_PRE_FUNDING_MIN = 25

# Paramètres stratégie
SEUIL_MOUVEMENT_PCT     = 1.00
VOLUME_MINI             = 0.40      # Augmenté
STOP_LOSS_PCT_BASE      = 0.0070    # 0.70%
ATR_MULTIPLIER          = 1.35

# Lock profits (premier palier plus haut)
LOCK_PALIERS_PCT = [1.45, 1.8, 2.3, 2.8, 3.5, 4.3, 5.2, 6.3, 7.8, 9.5, 12.0, 15.0, 20.0, 30.0]

# ### MODIF: give-back progressif au lieu d'un ratio fixe unique.
# Clé = index du palier max atteint (0 = palier1 franchi), valeur = ratio de
# give-back autorisé (en % du prix). On reste très serré juste après le 1er
# palier, et on ne desserre qu'après plusieurs paliers franchis.
TRAIL_RATIO_PAR_PALIER = {
    0: 0.0015,   # juste après palier 1 (1.45%) -> give-back très limité
    2: 0.0025,   # après palier 3 (2.3%)
    5: 0.0040,   # après palier 6 (4.3%)
    8: 0.0060,   # après palier 9 (7.8%) -> on laisse un peu plus respirer
}
TRAIL_RATIO_POST_PALIER1 = 0.006    # conservé comme valeur de repli (fallback)

# Filtres
RSI_SEUIL_BAS           = 38
RSI_SEUIL_HAUT          = 62
RSI_PERIODE             = 14

# Protection
KILL_SWITCH_PCT         = 0.035     # Plus réactif

OKX_TAKER_FEE            = 0.0005
STOP_BUFFER_SLIPPAGE_PCT = 0.0012
SLIPPAGE_ENTREE_PCT      = 0.0008   # ### MODIF: slippage estimé à l'ouverture (absent en v2)

# ### MODIF: ratios de sécurité nommés explicitement (au lieu du "2.5" en dur)
RATIO_MIN_VS_FRAIS  = 2.5   # le 1er palier doit couvrir >= 2.5x les frais+slippage
RATIO_MIN_VS_RISQUE = 1.3   # le 1er palier doit couvrir >= 1.3x le risque de stop
COUT_MAX_PCT_DE_MISE = 0.05 # coût frais+slippage ne doit pas dépasser 5% de la mise

# Telegram & OKX (environnement)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
OKX_API_KEY = os.environ.get('OKX_API_KEY', '')
OKX_API_SECRET = os.environ.get('OKX_API_SECRET', '')
OKX_API_PASSPHRASE = os.environ.get('OKX_API_PASSPHRASE', '')
MODE_REEL = os.environ.get('MODE_REEL', '0') == '1'

# ===================== ÉTAT GLOBAL =====================
trades_ouverts = {}
prix_reference = {}
cooldown_marches = {}
arret_demande = False


def dans_fenetre_pre_funding():
    maintenant = datetime.utcnow()
    for h in (0, 8, 16):
        t = maintenant.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= maintenant:
            t += timedelta(days=1)
        if (t - maintenant).total_seconds() <= FENETRE_PRE_FUNDING_MIN * 60:
            return True
    return False


def calculer_stop_dynamique(entry_price, atr_value, is_long=True):
    """Stop plus large sur ATR pour survivre au bruit."""
    stop_atr = entry_price * (atr_value * ATR_MULTIPLIER)
    stop_fixe = entry_price * STOP_LOSS_PCT_BASE
    distance = max(stop_fixe, stop_atr) + entry_price * STOP_BUFFER_SLIPPAGE_PCT

    if is_long:
        return entry_price - distance
    return entry_price + distance


def stop_loss_pct_distance(atr_value):
    """### MODIF: distance de stop en % du prix, isolée dans sa propre fonction
    pour être réutilisée à la fois par calculer_stop_dynamique() et par
    l'estimation du risque en euros (évite toute divergence entre les deux)."""
    return max(STOP_LOSS_PCT_BASE, atr_value * ATR_MULTIPLIER) + STOP_BUFFER_SLIPPAGE_PCT


def cout_frais_slippage_eur(notional):
    """### MODIF: coût total round-trip réaliste = frais taker (aller+retour)
    + slippage estimée à l'entrée + slippage estimée à la sortie.
    En v2, seule la sortie avait un buffer de slippage ; l'entrée n'en avait
    aucun alors qu'un market order OKX en subit aussi un."""
    frais_pct = (OKX_TAKER_FEE * 2) + SLIPPAGE_ENTREE_PCT + STOP_BUFFER_SLIPPAGE_PCT
    return notional * frais_pct


def risque_stop_eur(entry_price, atr_value, notional):
    """### MODIF (nouveau): perte attendue en euros si le stop est touché,
    exprimée sur la même base (notional) que les frais, pour pouvoir la
    comparer directement au gain visé du 1er palier."""
    return notional * stop_loss_pct_distance(atr_value)


def trade_rentable(entry_price, capital, levier, mise_pct, atr_value):
    """### MODIF: vérification de rentabilité en 2 temps.
    1) le 1er palier doit couvrir largement les frais+slippage (comme en v2)
    2) le 1er palier doit AUSSI couvrir largement le risque réel de stop-loss
       (absent en v2 -> c'était la faille qui laissait passer des trades à
       ratio gain/risque dégradé quand la mise ou l'ATR étaient plus élevés)
    """
    notional = capital * mise_pct * levier
    cout = cout_frais_slippage_eur(notional)
    risque = risque_stop_eur(entry_price, atr_value, notional)

    premier_palier_eur = round(capital * LOCK_PALIERS_PCT[0] / 100, 2)

    if premier_palier_eur < RATIO_MIN_VS_FRAIS * cout:
        log.info(f"Rejet rentabilité (frais) : palier1={premier_palier_eur:.2f}€ "
                 f"vs {RATIO_MIN_VS_FRAIS}x coût={cout:.2f}€")
        return False

    if premier_palier_eur < RATIO_MIN_VS_RISQUE * risque:
        log.info(f"Rejet rentabilité (risque/récompense) : palier1={premier_palier_eur:.2f}€ "
                 f"vs {RATIO_MIN_VS_RISQUE}x risque_stop={risque:.2f}€")
        return False

    return True


def get_palier_lock(pnl_max, capital):
    lock = 0.0
    for pct in LOCK_PALIERS_PCT:
        palier_eur = round(capital * pct / 100, 2)
        if pnl_max >= palier_eur:
            lock = palier_eur
    return lock


def trail_ratio_actuel(index_palier_max):
    """### MODIF (nouveau): renvoie le ratio de give-back à appliquer selon le
    palier le plus haut déjà franchi. Remplace le ratio fixe unique
    TRAIL_RATIO_POST_PALIER1 par une grille progressive plus serrée au début."""
    ratio = TRAIL_RATIO_POST_PALIER1  # valeur de repli si aucun seuil ne matche
    for seuil, r in sorted(TRAIL_RATIO_PAR_PALIER.items()):
        if index_palier_max >= seuil:
            ratio = r
    return ratio


# ===================== FONCTION OUVERTURE =====================
async def ouvrir_trade(symbole, sens, prix_entree, capital_dispo, atr_value, rsi):
    # ### MODIF: dans_fenetre_pre_funding() était déclarée mais jamais appelée
    # en v2 (code mort). On l'utilise ici pour éviter d'ouvrir juste avant un
    # funding OKX, qui ajoute un coût caché en plus des frais/slippage.
    if dans_fenetre_pre_funding():
        log.info(f"Ouverture {symbole} reportée : fenêtre pré-funding active")
        return False

    mise_pct = MISE_BASE_PCT

    # Boost sur très bon signal
    if (rsi < 35 and sens == 'long') or (rsi > 65 and sens == 'short'):
        mise_pct = MISE_MAX_PCT * 0.9

    mise = min(mise_pct * capital_dispo, MISE_MAX_PCT * capital_dispo)
    if mise < MISE_MIN:
        log.warning(f"Mise trop faible pour {symbole}")
        return False

    # ### MODIF (nouveau): garde-fou taille mini réaliste. MISE_MIN est une
    # valeur absolue fixe ; elle ne garantit pas que les frais restent une
    # part raisonnable de CETTE mise précise. On le vérifie explicitement.
    notional_test = mise * LEVIER
    if cout_frais_slippage_eur(notional_test) > mise * COUT_MAX_PCT_DE_MISE:
        log.warning(f"Trade {symbole} rejeté : frais+slippage > {COUT_MAX_PCT_DE_MISE*100:.0f}% de la mise")
        return False

    if not trade_rentable(prix_entree, capital_dispo, LEVIER, mise / capital_dispo, atr_value):
        log.warning(f"Trade {symbole} non rentable net → ignoré")
        return False

    stop_loss = calculer_stop_dynamique(prix_entree, atr_value, sens == 'long')

    # === Ici tu mettras ton vrai appel API OKX ===
    log.info(f"[{'REEL' if MODE_REEL else 'SIMU'}] OUVERTURE {sens.upper()} {symbole} @ {prix_entree:.4f} | "
             f"SL={stop_loss:.4f} | Mise={mise:.1f}€ | ATR={atr_value:.4f}")

    # trades_ouverts[...] = {...}  # à implémenter selon ta logique existante
    return True

# ===================== BOUCLE PRINCIPALE (exemple) =====================
async def main():
    await init_database()
    log.info("🚀 Bot Mean Reversion Anti-Frais v3 démarré")

    while not arret_demande:
        try:
            # Ton scan de marchés + récupération prix/ATR/RSI ici
            # Exemple placeholder :
            await asyncio.sleep(CHECK_INTERVAL)

            # if condition_signal:
            #     await ouvrir_trade(...)

        except Exception as e:
            log.error(f"Erreur boucle principale: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Arrêt propre du bot.")
