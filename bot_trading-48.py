# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════╗
║         BOT MEAN REVERSION — OKX X10                             ║
║  Mean Reversion 0.50% | Surveillance prix temps réel            ║
║  Lock Profits Paliers | Marchés x10 uniquement | 24h/24         ║
║  Capital 500€ | Architecture async aiohttp                      ║
╚══════════════════════════════════════════════════════════════════╝
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
from datetime import datetime, timedelta
import pandas as pd
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from database import init_database, charger_etat, sauvegarder_etat, enregistrer_trade

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════
CAPITAL_INITIAL         = 500.0
LEVIER                  = 10

# ── Mise calculée pour avoir des frais totaux de ~0.50€ avec les vrais frais OKX
# Frais réels OKX (taker, palier standard) = 0.05% ouverture + 0.05% fermeture = 0.10% total
# Pour frais = 0.50€ → position = 0.50 / 0.0010 = 500€ → mise = 500 / 10 = 50€
# Soit 50 / 500 = 10% du capital
MISE_BASE_PCT           = 0.10    # 10% du capital → mise ~50€ → position ~500€ → frais ~0.50€
MISE_MIN                = 10.0    # mise minimum cohérente avec l'objectif frais 0.50€
MISE_MAX_PCT            = 0.12    # plafond légèrement au-dessus pour le boost confiance
CHECK_INTERVAL          = 3          # secondes entre chaque check prix
PAUSE_SCAN              = 30         # secondes entre chaque scan de nouveaux marchés
MAX_TRADES_SIMULTANES   = 10         # 10 marchés max = 1 par marché

# ── Détection signal mean reversion — surveillance temps réel
SEUIL_MOUVEMENT_PCT     = 0.50   # dès que le prix bouge de 0.50% → signal
VOLUME_MINI             = 0.25   # volume min vs moyenne 24h
STOP_LOSS_PCT           = 0.003  # stop = 0.3% du prix d'entrée (≈ -2€ au capital/mise actuels) — évolue avec la taille de position, contrairement à un stop fixe en €
DUREE_MAX_MINUTES       = 360    # 6h — fermeture forcée si ni stop ni lock atteint avant

# ── Filtre RSI 1h
RSI_SEUIL_BAS           = 45     # RSI < 45 → marché baissier → inverser ACHAT en VENTE
RSI_SEUIL_HAUT          = 55     # RSI > 55 → marché haussier → inverser VENTE en ACHAT
RSI_PERIODE             = 14

# ── Protections
KILL_SWITCH_JOUR        = -100.0
SEUIL_RUINE             = 300.0

# ── Lock profits par paliers proportionnels au capital
# Premier palier : 0.15% = 0.75€ à 500€
LOCK_PALIERS_PCT = [
    0.17, 0.22, 0.30, 0.40, 0.50, 0.65, 0.80, 1.00, 1.20, 1.50,
    1.80, 2.20, 2.60, 3.20, 3.80, 4.60, 5.50, 6.50, 7.50, 9.00,
    10.00, 12.50, 15.00, 17.50, 20.00, 25.00, 30.00, 45.00, 60.00,
]

def get_palier_lock(pnl_max, capital):
    """Retourne le gain garanti selon le PnL max atteint — proportionnel au capital."""
    lock = 0.0
    for pct in LOCK_PALIERS_PCT:
        palier_eur = round(capital * pct / 100, 2)
        if pnl_max >= palier_eur:
            lock = palier_eur
    return lock

# ── Gestion mise dynamique
WINS_CONFIANCE          = 3
BOOST_CONFIANCE         = 1.20

# ── Frais OKX réels (X-Perps, palier standard/non-VIP — identiques aux Swaps Perpétuels classiques)
# Maker 0.02% / Taker 0.05% du notionnel — le bot sort au marché à l'ouverture
# ET à la fermeture, donc taker des deux côtés. Pas de rollover/funding modélisé
# ici (contrairement aux frais Kraken margin, OKX ne facture pas de frais de
# financement séparé de cette façon sur ce produit dans cette version du bot).
OKX_TAKER_FEE            = 0.0005  # 0.05% par exécution (ouverture OU fermeture)

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
OKX_API_KEY      = os.environ.get('OKX_API_KEY', '')
OKX_API_SECRET   = os.environ.get('OKX_API_SECRET', '')
OKX_API_PASSPHRASE = os.environ.get('OKX_API_PASSPHRASE', '')
MODE_REEL        = os.environ.get('MODE_REEL', '0') == '1'

# ── Sécurité à DEUX niveaux avant de toucher de l'argent réel :
#    1) MODE_REEL doit être à 1 pour envoyer le moindre ordre (sinon simulation pure)
#    2) OKX_COMPTE_DEMO reste à 1 PAR DÉFAUT — même avec MODE_REEL=1, les ordres
#       partent avec l'en-tête x-simulated-trading:1, qui les cantonne à l'argent
#       fictif du compte Démo Trading OKX. Il faut DEUX actions volontaires et
#       explicites sur Railway (MODE_REEL=1 ET OKX_COMPTE_DEMO=0) pour qu'un ordre
#       touche enfin de l'argent réel — jamais un seul interrupteur suffit.
OKX_COMPTE_DEMO  = os.environ.get('OKX_COMPTE_DEMO', '1') == '1'

# ── Reset complet piloté depuis Railway (Variables → RESET_TOUT = 1, puis Deploy)
#    Remet à zéro : capital, PnL jour, kill switch, compteurs, historique.
#    Remettre à 0 (ou supprimer la variable) + redeploy pour revenir en
#    fonctionnement normal sans redéclencher un reset à chaque redémarrage.
RESET_TOUT = os.environ.get('RESET_TOUT', '0').strip().lower() in ('1', 'true', 'oui', 'yes')

# ── Marchés — uniquement ceux à levier x10 sur OKX (X-Perps, compte France/EEA)
# Chargés dynamiquement via API au démarrage et mis à jour chaque nuit à minuit
MARCHES          = []   # liste des symboles actifs (levier x10 uniquement)
OKX_SYMBOLS      = {}   # { "BTCUSD": "BTC-USD-YYMMDD", ... } — instId PUBLIC (www.okx.com), utilisé pour les prix (WebSocket + REST) — NE PAS écraser avec l'instId du compte
OKX_SYMBOLS_EXEC = {}   # { "BTCUSD": "BTC-USD-YYMMDD", ... } — instId scopé au COMPTE (démo ou réel), utilisé uniquement pour passer/fermer un ordre
OKX_CT_VAL       = {}   # { "BTCUSD": 0.01, ... } — valeur d'un contrat (usage réel uniquement)

# ═══════════════════════════════════════════════════════════════
#  ÉTAT GLOBAL
# ═══════════════════════════════════════════════════════════════
trades_ouverts    = {}    # { symbole: True }
prix_reference    = {}    # { symbole: prix_au_moment_du_scan }
cooldown_marches  = {}    # { symbole: timestamp_fin_cooldown }
trades_lock       = None  # initialisé dans boucle_principale()
PRIX_LIVE           = {}    # { symbole: dernier prix reçu via WebSocket OKX }
PRIX_LIVE_TS        = {}    # { symbole: timestamp du dernier tick reçu — pour détecter un cache périmé }
WS_CONNEXION_ACTIVE = None  # référence à la connexion WebSocket en cours (pour forcer une resynchro)
SEUIL_FRAICHEUR_PRIX_SEC = 2.0  # au-delà, le cache WS est considéré périmé → repli REST

log.info("=" * 60)
log.info("  BOT MEAN REVERSION — OKX X10")
log.info(f"  Capital : {CAPITAL_INITIAL}€ | Levier x{LEVIER} (marchés x10 uniquement)")
log.info(f"  Signal : mouvement >= {SEUIL_MOUVEMENT_PCT}% depuis le prix de référence")
log.info(f"  RSI 1h : seuil bas={RSI_SEUIL_BAS} | seuil haut={RSI_SEUIL_HAUT}")
log.info(f"  Stop : {STOP_LOSS_PCT*100:.2f}% du prix d'entrée par trade")
log.info(f"  Frais OKX : {OKX_TAKER_FEE*100:.2f}% ouv + {OKX_TAKER_FEE*100:.2f}% ferm (taker)")
log.info(f"  Kill switch : {KILL_SWITCH_JOUR}€/jour | Ruine : {SEUIL_RUINE}€")
log.info(f"  Durée max par trade : {DUREE_MAX_MINUTES//60}h — fermeture forcée si ni stop ni lock atteint avant")
log.info(f"  Telegram : {'ON' if TELEGRAM_TOKEN else 'OFF'}")
log.info(f"  Mode : {'REEL' if MODE_REEL else 'SIMULATION'}")
if MODE_REEL:
    log.warning(f"  ⚠️ Compte ciblé pour les ordres : {'DÉMO (argent fictif)' if OKX_COMPTE_DEMO else '🚨 RÉEL — ARGENT VÉRITABLE 🚨'}")
log.info("=" * 60)

# ═══════════════════════════════════════════════════════════════
#  CHARGEMENT MARCHÉS x10 DEPUIS API OKX
# ═══════════════════════════════════════════════════════════════
async def charger_marches_x10(session):
    """
    Vérifie, pour une liste de cryptos majeures connues, lesquelles ont
    réellement un X-Perp avec levier x10 disponible sur OKX — le produit
    accessible sur le compte France/EEA (instType=FUTURES, ruleType=xperp),
    plafonné à x10 côté OKX lui-même, contrairement aux Swaps Perpétuels
    classiques (jusqu'à x50-x100) qui ne sont pas ceux réellement tradables
    sur ce compte. Met à jour MARCHES et OKX_SYMBOLS avec le VRAI instId du
    X-Perp (ex: BTC-USD-YYMMDD) — pas un Swap USDT. Supprime les marchés qui
    ne sont plus x10 ou qui n'ont plus de X-Perp.
    """
    global MARCHES, OKX_SYMBOLS, OKX_CT_VAL

    # Cryptos majeures à vérifier — mêmes candidats que la version Kraken
    # d'origine. Tous n'auront pas forcément de X-Perp (l'offre OKX X-Perp
    # est plus restreinte que les Swaps classiques) — seuls ceux qui en ont
    # un réellement à x10 seront gardés.
    CANDIDATS_BASE = [
        "ETH", "XRP", "SOL", "ADA", "LINK", "DOGE", "LTC", "DOT", "TRX", "UNI",
        "HYPE", "AVAX", "ATOM", "NEAR", "AAVE", "ARB", "SUI", "FIL", "BTC",
        "ALGO", "INJ", "OP", "BNB", "HBAR",
    ]

    try:
        async with session.get(
            "https://www.okx.com/api/v5/public/instruments",
            params={"instType": "FUTURES"},
            timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  Erreur API OKX instruments : {data}")
                return
            xperps = {}
            for inst in data.get("data", []):
                if inst.get("ruleType") != "xperp":
                    continue
                base = inst.get("instId", "").split("-")[0]
                if base:
                    xperps[base] = inst

        nouveaux_marches  = []
        nouveaux_symbols  = {}
        nouveaux_ct_val   = {}
        marches_supprimes = []

        for base in CANDIDATS_BASE:
            symbole = f"{base}USD"  # X-Perps cotés en USD, pas en USDT
            inst = xperps.get(base)

            if inst is None:
                if symbole in MARCHES:
                    marches_supprimes.append(symbole)
                log.info(f"  ❌ {symbole} — pas de X-Perp OKX trouvé")
                continue

            try:
                levier_max = float(inst.get("lever", 0) or 0)
            except (TypeError, ValueError):
                levier_max = 0

            if levier_max >= 10:
                nouveaux_marches.append(symbole)
                nouveaux_symbols[symbole] = inst["instId"]
                try:
                    nouveaux_ct_val[symbole] = float(inst.get("ctVal", 0) or 0)
                except (TypeError, ValueError):
                    nouveaux_ct_val[symbole] = 0.0
                log.info(f"  ✅ {symbole} — X-Perp levier max x{levier_max:.0f} → inclus")
                log.info(f"     [DIAG] {symbole} instId={inst.get('instId')} "
                         f"ctVal={inst.get('ctVal')} ctValCcy={inst.get('ctValCcy')} "
                         f"settleCcy={inst.get('settleCcy')} instFamily={inst.get('instFamily')}")
            else:
                if symbole in MARCHES:
                    marches_supprimes.append(symbole)
                log.info(f"  ❌ {symbole} — X-Perp levier max x{levier_max:.0f} → exclu (pas x10)")

        # Fermer les trades ouverts sur les marchés supprimés
        if marches_supprimes:
            log.warning(f"  Marchés supprimés (plus x10) : {marches_supprimes}")
            for m in marches_supprimes:
                prix_reference.pop(m, None)
                cooldown_marches.pop(m, None)

        anciens = set(MARCHES)
        MARCHES     = nouveaux_marches
        OKX_SYMBOLS = nouveaux_symbols
        OKX_CT_VAL  = nouveaux_ct_val
        nouveaux = set(MARCHES)

        ajouts   = nouveaux - anciens
        retraits = anciens - nouveaux

        if ajouts:
            log.info(f"  Nouveaux marchés x10 : {list(ajouts)}")
        if retraits:
            log.info(f"  Marchés retirés : {list(retraits)}")

        log.info(f"  Marchés x10 actifs : {len(MARCHES)} → {MARCHES}")

    except Exception as e:
        log.error(f"  Erreur chargement marchés x10 : {e}")

def get_marches_actifs():
    """Retourne tous les marchés actifs (levier x10 uniquement) — trading 24h/24, 7j/7."""
    return MARCHES

async def filtrer_marches_selon_compte(session):
    """En MODE_REEL uniquement : réduit MARCHES à l'intersection avec ce que
    CE COMPTE peut réellement trader, via l'endpoint authentifié et scopé au
    compte /api/v5/account/instruments — pas le catalogue public générique.

    Diagnostic confirmé : le catalogue public (www.okx.com) liste 15+ marchés
    X-Perp, mais le compte Démo Trading OKX n'en expose réellement qu'un
    sous-ensemble très restreint via l'API (BTC, ETH, DOGE + quelques
    produits TradFi hors périmètre) — d'où des trades ouverts puis
    systématiquement annulés faute d'instId reconnu. Ce filtre évite le
    problème à la source : on ne scanne/trade que ce qui est effectivement
    exécutable pour CE compte (démo ou réel) à cet instant.

    IMPORTANT : on stocke l'instId scopé au compte dans OKX_SYMBOLS_EXEC,
    PAS dans OKX_SYMBOLS — ce dernier reste celui du catalogue public et
    continue d'alimenter le WebSocket + le repli REST pour les prix. Écraser
    OKX_SYMBOLS ici cassait silencieusement la détection de prix (bug réel
    rencontré : plus aucun signal détecté après ce filtrage, faute de prix
    reçu, car le WebSocket restait abonné à l'ancien instId public tandis
    que le code cherchait à faire correspondre le nouvel instId compte)."""
    global MARCHES, OKX_SYMBOLS_EXEC, OKX_CT_VAL

    if not MODE_REEL:
        return  # inutile en simulation pure, aucun ordre n'est jamais envoyé

    path  = "/api/v5/account/instruments"
    query = "?instType=FUTURES"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()

        if data.get("code") != "0":
            log.error(f"  ❌ Erreur filtrage marchés selon compte : {data}")
            return

        inst_par_base = {}
        for inst in data.get("data", []):
            if inst.get("ruleType") != "xperp":
                continue
            base = inst.get("instId", "").split("-")[0]
            if base:
                inst_par_base[base] = inst  # instrument complet, scopé au compte

        avant            = set(MARCHES)
        nouveaux_marches = [m for m in MARCHES if m[:-3] in inst_par_base]
        supprimes        = avant - set(nouveaux_marches)

        MARCHES = nouveaux_marches
        nouveaux_ct_val_exec = {}
        for m in MARCHES:
            inst_exec = inst_par_base[m[:-3]]
            OKX_SYMBOLS_EXEC[m] = inst_exec.get("instId")  # instId d'exécution, scopé au compte — jamais utilisé pour les prix
            ct_val_public = OKX_CT_VAL.get(m)
            try:
                ct_val_exec = float(inst_exec.get("ctVal", 0) or 0)
            except (TypeError, ValueError):
                ct_val_exec = 0.0
            nouveaux_ct_val_exec[m] = ct_val_exec
            ecart = " ⚠️ ÉCART DÉTECTÉ" if ct_val_public and ct_val_exec and ct_val_public != ct_val_exec else ""
            log.info(f"     [DIAG-EXEC] {m} instId={inst_exec.get('instId')} "
                     f"ctVal={inst_exec.get('ctVal')} ctValCcy={inst_exec.get('ctValCcy')} "
                     f"settleCcy={inst_exec.get('settleCcy')} instFamily={inst_exec.get('instFamily')} "
                     f"(vs ctVal catalogue public : {ct_val_public}){ecart}")
        # CORRECTIF : on utilise désormais le ctVal de l'instrument d'EXÉCUTION
        # (celui du compte, effectivement tradé), pas celui du catalogue public
        # — les deux peuvent référencer des contrats différents (échéances
        # distinctes) avec un ctVal différent, comme confirmé sur BTCUSD
        # (0.0001 côté catalogue public vs 1 côté compte, facteur 10000,
        # cause de la sur-taille de position et de l'échec systématique).
        OKX_CT_VAL = nouveaux_ct_val_exec

        compte_label = "DÉMO" if OKX_COMPTE_DEMO else "RÉEL"
        if supprimes:
            log.warning(f"  ⚠️ Marchés retirés (indisponibles pour ce compte {compte_label}) : {sorted(supprimes)}")
        log.info(f"  Marchés réellement tradables pour ce compte ({compte_label}) : {len(MARCHES)} → {MARCHES}")
    except Exception as e:
        log.error(f"  ❌ Exception filtrage marchés selon compte : {e}")

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
#  EXÉCUTION RÉELLE OKX — préparé mais INACTIF tant que MODE_REEL=0
# ═══════════════════════════════════════════════════════════════
#  ⚠️ NON TESTÉ EN CONDITIONS RÉELLES (pas d'accès réseau pour vérifier).
#  Construit selon la documentation officielle OKX v5, mais À VALIDER
#  OBLIGATOIREMENT sur le Demo Trading OKX (domaine wspap.okx.com séparé,
#  argent fictif, données réelles — voir la conversation précédente) avant
#  tout passage avec de l'argent réel. Ne jamais activer MODE_REEL=1 sans
#  être passé par cette étape de validation.
# Domaine pour les appels PRIVÉS (authentifiés) — my.okx.com est le
# sous-domaine spécifique à l'entité EEA/France (voir échange précédent :
# les données publiques restent sur www.okx.com, qui fonctionne déjà bien
# depuis le début, mais les clés API créées sur un compte France/EEA
# n'existent QUE côté my.okx.com — les envoyer vers www.okx.com renvoie
# "API key doesn't exist" (code 50119) même avec une clé 100% correcte).
OKX_BASE_URL = "https://my.okx.com"

def _okx_headers(method, path, body=""):
    """Construit les en-têtes signés requis par l'API privée OKX v5.
    Ajoute x-simulated-trading:1 tant que OKX_COMPTE_DEMO=1 (valeur par
    défaut) — c'est cet en-tête qui garantit que les ordres restent cantonnés
    au compte Démo Trading (argent fictif), indépendamment de MODE_REEL."""
    timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.') + \
                f"{datetime.utcnow().microsecond // 1000:03d}Z"
    message = timestamp + method + path + body
    mac = hmac.new(OKX_API_SECRET.encode(), message.encode(), hashlib.sha256)
    signature = base64.b64encode(mac.digest()).decode()
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json",
    }
    if OKX_COMPTE_DEMO:
        headers["x-simulated-trading"] = "1"
    return headers

async def okx_resoudre_instid_reel(session, base):
    """Récupère l'instId du X-Perp tel que réellement disponible pour CE
    COMPTE, via l'endpoint AUTHENTIFIÉ /api/v5/account/instruments — pas le
    catalogue public générique (/api/v5/public/instruments), qui diffère
    selon la région/le type de compte. Confirmé par la documentation
    officielle OKX elle-même : les utilisateurs sont classés en deux
    catégories selon leur pays, et reçoivent des résultats différents
    (parfois un tableau vide) sur les endpoints d'instruments génériques.
    L'endpoint /account/instruments, lui, est scopé au compte qui appelle —
    donc fiable quelle que soit la région. Retourne None si non trouvé."""
    path = "/api/v5/account/instruments"
    query = "?instType=FUTURES"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.warning(f"  Résolution instId (account/instruments) : réponse invalide {data}")
                return None
            for inst in data.get("data", []):
                if inst.get("ruleType") != "xperp":
                    continue
                if inst.get("instId", "").split("-")[0] == base:
                    log.info(f"  ✅ instId résolu via /account/instruments : {inst.get('instId')}")
                    return inst.get("instId")
            log.warning(f"  Résolution instId (account/instruments) : aucun X-Perp trouvé pour base={base}")
            return None
    except Exception as e:
        log.error(f"  Erreur résolution instId (account/instruments) pour {base} : {e}")
        return None

async def okx_definir_levier(session, inst_id, levier):
    """Configure le levier sur un instrument avant ouverture (marge isolée)."""
    path = "/api/v5/account/set-leverage"
    body = json.dumps({"instId": inst_id, "lever": str(int(levier)), "mgnMode": "isolated"})
    try:
        async with session.post(
            OKX_BASE_URL + path, data=body,
            headers=_okx_headers("POST", path, body),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  ❌ Erreur set-leverage {inst_id} : {data}")
                return False
            return True
    except Exception as e:
        log.error(f"  ❌ Exception set-leverage {inst_id} : {e}")
        return False

async def okx_diag_solde(session, ccy_attendue=None):
    """[DIAGNOSTIC UNIQUEMENT — ne bloque jamais un trade] Lit le solde du
    compte (/api/v5/account/balance) et logge le disponible par devise.
    Sert à comparer ce qui est réellement disponible juste avant un ordre
    BTC vs ETH/DOGE, pour confirmer ou infirmer une carence de marge dans
    une devise précise (ex: USDC) signalée par l'erreur OKX 51008."""
    path  = "/api/v5/account/balance"
    query = f"?ccy={ccy_attendue}" if ccy_attendue else ""
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  [DIAG-SOLDE] Erreur lecture solde : {data}")
                return
            details = data.get("data", [{}])[0].get("details", [])
            if not details:
                log.warning(f"  [DIAG-SOLDE] Aucun détail de devise renvoyé (compte vide ou devise absente) : {data}")
                return
            for d in details:
                log.info(f"  [DIAG-SOLDE] {d.get('ccy')} : dispo={d.get('availBal')} "
                         f"| équité={d.get('eq')} | gelé={d.get('frozenBal')}")
    except Exception as e:
        log.error(f"  [DIAG-SOLDE] Exception lecture solde : {e}")

async def okx_diag_statut_ordre(session, inst_id, ord_id):
    """[DIAGNOSTIC UNIQUEMENT] Interroge /api/v5/trade/order pour connaître
    l'état réel de l'ordre d'ouverture (state: filled/canceled/live/
    partially_filled) et la quantité effectivement remplie (accFillSz).
    But : vérifier si un ordre annoncé 'placé' avec succès (code=0 à la
    soumission) a réellement été REMPLI, ce qui n'est pas garanti par le
    seul code=0 (qui signifie juste 'accepté par le moteur de matching')."""
    path  = "/api/v5/trade/order"
    query = f"?instId={inst_id}&ordId={ord_id}"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0" or not data.get("data"):
                log.error(f"  [DIAG-ORDRE] Erreur lecture statut ordre {ord_id} : {data}")
                return
            o = data["data"][0]
            log.info(f"  [DIAG-ORDRE] {inst_id} ordId={ord_id} state={o.get('state')} "
                     f"sz={o.get('sz')} accFillSz={o.get('accFillSz')} "
                     f"avgPx={o.get('avgPx')} px={o.get('px')}")
    except Exception as e:
        log.error(f"  [DIAG-ORDRE] Exception lecture statut ordre {ord_id} : {e}")

async def okx_diag_position(session, inst_id):
    """[DIAGNOSTIC UNIQUEMENT] Interroge /api/v5/account/positions pour voir
    si OKX considère qu'une position existe réellement sur cet instrument,
    juste avant une tentative de fermeture — but : confirmer si l'erreur
    'Position doesn't exist' (51023) reflète un état déjà connu avant même
    d'appeler close-position, ou une surprise de dernière seconde."""
    path  = "/api/v5/account/positions"
    query = f"?instId={inst_id}"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  [DIAG-POSITION] Erreur lecture position {inst_id} : {data}")
                return
            positions = data.get("data", [])
            if not positions:
                log.warning(f"  [DIAG-POSITION] {inst_id} : AUCUNE position ouverte côté OKX au moment du check")
                return
            for p in positions:
                log.info(f"  [DIAG-POSITION] {inst_id} pos={p.get('pos')} "
                         f"avgPx={p.get('avgPx')} upl={p.get('upl')} "
                         f"mgnMode={p.get('mgnMode')} posSide={p.get('posSide')}")
    except Exception as e:
        log.error(f"  [DIAG-POSITION] Exception lecture position {inst_id} : {e}")

async def okx_placer_ordre_marche(session, inst_id, side, taille_contrats):
    """Place un ordre au marché. side = 'buy' ou 'sell'. Retourne l'ordId si
    succès, sinon None. taille_contrats est en nombre de contrats OKX (PAS en
    USD ni en quantité de crypto brute) — voir le TODO de conversion ctVal
    dans executer_trade avant tout usage réel."""
    path = "/api/v5/trade/order"
    body = json.dumps({
        "instId": inst_id,
        "tdMode": "isolated",
        "side": side,
        "ordType": "market",
        "sz": str(taille_contrats),
    })
    try:
        async with session.post(
            OKX_BASE_URL + path, data=body,
            headers=_okx_headers("POST", path, body),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  ❌ Erreur ordre {inst_id} {side} : {data}")
                return None
            ord_id = data.get("data", [{}])[0].get("ordId")
            log.warning(f"  💰 ORDRE RÉEL PLACÉ : {inst_id} {side} {taille_contrats} contrats (ordId={ord_id})")
            return ord_id
    except Exception as e:
        log.error(f"  ❌ Exception ordre {inst_id} : {e}")
        return None

async def okx_fermer_position(session, inst_id):
    """Ferme intégralement la position ouverte sur cet instrument (marge isolée).
    Retourne True si la position est fermée (ou déjà inexistante), False
    seulement en cas de véritable échec où une position existe encore."""
    path = "/api/v5/trade/close-position"
    body = json.dumps({"instId": inst_id, "mgnMode": "isolated"})
    try:
        async with session.post(
            OKX_BASE_URL + path, data=body,
            headers=_okx_headers("POST", path, body),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                # CAS SPÉCIAL : code 51023 = "Position doesn't exist" — ça ne
                # veut PAS dire que la fermeture a échoué, ça veut dire qu'il
                # n'y a déjà plus de position à fermer (but atteint, par ex.
                # déjà close par un mécanisme OKX). Ce n'est pas une erreur.
                sCode = None
                if isinstance(data.get("data"), list) and data["data"]:
                    sCode = data["data"][0].get("sCode")
                if data.get("code") == "51023" or sCode == "51023":
                    log.info(f"  ℹ️ Position déjà inexistante pour {inst_id} (51023) — "
                             f"rien à fermer, considéré comme fermé.")
                    return True
                log.error(f"  ❌ Erreur fermeture position {inst_id} : {data}")
                return False
            log.warning(f"  💰 POSITION RÉELLE FERMÉE : {inst_id}")
            return True
    except Exception as e:
        log.error(f"  ❌ Exception fermeture {inst_id} : {e}")
        return False

async def telegram(session, message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        await session.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        log.error(f"Erreur Telegram : {e}")

# ═══════════════════════════════════════════════════════════════
#  DONNÉES MARCHÉ — OKX
# ═══════════════════════════════════════════════════════════════
async def get_klines(session, symbole, interval=15, limite=50):
    okx_symbol = OKX_SYMBOLS.get(symbole, symbole)
    bar = {15: "15m", 60: "1H"}.get(interval, "15m")
    try:
        async with session.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": okx_symbol, "bar": bar, "limit": str(limite)},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                return None
            candles = data.get("data", [])
            if not candles:
                return None
            candles = list(reversed(candles))  # OKX renvoie du plus récent au plus ancien
            df = pd.DataFrame(candles, columns=[
                'time', 'open', 'high', 'low', 'close',
                'volume', 'volCcy', 'volCcyQuote', 'confirm'
            ])
            df = df.astype({
                'open': float, 'high': float, 'low': float,
                'close': float, 'volume': float
            })
            return df.tail(limite).reset_index(drop=True)
    except Exception as e:
        log.error(f"Erreur klines {symbole} : {e}")
        return None

async def get_prix_rest(session, symbole):
    """Prix via l'API REST OKX (fallback tant que le WebSocket n'a pas encore
    poussé de tick, ou si le cache est périmé)."""
    okx_symbol = OKX_SYMBOLS.get(symbole, symbole)
    try:
        async with session.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": okx_symbol},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                return None
            result = data.get("data", [])
            if not result:
                return None
            return float(result[0]["last"])
    except Exception as e:
        log.error(f"Erreur prix REST {symbole} : {e}")
        return None

async def get_prix_actuel(session, symbole):
    """Prix temps réel : lit le cache alimenté par le WebSocket OKX.
    Double contrôle de fraîcheur : si le dernier tick reçu pour ce marché date
    de plus de SEUIL_FRAICHEUR_PRIX_SEC, on ne lui fait plus confiance (marché
    peu liquide où OKX ne pousse pas de tick pendant plusieurs secondes) et on
    va chercher un prix frais via REST — pour éviter de piloter un stop loss
    sur une valeur périmée qui a pu bouger fortement entre-temps."""
    prix_ws = PRIX_LIVE.get(symbole)
    ts_ws   = PRIX_LIVE_TS.get(symbole)
    if prix_ws is not None and ts_ws is not None and (time.time() - ts_ws) <= SEUIL_FRAICHEUR_PRIX_SEC:
        return prix_ws
    prix_rest = await get_prix_rest(session, symbole)
    if prix_rest is not None:
        return prix_rest
    return prix_ws  # REST a échoué mais on a quand même un prix WS (même périmé) → mieux que rien

async def _ws_keepalive(ws):
    """Envoie un 'ping' applicatif toutes les 20s — requis par OKX pour garder
    la connexion WebSocket ouverte (sinon fermeture après ~30s d'inactivité)."""
    try:
        while not ws.closed:
            await asyncio.sleep(20)
            if not ws.closed:
                await ws.send_str("ping")
    except Exception:
        pass

async def websocket_prix(session):
    """Connexion WebSocket temps réel OKX (canal public 'tickers').
    Remplace le polling REST pour la détection de signal : PRIX_LIVE est mis
    à jour en continu, dès qu'OKX pousse un tick, pour les marchés suivis.
    Se reconnecte automatiquement en cas de coupure, et relit MARCHES à
    chaque reconnexion (donc prend en compte les marchés ajoutés/retirés par
    le rafraîchissement quotidien de minuit)."""
    global WS_CONNEXION_ACTIVE
    url = "wss://ws.okx.com:8443/ws/v5/public"

    while True:
        keepalive_task = None
        if not MARCHES:
            await asyncio.sleep(5)
            continue
        args = [{"channel": "tickers", "instId": OKX_SYMBOLS[m]} for m in MARCHES if m in OKX_SYMBOLS]
        try:
            async with session.ws_connect(url, heartbeat=25) as ws:
                WS_CONNEXION_ACTIVE = ws
                await ws.send_json({"op": "subscribe", "args": args})
                keepalive_task = asyncio.create_task(_ws_keepalive(ws))
                log.info(f"  🔌 WebSocket OKX connecté — abonnement tickers ({len(args)} marchés)")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if msg.data == "pong":
                            continue
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        if data.get("event") in ("subscribe", "error"):
                            if data.get("event") == "error":
                                log.error(f"  Erreur abonnement WebSocket : {data}")
                            continue
                        if "data" in data and "arg" in data:
                            inst_id = data["arg"].get("instId")
                            symbole = None
                            for s, i in OKX_SYMBOLS.items():
                                if i == inst_id:
                                    symbole = s
                                    break
                            ticks = data.get("data", [])
                            if symbole and ticks:
                                try:
                                    PRIX_LIVE[symbole]    = float(ticks[0]["last"])
                                    PRIX_LIVE_TS[symbole] = time.time()
                                except (KeyError, ValueError, TypeError):
                                    pass
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break
        except Exception as e:
            log.error(f"Erreur WebSocket OKX : {e}")
        finally:
            WS_CONNEXION_ACTIVE = None
            if keepalive_task:
                keepalive_task.cancel()

        log.warning("  🔌 WebSocket OKX déconnecté — reconnexion dans 5s")
        await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════
#  INDICATEURS
# ═══════════════════════════════════════════════════════════════
def calc_atr(df, periode=14):
    try:
        val = AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close'], window=periode
        ).average_true_range().iloc[-1]
        return round(float(val), 8) if not pd.isna(val) else 0.0
    except Exception:
        return 0.0

def calc_volume_ratio(df):
    """Ratio bougie fermée vs moyenne 24h."""
    try:
        volumes = df['volume'].tolist()
        if len(volumes) < 10:
            return 0.0
        echantillon = volumes[-25:-1]
        nb          = len(echantillon)
        if nb == 0:
            return 0.0
        moyenne = sum(echantillon) / nb
        recent  = volumes[-2]   # dernière bougie FERMÉE
        return round(recent / moyenne, 2) if moyenne > 0 else 0.0
    except Exception:
        return 0.0

def calc_rsi_1h(df, periode=14):
    """Calcule le RSI sur les bougies 1h."""
    try:
        if len(df) < periode + 1:
            return 50.0
        val = RSIIndicator(close=df['close'], window=periode).rsi().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 50.0
    except Exception:
        return 50.0

# ═══════════════════════════════════════════════════════════════
#  DÉTECTION SIGNAL — SURVEILLANCE TEMPS RÉEL
# ═══════════════════════════════════════════════════════════════
async def analyser_marche(session, symbole):
    prix_actuel = await get_prix_actuel(session, symbole)
    if prix_actuel is None:
        return "NEUTRE", {}

    # Enregistrement du prix de référence au premier passage
    if symbole not in prix_reference:
        prix_reference[symbole] = prix_actuel
        log.info(f"  {symbole} : prix référence enregistré @ {prix_actuel}")
        return "NEUTRE", {}

    prix_ref = prix_reference[symbole]
    if prix_ref <= 0:
        prix_reference[symbole] = prix_actuel
        return "NEUTRE", {}

    variation_pct = (prix_actuel - prix_ref) / prix_ref * 100

    # Récupération des données techniques
    df_15m = await get_klines(session, symbole, interval=15, limite=50)
    df_1h  = await get_klines(session, symbole, interval=60, limite=50)

    vol_ratio = 0.0
    atr_val   = 0.0
    rsi_1h    = 50.0

    if df_15m is not None and len(df_15m) >= 15:
        vol_ratio = calc_volume_ratio(df_15m)
        atr_val   = calc_atr(df_15m)

    if df_1h is not None and len(df_1h) >= 20:
        rsi_1h = calc_rsi_1h(df_1h, RSI_PERIODE)

    # Filtre volume
    if vol_ratio < VOLUME_MINI:
        log.info(f"  {symbole} : Vol {vol_ratio:.2f}x | Variation={variation_pct:+.2f}% → skip volume")
        return "NEUTRE", {}

    details = {
        "atr":           atr_val,
        "vol_ratio":     vol_ratio,
        "rsi_1h":        rsi_1h,
        "variation_pct": abs(variation_pct),
        "prix_ref":      prix_ref,
        "prix_actuel":   prix_actuel,
    }

    # Signal ACHAT : prix a chuté de >= 0.50%
    if variation_pct <= -SEUIL_MOUVEMENT_PCT:
        prix_reference[symbole] = prix_actuel
        if rsi_1h < RSI_SEUIL_BAS:
            log.info(f"  {symbole} ACHAT->VENTE | RSI={rsi_1h} < {RSI_SEUIL_BAS} | Vol={vol_ratio:.2f}x")
            return "VENTE", details
        else:
            log.info(f"  {symbole} ACHAT | Chute={variation_pct:.2f}% | RSI={rsi_1h} | Vol={vol_ratio:.2f}x")
            return "ACHAT", details

    # Signal VENTE : prix a monté de >= 0.50%
    if variation_pct >= SEUIL_MOUVEMENT_PCT:
        prix_reference[symbole] = prix_actuel
        if rsi_1h > RSI_SEUIL_HAUT:
            log.info(f"  {symbole} VENTE->ACHAT | RSI={rsi_1h} > {RSI_SEUIL_HAUT} | Vol={vol_ratio:.2f}x")
            return "ACHAT", details
        else:
            log.info(f"  {symbole} VENTE | Montée={variation_pct:.2f}% | RSI={rsi_1h} | Vol={vol_ratio:.2f}x")
            return "VENTE", details

    log.info(f"  {symbole} : Variation={variation_pct:+.2f}% (seuil +/-{SEUIL_MOUVEMENT_PCT}%) | RSI={rsi_1h}")
    return "NEUTRE", {}

# ═══════════════════════════════════════════════════════════════
#  GESTION MISE DYNAMIQUE
# ═══════════════════════════════════════════════════════════════
def calculer_mise(capital, etat):
    wins_consec = etat.get("wins_consecutifs", 0)

    mise = capital * MISE_BASE_PCT

    # Boost après plusieurs gains consécutifs
    if wins_consec >= WINS_CONFIANCE:
        mise *= BOOST_CONFIANCE
        log.info(f"  Mise boostée +20% ({wins_consec} wins consecutifs)")

    mise    = max(mise, MISE_MIN)
    plafond = capital * MISE_MAX_PCT

    if plafond >= MISE_MIN:
        mise = min(mise, plafond)
    else:
        # Capital trop faible pour respecter le plafond ET le plancher en
        # même temps (ne devrait jamais arriver en pratique : SEUIL_RUINE
        # arrête le bot bien avant — capital < ~83€ avec les réglages
        # actuels). Le plancher MISE_MIN prime dans ce cas, pour ne jamais
        # ouvrir un trade en dessous du seuil où les frais deviennent
        # disproportionnés par rapport au gain visé.
        log.warning(f"  ⚠️ Capital très faible ({capital}€) : plafond mise "
                    f"({plafond}€) < plancher ({MISE_MIN}€) — plancher appliqué")

    return round(mise, 2)

# ═══════════════════════════════════════════════════════════════
#  CALCUL FRAIS OKX
# ═══════════════════════════════════════════════════════════════
def calc_frais(position):
    """Calcule les frais réels OKX : ouverture + fermeture (taker, 0.05% chacune)."""
    frais_ouv  = round(position * OKX_TAKER_FEE, 4)
    frais_ferm = round(position * OKX_TAKER_FEE, 4)
    total      = round(frais_ouv + frais_ferm, 4)
    return {
        "ouverture": frais_ouv,
        "fermeture": frais_ferm,
        "total":     total
    }

# ═══════════════════════════════════════════════════════════════
#  EXÉCUTION D'UN TRADE
# ═══════════════════════════════════════════════════════════════
async def executer_trade(session, symbole, direction, capital, details, etat_global):
    prix_entree = await get_prix_actuel(session, symbole)
    if prix_entree is None or prix_entree <= 0:
        async with trades_lock:
            trades_ouverts.pop(symbole, None)
        return

    mise = calculer_mise(capital, etat_global)
    position = round(mise * LEVIER, 2)

    # Stop loss en pourcentage du prix d'entrée (fixe en %, pas en €)
    ratio_prix    = STOP_LOSS_PCT
    stop_loss_eur = round(position * ratio_prix, 2)  # équivalent € affiché, dérivé du %

    rsi_1h = details.get("rsi_1h", 50.0)

    # Calcul stop et objectif en prix
    if direction == "ACHAT":
        stop_initial   = round(prix_entree * (1 - ratio_prix), 8)
        objectif_final = round(prix_entree * (1 + ratio_prix * 2), 8)
    else:
        stop_initial   = round(prix_entree * (1 + ratio_prix), 8)
        objectif_final = round(prix_entree * (1 - ratio_prix * 2), 8)

    # Frais d'ouverture
    frais_ouv = round(position * OKX_TAKER_FEE, 4)

    # Numéro de trade — sera attribué dans le lock final
    numero_trade = 0

    log.info(f"\n  {'='*55}")
    log.info(f"  TRADE EN COURS — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  {symbole} ({direction})")
    log.info(f"  Variation : {details.get('variation_pct', 0):.2f}% | "
             f"Ref={details.get('prix_ref')} → {details.get('prix_actuel')}")
    log.info(f"  Vol={details.get('vol_ratio', 0):.2f}x | RSI 1h={rsi_1h} | Stop fixe : -{stop_loss_eur}€")
    log.info(f"  Prix entrée : {prix_entree} | Stop : {stop_initial} | Obj : {objectif_final}")
    log.info(f"  Mise : {mise}€ x x{LEVIER} = {position}€ | Frais ouv : -{frais_ouv}€ | Trades : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}\n")

    await telegram(session,
        f"📊 <b>TRADE OUVERT</b>\n"
        f"{'🟢 ACHAT' if direction == 'ACHAT' else '🔴 VENTE'} {symbole}\n"
        f"Variation : {details.get('variation_pct', 0):.2f}% depuis ref\n"
        f"Volume : {details.get('vol_ratio', 0):.2f}x | RSI 1h : {rsi_1h}\n"
        f"Prix : {prix_entree} | Stop : {stop_initial}\n"
        f"Mise : {mise}€ x x{LEVIER} = {position}€\n"
        f"Frais ouverture : -{frais_ouv}€ | Stop max : -{stop_loss_eur}€\n"
        f"Trades : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}"
    )

    # ── Exécution réelle — INACTIF tant que MODE_REEL=0 (comportement simulé
    # inchangé dans ce cas). Voir l'avertissement en tête du bloc des
    # fonctions okx_* : code non testé en conditions réelles, à valider sur
    # le Demo Trading OKX avant tout usage avec de l'argent réel.
    inst_id = None
    if MODE_REEL:
        ct_val  = OKX_CT_VAL.get(symbole, 0)
        inst_id = OKX_SYMBOLS_EXEC.get(symbole)  # déjà résolu par filtrer_marches_selon_compte

        if not inst_id:
            # Filet de sécurité : marché pas encore résolu (ex: ajouté entre
            # deux rafraîchissements) — on le résout à la volée.
            base    = symbole[:-3]
            inst_id = await okx_resoudre_instid_reel(session, base)

        if not inst_id or not ct_val:
            log.error(f"  ❌ Impossible de résoudre l'instId d'exécution pour {symbole} — trade annulé")
            await telegram(session, f"❌ <b>TRADE ANNULÉ</b>\n{symbole} : instId d'exécution introuvable pour ce compte.")
            async with trades_lock:
                trades_ouverts.pop(symbole, None)
            return

        taille_contrats = round(position / (prix_entree * ct_val), 0)
        if taille_contrats < 1:
            log.error(f"  ❌ MODE_REEL actif mais taille calculée < 1 contrat pour {symbole} — trade annulé")
            await telegram(session, f"❌ <b>TRADE ANNULÉ</b>\n{symbole} : taille de position trop petite (<1 contrat).")
            async with trades_lock:
                trades_ouverts.pop(symbole, None)
            return

        log.info(f"  [DIAG-SOLDE] Avant ordre {symbole} — état du compte :")
        await okx_diag_solde(session)

        await okx_definir_levier(session, inst_id, LEVIER)
        side = "buy" if direction == "ACHAT" else "sell"
        ord_id = await okx_placer_ordre_marche(session, inst_id, side, taille_contrats)
        if ord_id is None:
            log.error(f"  ❌ Échec de l'ordre réel pour {symbole} — trade annulé")
            await telegram(session, f"❌ <b>TRADE ANNULÉ</b>\n{symbole} : l'ordre réel a échoué côté OKX. Vérifie les logs.")
            async with trades_lock:
                trades_ouverts.pop(symbole, None)
            return

        await asyncio.sleep(1)  # laisse le temps au matching engine de remplir avant de vérifier
        await okx_diag_statut_ordre(session, inst_id, ord_id)

    debut           = time.time()
    dernier_log     = 0
    pnl_max_atteint = 0.0
    lock_actuel     = 0.0
    resultat_final  = "PERDU"
    gain_final      = -stop_loss_eur
    prix_sortie     = prix_entree
    pnl             = 0.0
    duree           = 0

    # ── Boucle de surveillance — jusqu'au stop, au lock, ou 6h max
    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        prix_actuel = await get_prix_actuel(session, symbole)
        if prix_actuel is None:
            continue

        prix_sortie = prix_actuel
        duree       = int((time.time() - debut) / 60)

        # Calcul PnL brut
        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)

        if pnl > pnl_max_atteint:
            pnl_max_atteint = pnl

        # Lock paliers
        nouveau_lock = get_palier_lock(pnl_max_atteint, capital)
        if nouveau_lock > lock_actuel:
            lock_actuel = nouveau_lock
            log.info(f"  LOCK {lock_actuel}€ GARANTI [{symbole}] (PnL max={pnl_max_atteint:.2f}€)")
            await telegram(session,
                f"🔒 <b>{lock_actuel}€ garanti !</b>\n"
                f"{symbole} | PnL max : +{pnl_max_atteint:.2f}€\n"
                f"Gain verrouillé ✅"
            )

        # Stop loss — calculé et vérifié EN PREMIER, avant toute logique de
        # lock. Priorité absolue : si le prix a franchi le niveau de stop,
        # c'est un STOP, peu importe qu'un palier de lock ait été posé
        # auparavant. AVANT ce correctif, un effondrement brutal du prix
        # après un lock pouvait déclencher la branche "Sortie lock" (qui ne
        # vérifie que pnl < lock_actuel, sans limite basse) et rapporter le
        # gain du palier au lieu de la perte réelle — alors que la position
        # réelle pouvait déjà avoir été liquidée par OKX entre-temps.
        atteint_stop = (prix_actuel <= stop_initial if direction == "ACHAT"
                        else prix_actuel >= stop_initial)

        # Log toutes les minutes
        if time.time() - dernier_log >= 60:
            lock_flag = f" LOCK{lock_actuel}€" if lock_actuel > 0 else ""
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole} {prix_actuel} | "
                     f"PnL {'+' if pnl>=0 else ''}{pnl:.2f}€{lock_flag} | {duree}min")
            dernier_log = time.time()

        if atteint_stop:
            frais   = calc_frais(position)
            pnl_net = round(pnl - frais["total"], 4)
            if pnl_net > 0:
                resultat_final = "GAGNE"
            else:
                resultat_final = "PERDU"
            log.info(f"\n  STOP [{symbole}] {'+' if pnl>=0 else ''}{pnl:.2f}€ | net={pnl_net:+.4f}€ | {duree}min")
            await telegram(session,
                f"🛑 <b>STOP</b>\n"
                f"{symbole} {direction}\n"
                f"PnL brut : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                f"Frais (ouv+ferm) : -{frais['total']}€\n"
                f"Résultat net : {'+' if pnl_net>=0 else ''}{pnl_net}€\n"
                f"Durée : {duree} min"
            )
            gain_final = pnl_net
            break

        # Sortie lock : PnL redescend sous le palier verrouillé (mais le
        # stop n'est pas atteint, sinon on serait déjà sorti ci-dessus)
        if lock_actuel > 0 and pnl < lock_actuel:
            frais    = calc_frais(position)
            gain_net = round(lock_actuel - frais["total"], 4)
            log.info(f"\n  SORTIE LOCK [{symbole}] +{lock_actuel}€ (max={pnl_max_atteint:.2f}€) | {duree}min")
            await telegram(session,
                f"🔒 <b>SORTIE LOCK</b>\n"
                f"{symbole} | {direction}\n"
                f"Gain brut verrouillé : +{lock_actuel}€\n"
                f"Frais (ouv+ferm) : -{frais['total']}€\n"
                f"Gain net : +{gain_net}€\n"
                f"PnL max : +{pnl_max_atteint:.2f}€\n"
                f"Durée : {duree} min"
            )
            resultat_final = "GAGNE"
            gain_final     = gain_net
            break

        # Durée maximale : fermeture forcée à 6h si ni stop ni lock atteint
        if duree >= DUREE_MAX_MINUTES:
            frais   = calc_frais(position)
            pnl_net = round(pnl - frais["total"], 4)
            resultat_final = "GAGNE" if pnl_net > 0 else "PERDU"
            log.warning(f"\n  ⏰ DURÉE MAX ATTEINTE [{symbole}] {'+' if pnl>=0 else ''}{pnl:.2f}€ | "
                        f"net={pnl_net:+.4f}€ | {duree}min")
            await telegram(session,
                f"⏰ <b>DURÉE MAX (6h) — FERMETURE FORCÉE</b>\n"
                f"{symbole} {direction}\n"
                f"PnL brut : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                f"Frais (ouv+ferm) : -{frais['total']}€\n"
                f"Résultat net : {'+' if pnl_net>=0 else ''}{pnl_net}€\n"
                f"Durée : {duree} min"
            )
            gain_final = pnl_net
            break

    # ── Fermeture réelle de la position — INACTIF tant que MODE_REEL=0
    if MODE_REEL and inst_id:
        await okx_diag_position(session, inst_id)
        succes_fermeture = await okx_fermer_position(session, inst_id)
        if not succes_fermeture:
            log.error(f"  ❌ ÉCHEC DE FERMETURE RÉELLE pour {symbole} — intervention manuelle probablement nécessaire")
            await telegram(session,
                f"🚨 <b>ALERTE — ÉCHEC FERMETURE RÉELLE</b>\n"
                f"{symbole} : la position réelle n'a peut-être pas été fermée côté OKX.\n"
                f"Vérifie manuellement sur l'app OKX immédiatement."
            )

    # ── Libérer le marché + mise à jour état global dans un seul lock
    async with trades_lock:
        trades_ouverts.pop(symbole, None)
        cooldown_marches.pop(symbole, None)
        log.info(f"  [{symbole}] libéré")

        # Mise à jour capital et stats dans le même lock — pas de race condition
        etat_global["nb_trades"] = etat_global.get("nb_trades", 0) + 1
        numero_trade             = etat_global["nb_trades"]
        etat_global["capital"]   = round(etat_global["capital"] + gain_final, 2)
        etat_global["cumul_net"] = round(etat_global["capital"] - CAPITAL_INITIAL, 2)
        etat_global["pnl_jour"]  = round(etat_global.get("pnl_jour", 0) + gain_final, 2)

        if resultat_final == "GAGNE":
            etat_global["nb_wins"]             = etat_global.get("nb_wins", 0) + 1
            etat_global["total_gagne"]         = round(etat_global.get("total_gagne", 0) + gain_final, 2)
            etat_global["pertes_consecutives"] = 0
            etat_global["wins_consecutifs"]    = etat_global.get("wins_consecutifs", 0) + 1
        else:
            etat_global["nb_losses"]           = etat_global.get("nb_losses", 0) + 1
            etat_global["total_perdu"]         = round(etat_global.get("total_perdu", 0) + abs(gain_final), 2)
            etat_global["pertes_consecutives"] = etat_global.get("pertes_consecutives", 0) + 1
            etat_global["wins_consecutifs"]    = 0

        etat_global.setdefault("historique", []).append({
            'heure':         (datetime.utcnow() - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M'),
            'marche':        symbole,
            'direction':     direction,
            'resultat':      resultat_final,
            'gain':          round(gain_final, 2),
            'mise':          round(mise, 2),
            'capital':       etat_global["capital"],
            'duree_minutes': duree,
            'rsi':           rsi_1h,
            'vol_ratio':     details.get("vol_ratio", 0.0),
        })

    enregistrer_trade({
        'marche':        symbole,
        'direction':     direction,
        'resultat':      resultat_final,
        'prix_entree':   prix_entree,
        'prix_sortie':   prix_sortie,
        'stop_loss':     stop_initial,
        'objectif':      objectif_final,
        'mise':          mise,
        'gain':          round(gain_final, 2),
        'capital_apres': etat_global['capital'],
        'duree_minutes': duree,
        'score':         None,
        'adx':           None,
        'atr':           details.get("atr", None),
        'rsi':           rsi_1h,
    })
    sauvegarder_etat(etat_global)
    afficher_tableau_de_bord(etat_global)

    # Rapport Telegram après chaque trade
    nb_trades_total = etat_global.get("nb_trades", 0)
    nb_wins   = etat_global.get("nb_wins", 0)
    win_rate  = (nb_wins / nb_trades_total * 100) if nb_trades_total > 0 else 0
    perf      = (etat_global["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100
    await telegram(session,
        f"📈 <b>RAPPORT — Trade #{numero_trade}</b>\n"
        f"Capital : <b>{round(etat_global['capital'],2)}€</b> "
        f"({'+' if perf>=0 else ''}{round(perf,2)}%)\n"
        f"PnL jour : {'+' if etat_global.get('pnl_jour',0)>=0 else ''}"
        f"{round(etat_global.get('pnl_jour',0),2)}€\n"
        f"Trades : {nb_trades_total} | WR : {round(win_rate,1)}%\n"
        f"Gagné : +{round(etat_global.get('total_gagne',0),2)}€ | "
        f"Perdu : -{round(etat_global.get('total_perdu',0),2)}€\n"
        f"<b>NET : {'+' if etat_global.get('cumul_net',0)>=0 else ''}"
        f"{round(etat_global.get('cumul_net',0),2)}€</b>"
    )

# ═══════════════════════════════════════════════════════════════
#  PROTECTIONS
# ═══════════════════════════════════════════════════════════════
def verifier_protections(etat, capital):
    if capital < SEUIL_RUINE:
        log.critical(f"SEUIL RUINE ! Capital {capital}€ → ARRET")
        return "RUINE"
    if etat.get("pnl_jour", 0.0) <= KILL_SWITCH_JOUR:
        log.warning(f"KILL SWITCH — PnL jour {etat.get('pnl_jour', 0)}€")
        return "KILL_SWITCH"
    return "OK"

def reset_pnl_jour_si_nouveau_jour(etat):
    """Retourne True si le PnL du jour a été remis à 0 (changement de jour)."""
    maintenant_guyane = datetime.utcnow() - timedelta(hours=3)
    aujourd_hui = maintenant_guyane.strftime('%Y-%m-%d')
    if etat.get("date_jour", "") != aujourd_hui:
        etat["pnl_jour"]  = 0.0
        etat["date_jour"] = aujourd_hui
        log.info("  Nouveau jour — PnL remis à 0")
        return True
    return False

# ═══════════════════════════════════════════════════════════════
#  RAPPORT QUOTIDIEN
# ═══════════════════════════════════════════════════════════════
async def envoyer_rapport_quotidien(session, etat):
    """Envoie chaque jour à 19h Guyane (22h UTC)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import io

    historique        = etat.get("historique", [])
    maintenant_guyane = datetime.utcnow() - timedelta(hours=3)
    aujourd_hui       = maintenant_guyane.strftime('%Y-%m-%d')
    date_affich       = maintenant_guyane.strftime('%d/%m/%Y')

    trades_jour = [h for h in historique if h.get("heure", "")[:10] == aujourd_hui]
    if not trades_jour:
        return

    gains_jour   = {}
    wins_jour    = {}
    pertes_jour  = {}
    rsi_jour     = {}
    duree_wins   = []
    duree_pertes = []
    vol_wins     = []
    vol_pertes   = []
    heure_pertes = {}

    for h in trades_jour:
        marche   = h.get("marche", "?")
        gain     = h.get("gain", 0)
        resultat = h.get("resultat", "")
        duree    = h.get("duree_minutes", 0)
        rsi      = h.get("rsi", 50.0)
        vol      = h.get("vol_ratio", 0.0)
        heure_str = h.get("heure", "")

        gains_jour[marche]  = round(gains_jour.get(marche, 0) + gain, 2)
        rsi_jour.setdefault(marche, []).append(rsi)

        if resultat == "GAGNE":
            wins_jour[marche] = wins_jour.get(marche, 0) + 1
            duree_wins.append(duree)
            vol_wins.append(vol)
        else:
            pertes_jour[marche] = pertes_jour.get(marche, 0) + 1
            duree_pertes.append(duree)
            vol_pertes.append(vol)
            if len(heure_str) >= 13:
                heure_guyane = int(heure_str[11:13])
                tranche = f"{heure_guyane:02d}h"
                heure_pertes[tranche] = heure_pertes.get(tranche, 0) + 1

    # Graphique capital intraday
    try:
        capitaux_jour = []
        heures_jour   = []
        for h in trades_jour:
            heures_jour.append(h.get("heure", "")[11:16])
            capitaux_jour.append(h.get("capital", etat["capital"]))

        if len(capitaux_jour) >= 2:
            fig, ax = plt.subplots(figsize=(10, 4))
            fig.patch.set_facecolor('#1a1a2e')
            ax.set_facecolor('#16213e')
            ax.plot(range(len(capitaux_jour)), capitaux_jour,
                    color='#e94560', linewidth=2.5,
                    marker='o', markersize=5,
                    markerfacecolor='white', markeredgecolor='#e94560')
            ax.axhline(y=capitaux_jour[0], color='#ffffff',
                       linewidth=1, linestyle='--', alpha=0.4)
            ax.set_xticks(range(len(heures_jour)))
            ax.set_xticklabels(heures_jour, color='#aaaaaa', fontsize=7, rotation=45)
            ax.set_ylabel('Capital (€)', color='#aaaaaa', fontsize=9)
            ax.tick_params(colors='#aaaaaa')
            for spine in ax.spines.values():
                spine.set_color('#333366')
            ax.grid(True, alpha=0.1, color='#ffffff')
            pnl_jour = round(etat.get("pnl_jour", 0), 2)
            ax.set_title(
                f'Journee du {date_affich}\n'
                f'PnL jour : {"+"+str(pnl_jour)+"€" if pnl_jour>=0 else str(pnl_jour)+"€"}'
                f' | Capital : {etat["capital"]}€',
                color='white', fontsize=11, fontweight='bold', pad=10)
            plt.tight_layout(pad=1.5)
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=150,
                        bbox_inches='tight', facecolor='#1a1a2e')
            buf.seek(0)
            plt.close()
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
                form_data = aiohttp.FormData()
                form_data.add_field('chat_id', TELEGRAM_CHAT_ID)
                form_data.add_field('caption', f'Journee du {date_affich}')
                form_data.add_field('photo', buf, filename='journee.png',
                                    content_type='image/png')
                await session.post(url_photo, data=form_data,
                                   timeout=aiohttp.ClientTimeout(total=30))
    except Exception as e:
        log.error(f"Erreur graphique quotidien : {e}")

    classement   = sorted(gains_jour.items(), key=lambda x: x[1], reverse=True)
    total_jour   = round(sum(gains_jour.values()), 2)
    nb_trades    = len(trades_jour)
    nb_wins      = sum(wins_jour.values())
    win_rate     = round(nb_wins / nb_trades * 100, 1) if nb_trades > 0 else 0
    perf         = round((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100, 2)
    duree_moy_w  = round(sum(duree_wins) / len(duree_wins), 0) if duree_wins else 0
    duree_moy_p  = round(sum(duree_pertes) / len(duree_pertes), 0) if duree_pertes else 0
    vol_moy_w    = round(sum(vol_wins) / len(vol_wins), 2) if vol_wins else 0
    vol_moy_p    = round(sum(vol_pertes) / len(vol_pertes), 2) if vol_pertes else 0

    lignes_marches = []
    for marche, gain in classement:
        emoji  = "✅" if gain >= 0 else "❌"
        s_gain = f"{'+' if gain>=0 else ''}{gain}€"
        s_wl   = f"{wins_jour.get(marche,0)}G/{pertes_jour.get(marche,0)}P"
        rsi_list = rsi_jour.get(marche, [50.0])
        rsi_m  = round(sum(rsi_list) / len(rsi_list), 1)
        lignes_marches.append(
            f"{emoji} <code>{marche:<12} {s_gain:<10} {s_wl:<6} RSI:{rsi_m}</code>"
        )

    if heure_pertes:
        pertes_triees   = sorted(heure_pertes.items(), key=lambda x: x[1], reverse=True)
        lignes_pertes_h = " | ".join([f"{h}:{n}" for h, n in pertes_triees[:5]])
    else:
        lignes_pertes_h = "Aucune perte"

    top3   = classement[:3]
    pires3 = classement[-3:][::-1]
    msg_top  = "\n".join([f"🏆 {m} {'+' if g>=0 else ''}{g}€" for m, g in top3])
    msg_pire = "\n".join([f"💀 {m} {g}€" for m, g in pires3 if g < 0])

    message = (
        f"📊 <b>RAPPORT QUOTIDIEN</b>\n"
        f"Journee du {date_affich}\n\n"
        f"💰 <b>RÉSULTAT</b>\n"
        f"Total jour : <b>{'+' if total_jour>=0 else ''}{total_jour}€</b>\n"
        f"Capital : {round(etat['capital'],2)}€ ({'+' if perf>=0 else ''}{perf}%)\n"
        f"Trades : {nb_trades} | WR : {win_rate}%\n\n"
        f"📈 <b>TOP MARCHÉS</b>\n{msg_top}\n\n"
        + (f"📉 <b>PIRES MARCHÉS</b>\n{msg_pire}\n\n" if msg_pire else "") +
        f"⏱ <b>DURÉE MOYENNE</b>\n"
        f"Gagnants : {int(duree_moy_w)}min | Perdants : {int(duree_moy_p)}min\n\n"
        f"📊 <b>VOLUME MOYEN</b>\n"
        f"Gagnants : {vol_moy_w}x | Perdants : {vol_moy_p}x\n\n"
        f"🕐 <b>HEURES DES PERTES</b>\n"
        f"{lignes_pertes_h}\n\n"
        f"<code>{'─'*40}</code>\n"
        f"<b>CLASSEMENT MARCHÉS</b>\n"
        f"<code>{'MARCHÉ':<12} {'GAINS':<10} {'G/P':<6} RSI MOY</code>\n"
        f"{chr(10).join(lignes_marches)}"
    )
    log.info("  Envoi rapport quotidien Telegram")
    await telegram(session, message)

# ═══════════════════════════════════════════════════════════════
#  RAPPORT HEBDOMADAIRE
# ═══════════════════════════════════════════════════════════════
async def envoyer_rapport_hebdomadaire(session, etat):
    """Envoie chaque dimanche à 19h Guyane (22h UTC)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import io

    historique = etat.get("historique", [])
    if not historique:
        return

    maintenant     = datetime.utcnow() - timedelta(hours=3)
    il_y_a_7_jours = (maintenant - timedelta(days=7)).strftime('%Y-%m-%d')
    date_debut     = (maintenant - timedelta(days=7)).strftime('%d/%m')
    date_fin       = maintenant.strftime('%d/%m/%Y')

    gains_par_marche = {}
    capital_par_jour = {}

    for h in historique:
        if h.get("heure", "") >= il_y_a_7_jours:
            marche = h.get("marche", "?")
            gain   = h.get("gain", 0)
            jour   = h.get("heure", "")[:10]
            gains_par_marche[marche] = round(gains_par_marche.get(marche, 0) + gain, 2)
            capital_par_jour[jour]   = h.get("capital", etat["capital"])

    if not gains_par_marche:
        return

    jours_tries  = sorted(capital_par_jour.keys())
    capitaux     = [capital_par_jour[j] for j in jours_tries]
    labels_jours = [j[5:] for j in jours_tries]

    # Graphique
    try:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7),
                                        gridspec_kw={'height_ratios': [3, 1]})
        fig.patch.set_facecolor('#1a1a2e')

        ax1.set_facecolor('#16213e')
        if len(capitaux) >= 2:
            ax1.plot(range(len(jours_tries)), capitaux,
                     color='#e94560', linewidth=2.5,
                     marker='o', markersize=7,
                     markerfacecolor='white', markeredgecolor='#e94560',
                     markeredgewidth=2)
            ax1.fill_between(range(len(jours_tries)), capitaux, CAPITAL_INITIAL,
                              where=[c >= CAPITAL_INITIAL for c in capitaux],
                              color='#e94560', alpha=0.15)
            ax1.fill_between(range(len(jours_tries)), capitaux, CAPITAL_INITIAL,
                              where=[c < CAPITAL_INITIAL for c in capitaux],
                              color='#ff4444', alpha=0.25)

        ax1.axhline(y=CAPITAL_INITIAL, color='#ffffff', linewidth=1, linestyle='--', alpha=0.4)
        for i, (jour, cap) in enumerate(zip(jours_tries, capitaux)):
            couleur = '#00ff88' if cap >= CAPITAL_INITIAL else '#ff4444'
            ax1.annotate(f'{cap}€', xy=(i, cap),
                         xytext=(0, 12), textcoords='offset points',
                         ha='center', fontsize=8, color=couleur, fontweight='bold')
        ax1.set_xticks(range(len(jours_tries)))
        ax1.set_xticklabels(labels_jours, color='#aaaaaa', fontsize=9)
        ax1.set_ylabel('Capital (€)', color='#aaaaaa', fontsize=10)
        ax1.tick_params(colors='#aaaaaa')
        for spine in ax1.spines.values():
            spine.set_color('#333366')
        ax1.grid(True, alpha=0.1, color='#ffffff')

        net  = etat["capital"] - CAPITAL_INITIAL
        perf = (net / CAPITAL_INITIAL) * 100
        ax1.set_title(
            f'Progression du capital\n'
            f'NET : {"+"+str(round(net,2))+"€" if net>=0 else str(round(net,2))+"€"}'
            f' ({"+"+str(round(perf,2))+"%" if perf>=0 else str(round(perf,2))+"%"})'
            f' | Capital : {etat["capital"]}€',
            color='white', fontsize=11, fontweight='bold', pad=12)

        ax2.set_facecolor('#16213e')
        pnl_valeurs = []
        for i, jour in enumerate(jours_tries):
            if i == 0:
                pnl_valeurs.append(round(capitaux[0] - CAPITAL_INITIAL, 2))
            else:
                pnl_valeurs.append(round(capitaux[i] - capitaux[i-1], 2))

        couleurs = ['#00ff88' if p >= 0 else '#ff4444' for p in pnl_valeurs]
        bars = ax2.bar(range(len(jours_tries)), pnl_valeurs,
                        color=couleurs, alpha=0.8, width=0.6)
        ax2.axhline(y=0, color='#ffffff', linewidth=0.8, alpha=0.4)
        ax2.set_xticks(range(len(jours_tries)))
        ax2.set_xticklabels(labels_jours, color='#aaaaaa', fontsize=9)
        ax2.set_ylabel('PnL jour (€)', color='#aaaaaa', fontsize=9)
        ax2.tick_params(colors='#aaaaaa')
        for spine in ax2.spines.values():
            spine.set_color('#333366')
        ax2.grid(True, alpha=0.1, color='#ffffff', axis='y')
        for bar, val in zip(bars, pnl_valeurs):
            if val != 0:
                couleur = '#00ff88' if val >= 0 else '#ff4444'
                ax2.text(bar.get_x() + bar.get_width()/2,
                         bar.get_height() + (0.2 if val >= 0 else -1.2),
                         f'{"+"+str(val)+"€" if val >= 0 else str(val)+"€"}',
                         ha='center', fontsize=8, color=couleur, fontweight='bold')

        plt.tight_layout(pad=2.0)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150,
                    bbox_inches='tight', facecolor='#1a1a2e')
        buf.seek(0)
        plt.close()

        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            form_data = aiohttp.FormData()
            form_data.add_field('chat_id', TELEGRAM_CHAT_ID)
            form_data.add_field('caption', f'Progression semaine du {date_debut} au {date_fin}')
            form_data.add_field('photo', buf, filename='progression.png',
                                content_type='image/png')
            await session.post(url_photo, data=form_data,
                               timeout=aiohttp.ClientTimeout(total=30))
    except Exception as e:
        log.error(f"Erreur graphique hebdomadaire : {e}")

    # Rapport texte semaine + total
    gains_total    = {}
    wins_total     = {}
    pertes_total   = {}
    wins_semaine   = {}
    pertes_semaine = {}

    for h in historique:
        marche   = h.get("marche", "?")
        gain     = h.get("gain", 0)
        resultat = h.get("resultat", "")
        semaine  = h.get("heure", "") >= il_y_a_7_jours

        gains_total[marche] = round(gains_total.get(marche, 0) + gain, 2)
        if resultat == "GAGNE":
            wins_total[marche] = wins_total.get(marche, 0) + 1
        else:
            pertes_total[marche] = pertes_total.get(marche, 0) + 1

        if semaine:
            if resultat == "GAGNE":
                wins_semaine[marche] = wins_semaine.get(marche, 0) + 1
            else:
                pertes_semaine[marche] = pertes_semaine.get(marche, 0) + 1

    classement    = sorted(gains_par_marche.items(), key=lambda x: x[1], reverse=True)
    total_semaine = round(sum(gains_par_marche.values()), 2)
    total_global  = round(sum(gains_total.values()), 2)

    lignes = []
    for marche, gain_sem in classement:
        emoji  = "✅" if gain_sem >= 0 else "❌"
        s_gain = f"{'+' if gain_sem>=0 else ''}{gain_sem}€"
        s_wl   = f"{wins_semaine.get(marche,0)}G/{pertes_semaine.get(marche,0)}P"
        t_gain = gains_total.get(marche, 0)
        t_s    = f"{'+' if t_gain>=0 else ''}{t_gain}€"
        t_wl   = f"{wins_total.get(marche,0)}G/{pertes_total.get(marche,0)}P"
        lignes.append(
            f"{emoji} <code>{marche:<10} {s_gain:<10} {s_wl:<8} | {t_s:<10} {t_wl}</code>"
        )

    message = (
        f"📆 <b>RAPPORT HEBDOMADAIRE</b>\n"
        f"Semaine du {date_debut} au {date_fin}\n"
        f"<code>{'─'*44}</code>\n"
        f"<code>{'MARCHÉ':<10} {'SEMAINE':>8} {'G/P':>6}  | {'TOTAL':>8} {'G/P'}</code>\n"
        f"<code>{'─'*44}</code>\n"
        f"{chr(10).join(lignes)}\n"
        f"<code>{'─'*44}</code>\n"
        f"<b>Semaine : {'+' if total_semaine>=0 else ''}{total_semaine}€ | "
        f"Total : {'+' if total_global>=0 else ''}{total_global}€</b>"
    )
    log.info("  Envoi rapport hebdomadaire Telegram")
    await telegram(session, message)

# ═══════════════════════════════════════════════════════════════
#  TABLEAU DE BORD
# ═══════════════════════════════════════════════════════════════
def afficher_tableau_de_bord(etat):
    nb_trades = etat.get("nb_trades", 0)
    nb_wins   = etat.get("nb_wins", 0)
    win_rate  = (nb_wins / nb_trades * 100) if nb_trades > 0 else 0
    perf      = (etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100
    log.info(f"\n  {'='*55}")
    log.info(f"  BOT MEAN REVERSION — OKX X10")
    log.info(f"  {'='*55}")
    log.info(f"  Capital    : {round(etat['capital'],2)}€ ({'+' if perf>=0 else ''}{round(perf,2)}%)")
    log.info(f"  PnL jour   : {'+' if etat.get('pnl_jour',0)>=0 else ''}{round(etat.get('pnl_jour',0),2)}€")
    log.info(f"  Trades     : {nb_trades} | Wins : {nb_wins} ({win_rate:.1f}%)")
    log.info(f"  Ouverts    : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}")
    log.info(f"  Marchés x10 : {len(MARCHES)}")
    log.info(f"  Pertes c.  : {etat.get('pertes_consecutives',0)}")
    log.info(f"  Wins c.    : {etat.get('wins_consecutifs',0)}")
    log.info(f"  Gagné      : +{round(etat.get('total_gagne',0),2)}€")
    log.info(f"  Perdu      : -{round(etat.get('total_perdu',0),2)}€")
    log.info(f"  NET        : {'+' if etat.get('cumul_net',0)>=0 else ''}{round(etat.get('cumul_net',0),2)}€")
    if etat.get("historique"):
        log.info("  Derniers trades :")
        for h in etat["historique"][-5:]:
            icone = "✅" if h.get("resultat") == "GAGNE" else "❌"
            log.info(f"    {icone} {h['heure']} | {h['marche']} | "
                     f"{'+' if h['gain']>=0 else ''}{h['gain']}€")
    log.info(f"  {'='*55}")

# ═══════════════════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════
async def boucle_principale():
    global trades_lock
    trades_lock = asyncio.Lock()

    init_database()
    etat = charger_etat()

    if RESET_TOUT:
        log.warning("  🔄 RESET_TOUT activé sur Railway — remise à zéro complète de l'état du bot")
        etat = {}
        sauvegarder_etat(etat)

    # Initialiser les champs manquants
    for champ, valeur in [
        ("capital", CAPITAL_INITIAL),
        ("pnl_jour", 0.0),
        ("date_jour", ""),
        ("wins_consecutifs", 0),
        ("nb_skips", 0),
        ("nb_trades", 0),
        ("nb_wins", 0),
        ("nb_losses", 0),
        ("total_gagne", 0.0),
        ("total_perdu", 0.0),
        ("cumul_net", 0.0),
        ("pertes_consecutives", 0),
        ("historique", []),
        ("dernier_maj_marches", ""),
    ]:
        if champ not in etat:
            etat[champ] = valeur

    # Sauvegarde immédiate de l'état complet (avec les valeurs par défaut
    # tout juste peuplées) — sans ça, un redémarrage juste après un
    # RESET_TOUT rechargerait un état incomplet/vide depuis la base plutôt
    # que le pnl_jour à 0 fraîchement réinitialisé, et le kill switch
    # pourrait sembler ne pas s'être remis à zéro.
    sauvegarder_etat(etat)

    afficher_tableau_de_bord(etat)

    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector) as session:

        # ── Chargement initial des marchés x10 depuis l'API OKX
        log.info("  Chargement des marchés x10 depuis l'API OKX...")
        await charger_marches_x10(session)

        # Si aucun marché trouvé (panne API, réseau...), on réessaie quelques
        # fois avant d'abandonner — et surtout, on prévient TOUJOURS sur
        # Telegram plutôt que de s'arrêter en silence (bug précédent : un
        # simple `return` ici empêchait le moindre message de partir).
        tentatives = 0
        while not MARCHES and tentatives < 3:
            tentatives += 1
            log.warning(f"  Aucun marché x10 trouvé (tentative {tentatives}/3) — nouvel essai dans 10s...")
            await asyncio.sleep(10)
            await charger_marches_x10(session)

        if not MARCHES:
            log.error("  Aucun marché x10 trouvé après plusieurs tentatives")
            await telegram(session,
                "⚠️ <b>ALERTE DÉMARRAGE</b>\n"
                "Aucun marché x10 trouvé sur OKX après 3 tentatives.\n"
                "Le bot reste actif et réessaiera automatiquement à minuit Guyane, "
                "mais ne pourra pas trader tant que ce problème persiste.\n"
                "Vérifie les logs Railway pour plus de détails."
            )

        # En MODE_REEL, réduit MARCHES à ce que CE COMPTE peut vraiment
        # trader (le catalogue public ne suffit pas — voir le diagnostic
        # détaillé dans filtrer_marches_selon_compte). Sans effet en
        # simulation pure (MODE_REEL=0).
        await filtrer_marches_selon_compte(session)

        await telegram(session,
            (f"🔄 <b>RESET COMPLET EFFECTUÉ</b>\nCapital, PnL, compteurs et historique remis à zéro.\n\n" if RESET_TOUT else "")
            + f"🚀 <b>BOT DÉMARRÉ</b>\n"
            f"Capital : {round(etat['capital'],2)}€\n"
            f"Marchés x10 : {len(MARCHES)} cryptos | 24h/24 — 7j/7\n"
            + (f"{', '.join(MARCHES)}\n\n" if MARCHES else "\n")
            + f"Signal : mouvement >= {SEUIL_MOUVEMENT_PCT}%\n"
            f"Frais OKX : {OKX_TAKER_FEE*100:.2f}% ouv + {OKX_TAKER_FEE*100:.2f}% ferm (taker)\n"
            f"Kill switch : {KILL_SWITCH_JOUR}€/jour\n"
            f"Mode : {'REEL' if MODE_REEL else 'SIMULATION'}\n"
            + (f"Compte ordres : {'DÉMO (fictif)' if OKX_COMPTE_DEMO else '🚨 RÉEL — ARGENT VÉRITABLE 🚨'}\n" if MODE_REEL else "")
            + f"{(datetime.utcnow() - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # Lance le flux WebSocket temps réel en tâche de fond (reconnexion
        # auto en cas de coupure) — remplace le polling REST pour la
        # détection de signal, pour réagir aux mouvements de prix OKX en
        # temps réel plutôt que d'attendre le prochain appel REST.
        asyncio.create_task(websocket_prix(session))
        log.info("  ⏳ Attente des premiers ticks WebSocket (3s)...")
        await asyncio.sleep(3)

        while True:
            try:
                if reset_pnl_jour_si_nouveau_jour(etat):
                    sauvegarder_etat(etat)

                maintenant_utc = datetime.utcnow()

                # ── Mise à jour des marchés x10 chaque jour à minuit Guyane (3h UTC)
                if (maintenant_utc.hour == 3 and
                    maintenant_utc.minute < 1 and
                    etat.get("dernier_maj_marches", "") != maintenant_utc.strftime('%Y-%m-%d')):
                    log.info("  Minuit Guyane — mise à jour des marchés x10...")
                    await charger_marches_x10(session)
                    await filtrer_marches_selon_compte(session)
                    etat["dernier_maj_marches"] = maintenant_utc.strftime('%Y-%m-%d')
                    sauvegarder_etat(etat)
                    await telegram(session,
                        f"🔄 <b>Marchés x10 mis à jour</b>\n"
                        f"{len(MARCHES)} marchés actifs : {', '.join(MARCHES)}"
                    )
                    # Force une reconnexion WebSocket pour resynchroniser
                    # l'abonnement avec la liste de marchés à jour
                    if WS_CONNEXION_ACTIVE is not None and not WS_CONNEXION_ACTIVE.closed:
                        log.info("  🔌 Resynchronisation WebSocket forcée (marchés mis à jour)")
                        try:
                            await WS_CONNEXION_ACTIVE.close()
                        except Exception as e:
                            log.error(f"Erreur lors de la resynchronisation WebSocket : {e}")

                # Rapport quotidien à 19h Guyane = 22h UTC
                if (maintenant_utc.hour == 22 and
                    maintenant_utc.minute < 1 and
                    etat.get("dernier_rapport_quotidien", "") != maintenant_utc.strftime('%Y-%m-%d')):
                    await envoyer_rapport_quotidien(session, etat)
                    etat["dernier_rapport_quotidien"] = maintenant_utc.strftime('%Y-%m-%d')
                    sauvegarder_etat(etat)

                # Rapport hebdomadaire dimanche à 22h UTC
                if (maintenant_utc.weekday() == 6 and
                    maintenant_utc.hour == 22 and
                    maintenant_utc.minute < 1 and
                    etat.get("derniere_semaine", "") != maintenant_utc.strftime('%Y-%W')):
                    await envoyer_rapport_hebdomadaire(session, etat)
                    etat["derniere_semaine"] = maintenant_utc.strftime('%Y-%W')
                    sauvegarder_etat(etat)

                # Vérification protections
                statut = verifier_protections(etat, etat["capital"])
                if statut == "RUINE":
                    await telegram(session,
                        f"🚨 <b>SEUIL RUINE !</b>\nCapital : {etat['capital']}€\nBot arrêté !")
                    break
                if statut == "KILL_SWITCH":
                    await asyncio.sleep(60)
                    etat = charger_etat()
                    continue

                # Scan des marchés disponibles
                async with trades_lock:
                    slots_libres        = MAX_TRADES_SIMULTANES - len(trades_ouverts)
                    marches_actifs      = get_marches_actifs()
                    marches_disponibles = [
                        m for m in marches_actifs
                        if m not in trades_ouverts
                        and time.time() >= cooldown_marches.get(m, 0)
                    ]

                if slots_libres <= 0:
                    log.info(f"  {MAX_TRADES_SIMULTANES}/{MAX_TRADES_SIMULTANES} trades — attente...")
                    await asyncio.sleep(PAUSE_SCAN)
                    continue

                log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan "
                         f"| Slots : {slots_libres}/{MAX_TRADES_SIMULTANES} "
                         f"| Marchés x10 dispo : {len(marches_disponibles)}")

                signaux = {}
                for marche in marches_disponibles:
                    direction, details = await analyser_marche(session, marche)
                    if direction != "NEUTRE":
                        signaux[marche] = {"direction": direction, "details": details}
                    await asyncio.sleep(0.3)

                if not signaux:
                    log.info("  => Aucun signal.")
                    etat["nb_skips"] = etat.get("nb_skips", 0) + 1
                    sauvegarder_etat(etat)
                    await asyncio.sleep(PAUSE_SCAN)
                    continue

                # Trier par variation la plus forte
                meilleurs = sorted(
                    signaux.items(),
                    key=lambda x: x[1]["details"].get("variation_pct", 0),
                    reverse=True
                )[:slots_libres]

                for symbole, sig in meilleurs:
                    async with trades_lock:
                        if symbole in trades_ouverts:
                            continue
                        if len(trades_ouverts) >= MAX_TRADES_SIMULTANES:
                            break
                        trades_ouverts[symbole] = True

                    log.info(f"  {symbole} ({sig['direction']}) "
                             f"Variation={sig['details'].get('variation_pct', 0):.2f}%")

                    asyncio.create_task(
                        executer_trade(
                            session, symbole, sig["direction"],
                            etat["capital"],
                            sig["details"], etat
                        )
                    )

                await asyncio.sleep(PAUSE_SCAN)

            except KeyboardInterrupt:
                log.info("Bot arrêté.")
                break
            except Exception as e:
                log.error(f"Erreur inattendue : {e}")
                await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(boucle_principale())
