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
import random
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Miroir automatique des erreurs vers Telegram (08/07, demandé par
# Damien) : au lieu de devoir aller chercher une info dans les logs Railway
# à chaque fois qu'il y a un souci à diagnostiquer, TOUTE ligne de log de
# niveau ERROR ou CRITICAL — où qu'elle soit dans le fichier, présente ou
# future — est automatiquement mise en file d'attente ici, puis regroupée
# et envoyée sur Telegram par la boucle principale (voir
# vider_file_erreurs_vers_telegram) toutes les 60s. Choix du niveau ERROR
# (pas WARNING) : les WARNING sont souvent des cas déjà gérés/informatifs
# (repli normal, déjà annoncés ailleurs sur Telegram) — les envoyer aussi
# aurait noyé le chat. Un ERROR, lui, signale systématiquement quelque
# chose qui mérite l'attention de Damien.
FILE_ERREURS_TELEGRAM = []

class HandlerErreursTelegram(logging.Handler):
    """Handler de logging synchrone (donc utilisable partout, y compris hors
    contexte async) qui se contente d'empiler le message formaté — l'envoi
    réel vers Telegram (async, nécessite une session aiohttp) est fait à
    part par la boucle principale, jamais ici directement."""
    def emit(self, record):
        try:
            FILE_ERREURS_TELEGRAM.append(self.format(record))
        except Exception:
            pass  # ne jamais faire planter le logging lui-même

_handler_erreurs = HandlerErreursTelegram()
_handler_erreurs.setLevel(logging.ERROR)
_handler_erreurs.setFormatter(logging.Formatter('%(asctime)s - %(message)s', datefmt='%H:%M:%S'))
log.addHandler(_handler_erreurs)

# ═══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════
CAPITAL_INITIAL         = 543.65  # en USDC — capital réel actuel du compte de trading
SEUIL_ALERTE_PERTE_PCT  = 10.0  # alerte Telegram dès que le capital descend de ce % sous CAPITAL_INITIAL
LEVIER                  = 10

# ── Mise calculée pour avoir des frais totaux de ~0.50€ avec les vrais frais OKX
# Frais réels OKX (taker, palier standard) = 0.05% ouverture + 0.05% fermeture = 0.10% total
# Pour frais = 0.50€ → position = 0.50 / 0.0010 = 500€ → mise = 500 / 10 = 50€
# Soit 50 / 500 = 10% du capital
MISE_BASE_PCT           = 0.10    # 10% du capital → mise ~50€ → position ~500€ → frais ~0.50€
MISE_MIN                = 10.0    # mise minimum cohérente avec l'objectif frais 0.50€
MISE_MAX_PCT            = 0.12    # plafond légèrement au-dessus pour le boost confiance
CHECK_INTERVAL          = 1          # secondes entre chaque check prix — réduit de 3 à 1 le 07/07 (12:50) pour détecter les franchissements de palier plus vite ; sans surcoût API significatif, le cache WebSocket (SEUIL_FRAICHEUR_PRIX_SEC=2s) reste utilisé la plupart du temps
INTERVALLE_STATUT_TELEGRAM_SEC = 900  # 08/07 — statut de position envoyé sur Telegram
                                       # toutes les 15 min (au lieu du log toutes les
                                       # 1 min, gardé en interne) : assez fréquent pour
                                       # suivre un trade sans ouvrir Railway, assez
                                       # espacé pour ne pas spammer avec plusieurs
                                       # positions ouvertes en simultané.
INTERVALLE_CHECK_UPL_SEC = 3   # 07/07 (13:15) — la vérification du vrai PnL OKX (upl) ne
                                # peut PAS suivre le même rythme que CHECK_INTERVAL=1s : la
                                # doc officielle OKX confirme /account/positions limitée à
                                # 10 requêtes/2s PAR COMPTE (pas par instrument). Même réduit
                                # à MAX_TRADES_SIMULTANES=4 (voir plus bas), un check upl à
                                # chaque tick de 1s resterait risqué en cas de sursaut ; 3s
                                # laisse une bonne marge (4 trades / 3s ≈ 1.3 req/s, largement
                                # sous la limite).
PAUSE_SCAN              = 30         # secondes entre chaque scan de nouveaux marchés
# ── RÉDUIT de 10 à 4 le 08/07 (dernière chance accordée par Damien après une
# première journée réelle difficile, -3,3%) : la session du jour a montré
# jusqu'à 6 trades ouverts EN MÊME TEMPS, très majoritairement des VENTES sur
# des cryptos différentes — c'est-à-dire une diversification en apparence,
# mais en réalité une exposition corrélée (les cryptos bougent souvent
# ensemble). Un vrai mouvement de marché défavorable touche alors plusieurs
# positions à la fois, comme observé (ADAUSD, XRPUSD, AAVEUSD tous perdants
# le même après-midi, dépassant chacun leur stop max prévu). Réduire le
# nombre de trades simultanés réduit directement l'ampleur d'un tel épisode
# sur le capital réel, au prix d'un volume de trades plus faible.
# RELEVÉ à 10 le 09/07, puis RETIRÉ (mis à 999 = aucune limite pratique) le
# 10/07 (demandé par Damien) : phase d'analyse pure — aucune limite ne doit
# bloquer la collecte de données. À REMETTRE à 4 une fois l'analyse
# terminée et avant tout retour en argent réel.
MAX_TRADES_SIMULTANES   = 999
# ── NOUVEAU (08/07, carte blanche accordée par Damien après la première
# journée réelle) : un compteur de pertes consécutives existait déjà
# (pertes_consecutives) mais n'était utilisé nulle part pour agir — juste
# affiché dans les logs. Ajout d'une vraie pause automatique : après
# plusieurs pertes d'affilée, le bot arrête d'OUVRIR de nouveaux trades
# pendant un temps donné (les trades déjà ouverts continuent d'être
# surveillés normalement, stops/planchers actifs comme toujours). Objectif :
# éviter d'insister avec la même stratégie pendant un épisode de marché qui
# lui est défavorable (exactement le schéma du 08/07 : plusieurs pertes
# d'affilée sur des marchés corrélés, tous perdants dans la même fenêtre).
# DÉSACTIVÉE le 10/07 (demandé par Damien, phase d'analyse) — seuil énorme
# pour ne jamais se déclencher en pratique, sans retirer le mécanisme (facile
# à réactiver avec un vrai seuil plus tard). À REMETTRE à ~3 avant tout
# retour en argent réel.
SEUIL_PERTES_CONSECUTIVES_PAUSE = 999999
DUREE_PAUSE_APRES_PERTES_MIN    = 45   # minutes de pause avant de reprendre le scan

# ── Fenêtre anti-funding (08/07, carte blanche) : les frais de financement
# des X-Perps sont prélevés aux horaires de règlement standards des
# perpétuels (00h/08h/16h UTC). Confirmé en conditions réelles le 08/07 :
# un trade ETHUSD de seulement 53 minutes, ouvert juste avant 16h UTC, a
# payé -2,8629 USDC de funding — soit ~2x le gain du premier palier, un
# coût invisible qui à lui seul transforme un petit gagnant en perdant.
# Règle : aucune NOUVELLE ouverture dans les X minutes précédant un horaire
# de règlement (les positions déjà ouvertes ne sont pas touchées — les
# fermer de force coûterait des frais certains pour éviter un funding
# incertain, mauvais échange).
FENETRE_PRE_FUNDING_MIN = 20   # minutes avant 00h/08h/16h UTC sans nouvelle ouverture

def dans_fenetre_pre_funding():
    """True si on est à moins de FENETRE_PRE_FUNDING_MIN minutes du prochain
    horaire de règlement du funding (00h/08h/16h UTC)."""
    maintenant = datetime.utcnow()
    prochains = []
    for h in (0, 8, 16):
        t = maintenant.replace(hour=h, minute=0, second=0, microsecond=0)
        if t <= maintenant:
            t += timedelta(days=1)
        prochains.append(t)
    prochain = min(prochains)
    return (prochain - maintenant).total_seconds() <= FENETRE_PRE_FUNDING_MIN * 60
TRAIL_RATIO_POST_PALIER1 = 0.002   # 08/07 (05:34) — ajusté à 0.20% suite au retrait du
                                     # palier 0.20% (retour à 28 paliers, premier à 0.30%).
                                     # Historique : 0.30% d'origine jugé trop large (give-back
                                     # de 70% sur un petit pic de 2.81€) -> resserré à 0.10% ->
                                     # repassé à 0.20% ici, sur demande explicite.

# ── Détection signal mean reversion — surveillance temps réel
SEUIL_MOUVEMENT_PCT     = 0.50   # dès que le prix bouge de 0.50% → signal
# ── PLAFOND de mouvement (11/07) — borne HAUTE, symétrique du seuil de
# déclenchement ci-dessus. Jusqu'ici le signal partait dès |variation| >= 0.50%
# SANS AUCUN plafond : un mouvement de 1.8% déclenchait exactement comme un
# mouvement de 0.5%. C'est un contresens pour du mean reversion, qui parie sur
# une SUR-réaction qui se corrige : un mouvement très large est bien plus
# souvent une vraie repriçage / un début de tendance (news, liquidation) qu'une
# simple sur-réaction — parier contre revient à se battre contre le momentum, et
# à x10 la perte + le slippage sont lourds. Choix de PRINCIPE, pas de calage sur
# l'échantillon : fixé à ~2.4x le seuil de déclenchement (au-delà, ce n'est plus
# un "petit repli" mais un vrai mouvement), pas au chiffre qui optimise les
# trades déjà vus. À revalider sur des trades NEUFS avant de resserrer.
SEUIL_MOUVEMENT_MAX_PCT = 1.20   # au-delà : mouvement trop violent → pas d'entrée
# ── ÉLARGI le 09/07 (phase de collecte de données, demandé par Damien) : de
# 0.25x à 0.20x. Objectif explicite : laisser entrer PLUS de trades pour
# accumuler une base statistique sur RSI/volume (déjà enregistrés par trade
# dans le rapport quotidien) — les seuils larges d'aujourd'hui contiennent
# toujours les seuils serrés de demain (on peut filtrer après coup sur les
# données collectées), l'inverse n'est pas vrai. Resserrer seulement après
# validation sur un ÉCHANTILLON JAMAIS VU (pas les mêmes trades qui ont
# servi à choisir le seuil — sinon on cale le bot sur le bruit du moment,
# pas sur un vrai signal durable).
VOLUME_MINI             = 0.20   # volume min vs moyenne 24h
# ── ÉLARGI de 0.60% à 0.75% le 09/07 — analyse du suivi post-stop sur un lot
# de 8 stops en simulation : 6/8 sont repartis en sens favorable dans les 15
# minutes suivant la fermeture (voir suivre_prix_post_stop). Élargissement
# MODÉRÉ (pas radical) pour tester l'effet sur le taux de "stop bien placé"
# et le résultat net des PROCHAINS trades — pas une certitude, une hypothèse
# à valider sur de nouvelles données (les 8 trades déjà vus ne peuvent pas
# servir à eux-mêmes de preuve, voir discussion du 09/07).
STOP_LOSS_PCT           = 0.0075  # stop = 0.75% du prix d'entrée — évolue avec la taille de position, contrairement à un stop fixe en €
# ── BREAKEVEN ANTICIPÉ (11/07, demandé par Damien) — neutralise le RISQUE plus
# tôt que le palier 1. Analyse des trades : les grosses pertes ne sont PAS des
# gains coupés trop court, ce sont des trades partis à contresens dès l'entrée
# qui mangent tout le stop (-0.75%) AVANT d'atteindre +0.28% (palier 1) — seul
# moment où, jusqu'ici, le stop passait au breakeven. Dès que le trade a montré
# ne serait-ce que +0.15% dans le bon sens, on remonte le stop au prix d'entrée
# (+ tampon frais) : un trade qui monte un peu puis échoue ressort à ~0€ au lieu
# de -5€. Ne touche PAS la largeur du stop initial (donc ne contredit pas le
# suivi post-stop qui avait fait élargir à 0.75%), et ne sacrifie AUCUN gain (les
# trades qui atteignent +0.28% capturent exactement pareil). À valider sur des
# trades NEUFS en démo avant d'en tirer une conclusion.
SEUIL_BREAKEVEN_ANTICIPE_PCT = 0.0015  # +0.15% du prix d'entrée → stop remonté au breakeven

# ── Glissement SIMULÉ en mode SIMULATION (10/07, demandé par Damien) —
# jusqu'ici, en simulation, prix_entree était EXACTEMENT le prix visé, sans
# aucun frottement : irréaliste, très différent du réel. Calibré sur les
# glissements RÉELLEMENT observés en compte réel cette semaine (typiquement
# -0.05% à -0.35%, avec de rares accidents plus sévères lors de mouvements
# très rapides, jusqu'à -1.7% voire -2.8%). Toujours défavorable, comme un
# vrai ordre au marché qui mange le carnet plutôt que d'aider. Objectif :
# que la phase d'analyse en simulation prépare vraiment à ce qui attend en
# réel, plutôt que de donner une image trop optimiste des gains nets.
GLISSEMENT_SIMULE_MOYEN_PCT    = 0.15   # glissement typique (valeur absolue)
GLISSEMENT_SIMULE_ECART_TYPE   = 0.12
GLISSEMENT_SIMULE_PROBA_ACCIDENT = 0.05  # ~5% de chance d'un glissement sévère
GLISSEMENT_SIMULE_ACCIDENT_MOYEN = 1.8
GLISSEMENT_SIMULE_ACCIDENT_ECART = 0.8
GLISSEMENT_SIMULE_MAX_PCT      = 2.8    # plafond = pire cas RÉELLEMENT observé (avant : 4.0, irréaliste)
DUREE_MAX_MINUTES       = 360    # 6h — fermeture forcée si ni stop ni lock atteint avant
# ── Suivi post-stop (09/07, demandé par Damien) : après un stop-loss, on
# continue de suivre le prix pendant DUREE_SUIVI_POST_STOP_MIN de plus (sans
# aucune position ouverte, juste en observation) pour savoir si le marché a
# continué dans le sens du stop (bien calibré) ou est reparti en sens
# inverse (stop trop serré) — donnée concrète pour ajuster STOP_LOSS_PCT
# lors des prochaines mises à jour, plutôt que de deviner.
DUREE_SUIVI_POST_STOP_MIN = 15
TOLERANCE_LOCK_UPL_EUR  = 0.10   # tolérance sur la vérification du PnL réel OKX avant une
                                  # sortie LOCK — absorbe le bruit de sync normal entre le
                                  # tick WebSocket et l'API positions (quelques ms d'écart),
                                  # bien en-deçà des frais type d'un trade (~0.54€). Ne bloque
                                  # que les écarts réellement significatifs (le problème
                                  # observé était de l'ordre de -14€ à -27€, pas de -0.05€).

# ── Filtre RSI 1h — ÉLARGI le 09/07 (même raison que VOLUME_MINI ci-dessus) :
# de 45/55 à 40/60, pour laisser entrer plus de signaux et bâtir la base de
# données RSI/volume avant de resserrer sur un seuil validé.
RSI_SEUIL_BAS           = 40     # RSI < 40 → marché baissier → inverser ACHAT en VENTE
RSI_SEUIL_HAUT          = 60     # RSI > 60 → marché haussier → inverser VENTE en ACHAT
RSI_PERIODE             = 14

# ── Protections
# ── KILL SWITCH — refondu le 08/07 (carte blanche accordée par Damien) :
# l'ancien seuil FIXE de -100€/jour représentait ~19% du capital actuel
# (~530€) — un niveau de perte quotidienne qu'aucun gestionnaire de risque
# professionnel n'accepterait avant de couper. Les standards du métier se
# situent entre -2% et -5% par jour. Le seuil devient PROPORTIONNEL au
# capital : -4% du capital du jour (≈ -21€ actuellement), avec l'ancien
# -100€ conservé uniquement comme plafond absolu de sécurité si le capital
# grossissait beaucoup. La journée du 08/07 (-13€, -2.4%) serait passée
# JUSTE sous ce nouveau seuil — c'est voulu : une journée comme celle-là
# doit pouvoir se produire sans tout couper, mais pas beaucoup pire.
KILL_SWITCH_PCT         = 0.04     # perte max par jour en % du capital
KILL_SWITCH_JOUR        = -100.0   # plafond absolu (ne sert que si capital > 2500€)

def seuil_kill_switch(capital):
    """Seuil de perte quotidienne déclenchant l'arrêt : -4% du capital,
    borné par le plafond absolu KILL_SWITCH_JOUR."""
    return max(-abs(capital) * KILL_SWITCH_PCT, KILL_SWITCH_JOUR)
SEUIL_RUINE             = 300.0
SEUIL_CAPITAL_BTC       = 6000.0  # capital mini pour que BTCUSD soit inclus dans le scan — sous ce seuil, 1 seul contrat BTC (ctVal=1 ≈ 1 BTC) coûte plus cher que toute la position ; BTC est retiré des marchés actifs jusqu'à ce que le capital dépasse ce seuil

# ── Lock profits par paliers proportionnels au capital
# Recalibré le 07/07 (11:54) : les deux plus bas paliers (0.16%, 0.20%)
# avaient été retirés après analyse de TOUTES les sorties LOCK de la
# soirée — le bot surestimait systématiquement le gain net, à cause du
# coût réel d'un ordre au marché à la fermeture (spread) qui s'ajoute aux
# frais. Écarts observés à l'époque : 0.13€ à 0.54€ selon les trades.
# RÉTABLI le 08/07 à la demande explicite de Damien à 0.20%, puis RELEVÉ À
# 0.28% le même jour (dernière chance accordée par Damien, "fais comme tu le
# sens") après confirmation en conditions RÉELLES sur la première journée de
# trading réel : plusieurs trades verrouillés autour de 1,06-1,09€ ne sont
# ressortis qu'à 0,20-0,56€ net après frais — exactement le risque de marge
# quasi nulle identifié en théorie, désormais confirmé par des trades
# réels. 0.28% (~1,48€ sur 530€) laisse une marge nette raisonnable même
# dans le pire cas observé, sans revenir aux 0.30% d'avant la demande de
# Damien. Palier 0.36% ajouté le 07/07 (12:50), conservé.
LOCK_PALIERS_PCT = [
    0.28, 0.36, 0.40, 0.50, 0.65, 0.80, 1.00, 1.20, 1.50,
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

def get_palier_lock_index(pnl_max, capital):
    """Comme get_palier_lock, mais retourne aussi l'INDEX (1-based) du palier le plus
    haut atteint dans LOCK_PALIERS_PCT — nécessaire pour le mécanisme de 'plancher dur'
    (07/07, 23:11) : un palier sur deux (index pair) repositionne un stop fixe
    indépendant qui ne redescend plus jamais, en plus du trailing natif qui continue
    normalement au-dessus. Retourne (0.0, 0) si aucun palier atteint."""
    lock  = 0.0
    index = 0
    for i, pct in enumerate(LOCK_PALIERS_PCT, 1):
        palier_eur = round(capital * pct / 100, 2)
        if pnl_max >= palier_eur:
            lock  = palier_eur
            index = i
    return lock, index

def palier_pose_plancher_dur(index_lock):
    """Détermine si le palier d'index donné (1-based) doit poser/repositionner le
    plancher dur. MODIFIÉ le 08/07 à la demande explicite de Damien, suite à un
    trade HYPEUSD où le prix a grimpé jusqu'à +5,82€ de PnL max mais où seul le
    palier 7 (5,45€) avait un plancher posé — le trade est ressorti à +3,99€ net
    réel, en dessous de ce qu'un plancher posé à CHAQUE palier franchi aurait pu
    verrouiller de plus près. Désormais TOUS les paliers posent/repositionnent le
    plancher dur, sans exception — plus d'alternance ni de paliers "d'office"."""
    return index_lock >= 1

# ── Gestion mise dynamique
# ── BOOST DÉSACTIVÉ (12/07) — le boost gonflait la mise après 3 gains d'affilée,
# donc le bot pariait PLUS GROS juste après une bonne série… et un gap tombait
# pile sur cette mise gonflée (ex : LINKUSD -17,29€ sur une position à 641€ au
# lieu de 538€). Comme les gains sont petits et les gaps énormes, sur-miser après
# des gains amplifie surtout la casse. Mis à 999999 = ne se déclenche jamais.
# Remettre à 3 pour réactiver le boost.
WINS_CONFIANCE          = 999999
BOOST_CONFIANCE         = 1.20

# ── PAUSE AUTOMATIQUE DES MARCHÉS QUI GAPPENT (12/07) — le bot surveille lui-même
# les gaps par marché sur une fenêtre glissante et met en pause tout seul ceux qui
# déraillent (puis les réactive s'ils se calment). C'est la donnée qui décide en
# continu, pas une liste figée. Un "gap" = un trade fermé au stop dont la perte a
# dépassé le stop prévu d'un facteur (le prix a sauté À TRAVERS le stop). Analyse
# sur 5 jours : 28 gaps = -211€, tout le reste = +70€ → les gaps SONT le problème,
# et ils viennent de quelques marchés récidivistes (HYPE, ADA, INJ, LINK).
GAP_FENETRE_JOURS       = 7       # fenêtre glissante d'observation des gaps
GAP_FACTEUR_STOP        = 1.4     # perte > 1.4x le stop attendu = gap (a sauté à travers)
GAP_NB_POUR_PAUSE       = 2       # ce nombre de gaps dans la fenêtre → marché en pause
GAP_PERTE_CUMULEE_PAUSE = -12.0   # OU cette perte cumulée de gaps (€) → pause (capte un seul gap énorme)

# ── Frais OKX réels (X-Perps, palier standard/non-VIP — identiques aux Swaps Perpétuels classiques)
# Maker 0.02% / Taker 0.05% du notionnel — le bot sort au marché à l'ouverture
# ET à la fermeture, donc taker des deux côtés. CORRECTIF (08/07) — le
# commentaire précédent affirmait à tort qu'OKX ne facture PAS de frais de
# financement séparé sur ce produit : FAUX, confirmé en conditions réelles
# (trade ETHUSD du 08/07 16h UTC : -2,8629 USDC de funding fee à lui seul,
# plus de 5x le coût normal d'ouverture+fermeture). Ce frais tombe aux
# horaires de règlement habituels des perpétuels (généralement 00h/08h/16h
# UTC) si une position reste ouverte à ce moment précis, même brièvement.
# Non modélisé/anticipé dans le calcul interne du bot (STOP_LOSS_PCT,
# paliers...) — mais correctement intégré dans le résultat OFFICIEL du
# trade via la vérification posId (voir okx_recuperer_position_reelle),
# qui lit le champ fundingFee séparément et l'inclut toujours dans
# net_reel avant d'écraser l'estimation interne.
OKX_TAKER_FEE            = 0.0005  # 0.05% par exécution (ouverture OU fermeture)

# ── Marge anti-slippage sur les stops natifs (08/07, demandé par Damien) —
# un stop "au marché" (slOrdPx=-1) garantit la fermeture mais PAS le prix :
# lors d'un mouvement brutal (confirmé en conditions réelles : perte de
# -21,59 USDC pour un stop prévu à -3,24€), le prix d'exécution réel peut
# être largement pire que le niveau de déclenchement. En posant plutôt un
# prix LIMITE légèrement au-delà du déclenchement (recommandation officielle
# OKX : "set the order price not too close to the trigger price to ensure
# the order will be filled promptly"), on plafonne la perte maximale
# possible au lieu de la laisser ouverte. Contrepartie assumée : dans un gap
# extrême qui saute par-dessus ce prix limite lui-même, l'ordre pourrait ne
# pas se remplir immédiatement — la surveillance interne du bot (boucle de
# CHECK_INTERVAL) reste alors le filet de secours pour fermer au marché.
# ── ÉLARGI de 0.15% à 0.25% le 08/07 (dernière chance accordée par Damien,
# "fais comme tu le sens") : la première journée réelle a montré 3 stops
# (ADAUSD, AAVEUSD, HYPEUSD) dépassant leur "stop max" annoncé de 0,28€ à
# 0,73€ — signe qu'une marge de 0,15% était par moments trop juste pour que
# l'ordre limite se remplisse immédiatement lors d'un mouvement rapide,
# repoussant la fermeture réelle plus loin que prévu. Élargir la marge
# augmente la fiabilité de remplissage immédiat, au prix d'un pire cas
# théorique très légèrement plus large mais bien plus rarement atteint.
STOP_BUFFER_SLIPPAGE_PCT = 0.0025  # 0.25% au-delà du niveau de déclenchement

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

# ── EXPÉRIENCE : inversion du sens (12/07, demandé par Damien) — piloté depuis
# Railway (Variables → INVERSER_SENS = 1, puis Deploy). Teste l'hypothèse « le
# bot trade à l'envers » : quand actif, CHAQUE signal est pris dans le sens
# OPPOSÉ à ce que la stratégie déciderait normalement (ACHAT <-> VENTE), au tout
# dernier moment de la décision, SANS rien changer d'autre au code. Défaut = 0
# (comportement normal). Mettre à 1 pour tester, à 0 pour revenir à la normale —
# sans recoder. À comparer sur plusieurs jours dans les deux positions.
INVERSER_SENS = os.environ.get('INVERSER_SENS', '0').strip().lower() in ('1', 'true', 'oui', 'yes')

def _sens_effectif(direction):
    """Renvoie le sens réellement pris : inversé si INVERSER_SENS est actif,
    sinon inchangé. N'inverse jamais NEUTRE (pas de trade)."""
    if not INVERSER_SENS or direction == "NEUTRE":
        return direction
    return "VENTE" if direction == "ACHAT" else "ACHAT"

def _gaps_recents(symbole, etat):
    """Renvoie (nb_gaps, perte_cumulée) des gaps du marché encore DANS la fenêtre
    glissante. Élague au passage les gaps trop vieux pour garder l'état léger."""
    tous = etat.get("gaps_par_marche", {}).get(symbole, [])
    limite = time.time() - GAP_FENETRE_JOURS * 86400
    recents = [g for g in tous if g.get("ts", 0) >= limite]
    if len(recents) != len(tous):                       # purge des gaps périmés
        etat.setdefault("gaps_par_marche", {})[symbole] = recents
    nb = len(recents)
    perte = round(sum(g.get("perte", 0.0) for g in recents), 2)
    return nb, perte

def _marche_en_pause_gap(symbole, etat):
    """True si le marché a trop gappé récemment (trop de gaps OU une perte de gaps
    cumulée trop lourde dans la fenêtre). Purement piloté par les données : dès que
    les gaps sortent de la fenêtre, le marché se réactive tout seul."""
    nb, perte = _gaps_recents(symbole, etat)
    return nb >= GAP_NB_POUR_PAUSE or perte <= GAP_PERTE_CUMULEE_PAUSE

def _enregistrer_gap_si_besoin(symbole, gain_final, motif_sortie, position, etat):
    """À la fermeture d'un trade : si c'est un gap (fermé au stop, perte au-delà du
    stop attendu par le facteur), l'enregistre dans l'état. Renvoie True si ce gap
    vient JUSTE de faire basculer le marché en pause (pour notifier une seule fois)."""
    if motif_sortie not in ("STOP_NATIF", "STOP_INTERNE"):
        return False
    perte_stop_attendue = position * STOP_LOSS_PCT               # ~ perte prix du stop, en €
    if gain_final >= -perte_stop_attendue * GAP_FACTEUR_STOP:    # pas assez au-delà du stop → pas un gap
        return False
    etait_en_pause = _marche_en_pause_gap(symbole, etat)
    etat.setdefault("gaps_par_marche", {}).setdefault(symbole, []).append(
        {"ts": time.time(), "perte": round(gain_final, 2)}
    )
    return (not etait_en_pause) and _marche_en_pause_gap(symbole, etat)

# ── Marchés — uniquement ceux à levier x10 sur OKX (X-Perps, compte France/EEA)
# Chargés dynamiquement via API au démarrage et mis à jour chaque nuit à minuit
MARCHES          = []   # liste des symboles actifs (levier x10 uniquement)
OKX_SYMBOLS      = {}   # { "BTCUSD": "BTC-USD-YYMMDD", ... } — instId PUBLIC (www.okx.com), utilisé pour les prix (WebSocket + REST) — NE PAS écraser avec l'instId du compte
OKX_SYMBOLS_EXEC = {}   # { "BTCUSD": "BTC-USD-YYMMDD", ... } — instId scopé au COMPTE (démo ou réel), utilisé uniquement pour passer/fermer un ordre
OKX_CT_VAL       = {}   # { "BTCUSD": 0.01, ... } — valeur d'un contrat (usage réel uniquement)

# ── Alerte anticipée de rollover X-Perp (08/07) : chaque X-Perp a une date
# d'expiration ferme (~5 ans, champ expTime), à laquelle OKX génère un
# NOUVEAU contrat (nouvel instId) pour le même actif — confirmé via la
# doc officielle OKX ("On the first day of the expiry month, a new
# far-dated contract is automatically generated"). C'est très probablement
# la cause structurelle des incohérences instId feed/exécution observées
# depuis plusieurs semaines. On ne peut pas empêcher ce rollover (il est
# côté OKX), mais on peut le voir venir et prévenir Damien À L'AVANCE
# plutôt que de le découvrir en cours de trade.
SEUIL_ALERTE_ROLLOVER_JOURS = 10  # avertir dès que l'expiration est à moins de X jours
ROLLOVER_ALERTES_ENVOYEES   = set()  # instId déjà signalés cette session — pas de spam quotidien

# ═══════════════════════════════════════════════════════════════
#  ÉTAT GLOBAL
# ═══════════════════════════════════════════════════════════════
trades_ouverts    = {}    # { symbole: True }
prix_reference    = {}    # { symbole: prix_au_moment_du_scan }
cooldown_marches  = {}    # { symbole: timestamp_fin_cooldown }
trades_lock       = None  # initialisé dans boucle_principale()
taches_trades_actives = set()  # tâches asyncio en cours (executer_trade) — pour un arrêt propre (SIGTERM)
arret_demande     = False  # passe à True sur SIGTERM/SIGINT — arrête d'ouvrir de NOUVEAUX trades, sans tuer ceux en cours
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
log.info(f"  Kill switch : -{KILL_SWITCH_PCT*100:.0f}%/jour du capital (plafond {KILL_SWITCH_JOUR}€) | Ruine : {SEUIL_RUINE}€")
log.info(f"  Durée max par trade : {DUREE_MAX_MINUTES//60}h — fermeture forcée si ni stop ni lock atteint avant")
log.info(f"  Telegram : {'ON' if TELEGRAM_TOKEN else 'OFF'}")
log.info(f"  Mode : {'REEL' if MODE_REEL else 'SIMULATION'}")
if MODE_REEL:
    log.warning(f"  ⚠️ Compte ciblé pour les ordres : {'DÉMO (argent fictif)' if OKX_COMPTE_DEMO else '🚨 RÉEL — ARGENT VÉRITABLE 🚨'}")
if INVERSER_SENS:
    log.warning("  🔄 SENS INVERSÉ ACTIF — chaque trade est pris à L'ENVERS du signal normal (expérience). Mettre INVERSER_SENS=0 pour revenir à la normale.")
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

        # ── Snapshot de l'ancien mapping AVANT écrasement (08/07) — nécessaire
        # pour détecter un changement d'instId (= rollover survenu) et le
        # distinguer d'une simple première résolution.
        ancien_exec = dict(OKX_SYMBOLS_EXEC)

        avant                = set(MARCHES)
        candidats            = [m for m in avant if m[:-3] in inst_par_base]
        nouveaux_marches     = []
        nouveaux_ct_val_exec = {}

        for m in candidats:
            inst_exec    = inst_par_base[m[:-3]]
            inst_id_exec = inst_exec.get("instId")
            ct_val_public = OKX_CT_VAL.get(m)
            try:
                ct_val_exec = float(inst_exec.get("ctVal", 0) or 0)
            except (TypeError, ValueError):
                ct_val_exec = 0.0

            # ── GARDE-FOU (08/07) — "le bot s'adapte automatiquement au nouveau
            # contrat SI il est conforme, sinon il bloque le marché" (demandé
            # explicitement par Damien, suite à l'incident BTCUSD ctVal=0.0001
            # vs 1, facteur 10000, qui avait causé un surdimensionnement de
            # position). Conditions minimales de conformité avant d'accepter
            # ce contrat pour trader dessus : ctVal exploitable (>0), devise de
            # règlement et de contrat renseignées, et état "live" si le champ
            # est présent (pas "expired"/"suspend"). Si une seule de ces
            # conditions échoue, le marché est EXCLU de MARCHES (donc plus
            # aucun trade dessus) plutôt que de deviner ou de tenter avec des
            # valeurs invalides.
            conforme  = True
            problemes = []
            if ct_val_exec <= 0:
                conforme = False
                problemes.append(f"ctVal invalide ({inst_exec.get('ctVal')!r})")
            if not inst_exec.get("ctValCcy"):
                conforme = False
                problemes.append("ctValCcy manquant")
            if not inst_exec.get("settleCcy"):
                conforme = False
                problemes.append("settleCcy manquant")
            etat_inst = inst_exec.get("state")
            if etat_inst and etat_inst != "live":
                conforme = False
                problemes.append(f"state={etat_inst} (attendu 'live')")

            if not conforme:
                log.error(f"  🚫 [MARCHÉ BLOQUÉ] {m} : contrat {inst_id_exec} non conforme — "
                          f"{', '.join(problemes)}. Exclu de MARCHES jusqu'au prochain "
                          f"rafraîchissement.")
                await telegram(session,
                    f"🚫 <b>MARCHÉ BLOQUÉ : {m}</b>\n"
                    f"Le contrat {inst_id_exec} ne respecte pas les critères de conformité "
                    f"({', '.join(problemes)}).\n"
                    f"Aucun trade ne sera ouvert sur {m} tant que ce n'est pas résolu côté "
                    f"OKX — re-vérifié automatiquement à chaque rafraîchissement des marchés."
                )
                continue  # ne PAS inclure ce marché — jamais deviner avec des valeurs douteuses

            # ── Contrat conforme : on l'adopte. Si l'instId a changé depuis la
            # dernière fois (rollover survenu), le bot bascule automatiquement
            # dessus et le signale clairement — plutôt que de continuer à
            # utiliser silencieusement l'ancien identifiant.
            ancien_id = ancien_exec.get(m)
            if ancien_id and ancien_id != inst_id_exec:
                log.warning(f"  🔄 [ROLLOVER] {m} : nouveau contrat adopté automatiquement "
                            f"({ancien_id} → {inst_id_exec}, ctVal={ct_val_exec}).")
                await telegram(session,
                    f"🔄 <b>ROLLOVER DÉTECTÉ ET ADOPTÉ</b>\n"
                    f"{m} : bascule automatique vers le nouveau contrat conforme.\n"
                    f"Ancien : <code>{ancien_id}</code>\n"
                    f"Nouveau : <code>{inst_id_exec}</code> (ctVal={ct_val_exec})\n"
                    f"Aucune action requise — le bot trade désormais sur le nouveau contrat."
                )

            nouveaux_marches.append(m)
            OKX_SYMBOLS_EXEC[m]      = inst_id_exec  # instId d'exécution, scopé au compte — jamais utilisé pour les prix
            nouveaux_ct_val_exec[m]  = ct_val_exec
            ecart = " ⚠️ ÉCART DÉTECTÉ" if ct_val_public and ct_val_exec and ct_val_public != ct_val_exec else ""
            log.info(f"     [DIAG-EXEC] {m} instId={inst_id_exec} "
                     f"ctVal={inst_exec.get('ctVal')} ctValCcy={inst_exec.get('ctValCcy')} "
                     f"settleCcy={inst_exec.get('settleCcy')} instFamily={inst_exec.get('instFamily')} "
                     f"(vs ctVal catalogue public : {ct_val_public}){ecart}")

            # ── Alerte anticipée de rollover (08/07) — voir commentaire sur
            # SEUIL_ALERTE_ROLLOVER_JOURS. expTime est un timestamp epoch en
            # millisecondes (chaîne), vide pour les instruments sans échéance.
            exp_time_ms = inst_exec.get("expTime")
            if exp_time_ms and inst_id_exec not in ROLLOVER_ALERTES_ENVOYEES:
                try:
                    exp_dt        = datetime.utcfromtimestamp(int(exp_time_ms) / 1000)
                    jours_restants = (exp_dt - datetime.utcnow()).days
                    if jours_restants <= SEUIL_ALERTE_ROLLOVER_JOURS:
                        ROLLOVER_ALERTES_ENVOYEES.add(inst_id_exec)
                        log.warning(f"  📅 [ROLLOVER] {m} : le contrat {inst_id_exec} expire le "
                                    f"{exp_dt.strftime('%d/%m/%Y')} (dans {jours_restants} jours) — "
                                    f"un nouveau contrat sera généré automatiquement par OKX et "
                                    f"adopté automatiquement au prochain rafraîchissement (s'il "
                                    f"est conforme).")
                        await telegram(session,
                            f"📅 <b>ROLLOVER X-PERP À VENIR</b>\n"
                            f"{m} : le contrat actuel ({inst_id_exec}) expire le "
                            f"{exp_dt.strftime('%d/%m/%Y')} (dans {jours_restants} jours).\n"
                            f"OKX générera un nouveau contrat — le bot basculera dessus "
                            f"automatiquement s'il est conforme, ou bloquera {m} sinon. "
                            f"Aucune action requise de ta part."
                        )
                except (ValueError, TypeError, OverflowError) as e:
                    log.warning(f"  [ROLLOVER] Impossible de parser expTime={exp_time_ms} pour {m} : {e}")

        supprimes = avant - set(nouveaux_marches)
        MARCHES   = nouveaux_marches

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

async def okx_recuperer_position_reelle(session, inst_id, pos_id_attendu=None):
    """Interroge /api/v5/account/positions-history pour récupérer le résultat
    RÉEL (PnL réalisé, frais de transaction ET frais de financement séparés)
    de la position fermée sur cet instrument. Sert à comparer directement,
    dans le rapport Telegram, ce que le bot calcule en interne vs ce qu'OKX
    a réellement enregistré — et à distinguer clairement les frais
    d'ouverture/fermeture (0.05%+0.05% attendus) des frais de financement
    (funding fee, prélevés périodiquement sur les positions tenues assez
    longtemps, jamais pris en compte dans le calcul interne du bot).

    pos_id_attendu (07/07, 14:20 — confirmé via la doc officielle OKX, qui
    liste 'posId' comme identifiant UNIQUE de chaque position dans la
    réponse) : si fourni, on interroge plusieurs dossiers récents
    (limit=5, pas juste le plus récent) et on cherche celui dont le posId
    correspond EXACTEMENT — élimine tout risque de récupérer par erreur le
    dossier d'un AUTRE trade sur le même instrument (ce qu'une simple
    comparaison de prix d'entrée ne peut que suspecter approximativement).
    Si pos_id_attendu est absent (positions ouvertes avant ce correctif,
    ou échec de capture à l'ouverture), on garde le comportement précédent
    (dossier le plus récent, à valider ensuite par comparaison de prix côté
    appelant)."""
    path = "/api/v5/account/positions-history"
    limite = 5 if pos_id_attendu else 1
    query = f"?instId={inst_id}&limit={limite}"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0" or not data.get("data"):
                log.warning(f"  [VÉRIF-RÉELLE] Aucune position-history pour {inst_id} : {data}")
                return None
            enregistrements = data["data"]
            p = None
            matched_via_pos_id = False
            if pos_id_attendu:
                for candidat in enregistrements:
                    if candidat.get("posId") == pos_id_attendu:
                        p = candidat
                        matched_via_pos_id = True
                        break
                if p is None:
                    log.error(f"  [VÉRIF-RÉELLE] ⚠️ Aucun dossier avec posId={pos_id_attendu} "
                              f"trouvé parmi les {len(enregistrements)} plus récents pour {inst_id} — "
                              f"probablement pas encore synchronisé côté OKX.")
                    return None
            else:
                p = enregistrements[0]
            return {
                "pnl":                 float(p.get("pnl", 0) or 0),
                "fee":                 float(p.get("fee", 0) or 0),           # frais de transaction (ouv+ferm)
                "funding_fee":         float(p.get("fundingFee", 0) or 0),    # frais de financement séparés
                "open_px":             p.get("openAvgPx"),
                "close_px":            p.get("closeAvgPx"),
                "pos_id":              p.get("posId"),
                "matched_via_pos_id":  matched_via_pos_id,  # 08/07 — True = dossier confirmé
                                                              # EXACT par posId (identifiant unique
                                                              # OKX) ; False = dossier "le plus
                                                              # récent" pris par approximation
                                                              # (pos_id_attendu absent). Permet à
                                                              # l'appelant de ne PAS appliquer sa
                                                              # propre vérification de prix
                                                              # (approximative) par-dessus un
                                                              # résultat déjà certain.
            }
    except Exception as e:
        log.error(f"  [VÉRIF-RÉELLE] Exception position-history {inst_id} : {e}")
        return None

async def okx_recuperer_solde_reel(session, ccy="USDC"):
    """Récupère l'équité RÉELLE du compte OKX pour la devise donnée
    (par défaut USDC, la devise de marge de tous les contrats X-Perp
    utilisés). Retourne un float, ou None en cas d'échec.

    Utilisé comme SOURCE DE VÉRITÉ pour le capital après chaque trade réel,
    à la place du calcul cumulatif interne (capital + gain_final) qui peut
    dériver du vrai solde OKX — notamment quand une position est fermée
    manuellement par l'utilisateur (prix de sortie réel inconnu du bot),
    ou par accumulation de petits écarts d'arrondi/prix sur de nombreux
    trades. Le bot n'a plus le droit de "deviner" son capital : il va le
    lire directement sur le compte réel."""
    path  = "/api/v5/account/balance"
    query = f"?ccy={ccy}"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  [SOLDE-RÉEL] Erreur lecture solde réel : {data}")
                return None
            details = data.get("data", [{}])[0].get("details", [])
            for d in details:
                if d.get("ccy") == ccy:
                    eq = d.get("eq")
                    if eq is not None:
                        return float(eq)
            log.warning(f"  [SOLDE-RÉEL] Devise {ccy} introuvable dans la réponse du solde")
            return None
    except Exception as e:
        log.error(f"  [SOLDE-RÉEL] Exception lecture solde réel : {e}")
        return None

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
    """Interroge /api/v5/trade/order pour connaître l'état réel de l'ordre
    d'ouverture (state: filled/canceled/live/partially_filled) et le prix
    RÉELLEMENT rempli (avgPx) — pas seulement un diagnostic : ce prix réel
    est désormais RENVOYÉ à l'appelant, qui doit impérativement l'utiliser
    comme véritable prix d'entrée. Avant ce correctif, un ordre au marché
    pouvait se remplir avec un glissement important (confirmé en conditions
    réelles : ordre visé à 1777.0, rempli à 1696.39, écart de 80 points)
    sans que le reste du trade — stop, lock, PnL affiché — ne le sache
    jamais, puisque tout continuait à se baser sur le prix visé avant
    l'ordre plutôt que sur le prix réellement obtenu.
    Retourne le avgPx réel (float) si l'ordre est rempli, sinon None."""
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
                return None
            o = data["data"][0]
            log.info(f"  [DIAG-ORDRE] {inst_id} ordId={ord_id} state={o.get('state')} "
                     f"sz={o.get('sz')} accFillSz={o.get('accFillSz')} "
                     f"avgPx={o.get('avgPx')} px={o.get('px')}")
            avg_px = o.get("avgPx")
            if avg_px and float(avg_px) > 0:
                return float(avg_px)
            return None
    except Exception as e:
        log.error(f"  [DIAG-ORDRE] Exception lecture statut ordre {ord_id} : {e}")
        return None

async def okx_position_existe_deja(session, inst_id, contexte="anti-doublon"):
    """Vérifie auprès d'OKX (source de vérité externe, pas juste l'état
    interne Python) si une position non-nulle existe déjà sur cet
    instrument. Deux usages désormais : (1) garde-fou anti-doublon avant
    d'ouvrir un nouveau trade réel (usage d'origine), et (2) détecter si un
    trailing natif a DÉJÀ fermé une position, dans surveiller_et_fermer_trade
    (07/07, 15:58). Le paramètre contexte adapte le message loggé — dans le
    cas (2), trouver une position existante est le résultat ATTENDU la
    plupart du temps (le trailing n'a pas encore fermé), donc pas la peine
    de le logger comme un warning à chaque tick.
    Retourne True si une position existe déjà, False si la voie est libre
    (ou si le trailing a déjà fermé, selon le contexte), None si la
    vérification a échoué (repli : ne pas bloquer indéfiniment sur une
    panne réseau)."""
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
                log.error(f"  [{contexte.upper()}] Erreur lecture position {inst_id} : {data}")
                return None
            for p in data.get("data", []):
                pos_size = float(p.get("pos", 0) or 0)
                if pos_size != 0:
                    if contexte == "anti-doublon":
                        log.warning(f"  [ANTI-DOUBLON] Position déjà existante sur {inst_id} "
                                    f"(pos={pos_size}) — nouvelle ouverture bloquée")
                    # Sinon (contexte="trailing") : silencieux, c'est le cas
                    # normal/attendu tant que le trailing n'a pas encore fermé.
                    return True
            return False
    except Exception as e:
        log.error(f"  [{contexte.upper()}] Exception vérification position {inst_id} : {e}")
        return None

async def okx_algo_order_est_actif(session, inst_id, algo_id, algo_type):
    """Vérifie que l'ordre algo (stop fixe OU trailing) est bien encore VIVANT
    côté OKX — GET /api/v5/trade/orders-algo-pending (confirmé via la doc
    officielle : 'Retrieve a list of untriggered Algo orders'). Angle mort
    identifié le 07/07 (soir 2) : jusqu'ici, le bot vérifiait seulement si la
    POSITION existait encore (okx_position_existe_deja), jamais si l'ordre de
    PROTECTION lui-même était toujours actif. Si cet ordre disparaissait
    silencieusement côté OKX (annulation, expiration, rejet après coup —
    comportement possible sur l'environnement démo) SANS que la position ne
    se ferme, le bot continuait de suivre les paliers normalement (son
    propre calcul de PnL reste indépendant) tout en n'ayant PLUS AUCUNE
    protection réelle — jusqu'à ce que le prix s'effondre sans qu'aucun stop
    ne se déclenche. Exactement le symptôme rapporté : paliers 1, 2, 3
    franchis normalement puis chute brutale en négatif, sans fermeture.

    CORRECTIF (07/07, 22:54) : le paramètre 'ordType' est REQUIS par cet
    endpoint — confirmé par l'erreur OKX elle-même (code 51000 'Parameter
    ordType error', où 51000 est un code générique documenté 'Parameter %s
    error', %s précisant le nom du paramètre en cause). Sans lui, CHAQUE
    appel échouait silencieusement (aucune exception Python, juste un code
    d'erreur ignoré par l'appelant) — la vérification n'a donc JAMAIS
    fonctionné depuis sa création, bien que le reste du système ait
    continué à protéger correctement via l'autre garde-fou (vérification
    d'existence de la position). algo_type ('fixe' ou 'trailing') est
    mappé vers la valeur OKX correspondante ('conditional' ou
    'move_order_stop').

    Retourne True si l'ordre est bien dans la liste des ordres en attente,
    False s'il n'y est plus (danger : protection disparue), None si la
    vérification a échoué (repli : ne pas déclencher une fausse alerte sur
    un simple aléa réseau)."""
    if not algo_id:
        return None
    ord_type_okx = "move_order_stop" if algo_type == "trailing" else "conditional"
    path  = "/api/v5/trade/orders-algo-pending"
    query = f"?instId={inst_id}&algoId={algo_id}&ordType={ord_type_okx}"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                # ── CORRECTIF (08/07, 00:43) — confirmé en conditions réelles :
                # OKX répond parfois avec une ERREUR au niveau code (pas juste
                # une liste vide) quand l'algoId interrogé n'existe simplement
                # plus dans le système — code 51603 'Order does not exist'.
                # Ceci arrive normalement APRÈS qu'un ordre s'est déclenché et
                # a été nettoyé côté OKX. Sans cette distinction, CE code précis
                # était traité comme 'vérification impossible' (None) au lieu
                # de 'ordre disparu' (False) — la détection de fermeture ne se
                # déclenchait donc JAMAIS, laissant tourner le bot indéfiniment
                # sans jamais envoyer le message Telegram de clôture (symptôme
                # concret rapporté : trade fermé sur OKX, aucun message reçu).
                if data.get("code") == "51603":
                    log.info(f"  ℹ️ [ALGO-VIVANT] {inst_id} algoId={algo_id} : {data.get('msg')} "
                             f"(51603 = déjà déclenché et nettoyé côté OKX) — traité comme disparu.")
                    return False
                log.error(f"  [ALGO-VIVANT] Erreur lecture ordres pendants {inst_id} : {data}")
                return None
            for o in data.get("data", []):
                if o.get("algoId") == algo_id:
                    return True
            return False
    except Exception as e:
        log.error(f"  [ALGO-VIVANT] Exception vérification {inst_id} algoId={algo_id} : {e}")
        return None

async def okx_lister_toutes_positions_ouvertes(session):
    """Interroge /api/v5/account/positions SANS filtre d'instrument, pour
    lister TOUTES les positions réellement ouvertes sur le compte. Sert à
    synchroniser trades_ouverts au démarrage du bot — corrige l'angle mort
    suivant : trades_ouverts n'existe qu'en RAM (jamais persisté), donc un
    redémarrage de conteneur (crash, redéploiement Railway) le vide
    complètement, même si de vraies positions restent ouvertes sur OKX. Un
    bot qui redémarre ainsi 'amnésique' pourrait tenter d'ouvrir un nouveau
    trade sur un marché où une position tourne déjà réellement — le
    garde-fou okx_position_existe_deja le bloquerait à l'ouverture, mais
    autant synchroniser dès le départ plutôt que de compter uniquement sur
    ce filet de sécurité.
    Retourne la liste des instId avec une position non-nulle."""
    path = "/api/v5/account/positions"
    try:
        async with session.get(
            OKX_BASE_URL + path,
            headers=_okx_headers("GET", path, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  [SYNC-DÉMARRAGE] Erreur lecture positions ouvertes : {data}")
                return []
            instIds = []
            for p in data.get("data", []):
                pos_size = float(p.get("pos", 0) or 0)
                if pos_size != 0:
                    instIds.append(p.get("instId"))
            return instIds
    except Exception as e:
        log.error(f"  [SYNC-DÉMARRAGE] Exception lecture positions ouvertes : {e}")
        return []

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
    dans executer_trade avant tout usage réel.

    Utilise un clOrdId unique par tentative (bonne pratique OKX standard).
    En cas d'exception réseau (ex: timeout sur la RÉPONSE alors que l'ordre
    a pu être réellement traité côté OKX), on vérifie l'état réel de
    l'ordre via ce clOrdId avant de conclure à un échec — pour ne jamais
    annoncer 'TRADE ANNULÉ' à tort alors qu'une position a bel et bien été
    ouverte, ce qui mènerait plus tard à un doublon sur un nouveau signal."""
    path = "/api/v5/trade/order"
    cl_ord_id = f"b{int(time.time())}{uuid.uuid4().hex[:16]}"
    body = json.dumps({
        "instId": inst_id,
        "tdMode": "isolated",
        "side": side,
        "ordType": "market",
        "sz": str(taille_contrats),
        "clOrdId": cl_ord_id,
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
            log.warning(f"  💰 ORDRE RÉEL PLACÉ : {inst_id} {side} {taille_contrats} contrats "
                        f"(ordId={ord_id}, clOrdId={cl_ord_id})")
            return ord_id
    except Exception as e:
        log.error(f"  ❌ Exception ordre {inst_id} (clOrdId={cl_ord_id}) : {e} "
                  f"— vérification via clOrdId avant de conclure à un échec...")
        ord_id_recupere = await okx_verifier_ordre_par_clordid(session, inst_id, cl_ord_id)
        if ord_id_recupere:
            log.warning(f"  ⚠️ Ordre RÉELLEMENT PASSÉ malgré l'exception réseau : "
                        f"{inst_id} ordId={ord_id_recupere} (récupéré via clOrdId)")
            return ord_id_recupere
        return None

async def okx_verifier_ordre_par_clordid(session, inst_id, cl_ord_id):
    """Interroge OKX pour savoir si un ordre avec ce clOrdId a réellement été
    traité, malgré une exception réseau côté client (timeout sur la réponse
    par exemple). Retourne l'ordId si l'ordre existe et a été rempli/accepté,
    sinon None. Évite de déclarer à tort un 'TRADE ANNULÉ' alors que
    l'ordre est réellement passé côté OKX — la cause la plus concrète
    identifiée pour expliquer un doublon de position sans double instance
    ni bug de verrou."""
    path  = "/api/v5/trade/order"
    query = f"?instId={inst_id}&clOrdId={cl_ord_id}"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0" or not data.get("data"):
                return None
            o = data["data"][0]
            if o.get("state") in ("filled", "live", "partially_filled"):
                return o.get("ordId")
            return None
    except Exception as e:
        log.error(f"  [VÉRIF-CLORDID] Exception vérification {cl_ord_id} : {e}")
        return None

async def okx_placer_ordre_stop_algo(session, inst_id, side_ouverture, taille_contrats, prix_stop):
    """Pose un ordre STOP CONDITIONNEL natif côté OKX (endpoint /trade/order-algo),
    déclenché et exécuté par OKX LUI-MÊME dès que le prix atteint prix_stop —
    sans dépendre du prochain check du bot (CHECK_INTERVAL=3s). Réduit le
    slippage d'exécution en cas de mouvement brutal entre deux vérifications
    internes.

    reduceOnly=true : ne peut QUE réduire/fermer une position existante,
    jamais en ouvrir une nouvelle par erreur (garde-fou) — même si appelé
    avec une taille ou un side incohérent, OKX rejette plutôt que d'ouvrir
    une position inattendue.

    side_ouverture = 'buy' ou 'sell' (le sens du trade à sa création) — le
    stop doit être posé dans le sens INVERSE pour fermer la position.
    Retourne l'algoId si succès, sinon None (dans ce cas, la surveillance
    interne du bot — boucle de 3s — reste l'unique filet de sécurité,
    exactement comme avant ce correctif).

    ── PRIX LIMITE PLAFONNÉ (08/07) — voir STOP_BUFFER_SLIPPAGE_PCT. Au lieu
    d'un ordre au marché pur (slOrdPx=-1, aucune limite de prix), on pose un
    prix limite légèrement au-delà du déclenchement, dans le sens qui
    garantit un remplissage quasi certain en conditions normales tout en
    plafonnant la perte maximale en cas de mouvement brutal — recommandation
    officielle OKX. Le déclenchement lui-même passe au prix "mark" (moyenne
    lissée, moins sensible à une mèche isolée) plutôt que "last", pour éviter
    un déclenchement prématuré sur un pic de prix ponctuel non confirmé."""
    side_fermeture = "sell" if side_ouverture == "buy" else "buy"
    # Fermeture par VENTE (position ACHAT/longue) : le marché baisse jusqu'au
    # déclenchement — le prix limite doit être un peu EN DESSOUS pour être
    # sûr de trouver preneur. Fermeture par ACHAT (position VENTE/courte) :
    # le marché monte jusqu'au déclenchement — le prix limite doit être un
    # peu AU-DESSUS.
    if side_fermeture == "sell":
        prix_limite = round(float(prix_stop) * (1 - STOP_BUFFER_SLIPPAGE_PCT), 8)
    else:
        prix_limite = round(float(prix_stop) * (1 + STOP_BUFFER_SLIPPAGE_PCT), 8)
    path = "/api/v5/trade/order-algo"
    body = json.dumps({
        "instId":         inst_id,
        "tdMode":         "isolated",
        "side":           side_fermeture,
        "ordType":        "conditional",
        "sz":             str(taille_contrats),
        "reduceOnly":     "true",
        "slTriggerPx":    str(prix_stop),
        "slOrdPx":        str(prix_limite),  # prix limite plafonné, plus "-1" (marché pur)
        "slTriggerPxType": "mark",           # moins sensible à une mèche isolée que "last"
    })
    try:
        async with session.post(
            OKX_BASE_URL + path, data=body,
            headers=_okx_headers("POST", path, body),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  ❌ [STOP-ALGO] Échec pose stop natif {inst_id} @ {prix_stop} : {data}")
                return None
            algo_id = data.get("data", [{}])[0].get("algoId")
            log.info(f"  🛡️ [STOP-ALGO] Stop natif OKX posé : {inst_id} {side_fermeture} "
                     f"{taille_contrats} contrats @ {prix_stop} (algoId={algo_id})")
            return algo_id
    except Exception as e:
        log.error(f"  ❌ [STOP-ALGO] Exception pose stop natif {inst_id} @ {prix_stop} : {e}")
        return None

async def okx_annuler_ordre_algo(session, inst_id, algo_id):
    """Annule un ordre algo (stop conditionnel) préalablement posé. Appelée
    systématiquement dès qu'un trade se termine par un autre chemin que le
    déclenchement du stop lui-même (lock de profit, durée max, ou même le
    stop détecté en interne en parallèle) — indispensable pour ne JAMAIS
    laisser un ordre stop actif traîner sur l'instrument après la fin du
    trade : un ordre orphelin de ce type pourrait sinon se déclencher plus
    tard sur un AUTRE trade ouvert ensuite sur le même marché, et le fermer
    à un moment totalement inattendu.
    Retourne True si annulé (ou déjà inexistant/déjà déclenché — dans les
    deux cas, rien à annuler, ce n'est pas un échec), False sinon."""
    if not algo_id:
        return True
    path = "/api/v5/trade/cancel-algos"
    body = json.dumps([{"instId": inst_id, "algoId": algo_id}])
    try:
        async with session.post(
            OKX_BASE_URL + path, data=body,
            headers=_okx_headers("POST", path, body),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                # Code générique OK au niveau requête, mais vérifier le sCode
                # par ordre : un algo déjà déclenché/annulé n'est pas une
                # vraie erreur, juste "plus rien à annuler".
                items = data.get("data", [])
                sCode = items[0].get("sCode") if items else None
                if sCode in ("51535", "51536", "51000"):  # déjà inexistant/traité
                    log.info(f"  ℹ️ [STOP-ALGO] {inst_id} algoId={algo_id} déjà inexistant/traité "
                             f"— rien à annuler.")
                    return True
                log.warning(f"  ⚠️ [STOP-ALGO] Échec annulation {inst_id} algoId={algo_id} : {data}")
                return False
            log.info(f"  🛡️ [STOP-ALGO] Stop natif annulé : {inst_id} algoId={algo_id}")
            return True
    except Exception as e:
        log.error(f"  ❌ [STOP-ALGO] Exception annulation {inst_id} algoId={algo_id} : {e}")
        return False

async def okx_placer_trailing_stop_natif(session, inst_id, side_ouverture, taille_contrats,
                                          callback_ratio, active_px=None):
    """Pose un TRAILING STOP natif OKX (ordType='move_order_stop') — confirmé
    le 07/07 via la doc officielle OKX + une librairie de trading tierce qui
    l'implémente en production (NautilusTrader) : c'est un type d'ordre
    algo DISTINCT du stop conditionnel classique qu'on utilisait jusqu'ici
    (ordType='conditional'). Contrairement au stop fixe, celui-ci suit
    automatiquement le prix côté serveur OKX — plus besoin d'annuler/reposer
    à chaque nouveau palier de gain, éliminant la fenêtre de risque où le
    prix peut bouger entre notre détection et notre repositionnement
    (confirmé responsable d'un échec réel le 07/07 12:40 : 'SL trigger
    price cannot be lower than the last price').

    callback_ratio : écart de suivi en fraction (ex: 0.006 pour 0.6%) —
    le stop se recalcule en continu à cette distance du plus haut (ACHAT)
    ou du plus bas (VENTE) atteint depuis l'activation.
    active_px : prix à partir duquel le trailing s'active. None = activation
    immédiate au prix courant (comportement souhaité ici, le trailing doit
    protéger dès l'ouverture, pas seulement une fois un seuil dépassé).

    side_ouverture = 'buy' ou 'sell' (le sens du trade à l'ouverture) — la
    fonction déduit le side de fermeture, comme okx_placer_ordre_stop_algo.
    Retourne l'algoId si succès, sinon None (dans ce cas, le stop fixe
    classique — voir okx_placer_ordre_stop_algo — reste utilisable comme
    repli)."""
    side_fermeture = "sell" if side_ouverture == "buy" else "buy"
    path = "/api/v5/trade/order-algo"
    body_dict = {
        "instId":        inst_id,
        "tdMode":        "isolated",
        "side":          side_fermeture,
        "ordType":       "move_order_stop",
        "sz":            str(taille_contrats),
        "reduceOnly":    "true",
        "callbackRatio": str(callback_ratio),
    }
    if active_px is not None:
        body_dict["activePx"] = str(active_px)
    body = json.dumps(body_dict)
    try:
        async with session.post(
            OKX_BASE_URL + path, data=body,
            headers=_okx_headers("POST", path, body),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                log.error(f"  ❌ [TRAILING] Échec pose trailing stop {inst_id} "
                          f"(callback={callback_ratio}) : {data}")
                return None
            algo_id = data.get("data", [{}])[0].get("algoId")
            log.info(f"  🛡️ [TRAILING] Trailing stop natif posé : {inst_id} {side_fermeture} "
                     f"{taille_contrats} contrats, callback={callback_ratio} (algoId={algo_id})")
            return algo_id
    except Exception as e:
        log.error(f"  ❌ [TRAILING] Exception pose trailing stop {inst_id} : {e}")
        return None

async def okx_amender_stop_floor(session, inst_id, algo_id, nouveau_prix_stop, side_ouverture):
    """Modifie EN PLACE le prix de déclenchement (slTriggerPx) du plancher dur
    déjà posé, via /api/v5/trade/amend-algos — SANS l'annuler ni en reposer
    un nouveau. Confirmé via la doc officielle OKX (endpoint distinct de
    place-algo/cancel-algos, section Algo Trading) : 1 seul appel réseau au
    lieu de 2 (pose du nouveau + annulation de l'ancien), et surtout aucune
    fenêtre où le plancher serait absent ou dupliqué pendant la transition.

    Ne s'applique QU'au plancher dur (ordType='conditional') — le trailing
    natif ('move_order_stop') n'a pas de champ callbackRatio amendable par
    cet endpoint, mais n'en a de toute façon pas besoin puisqu'il suit déjà
    le prix en continu côté serveur OKX.

    side_ouverture = 'buy' ou 'sell' (sens du trade à l'ouverture) — permet
    de calculer le prix limite plafonné (voir STOP_BUFFER_SLIPPAGE_PCT dans
    okx_placer_ordre_stop_algo) dans le bon sens, cohérent avec la pose
    initiale du plancher.

    Retourne True si l'amendement a réussi, False sinon — dans ce cas,
    l'appelant doit se rabattre sur l'ancien mécanisme pose+annulation
    (jamais de trade laissé sans plancher à cause d'un simple échec ici)."""
    if not algo_id:
        return False
    side_fermeture = "sell" if side_ouverture == "buy" else "buy"
    if side_fermeture == "sell":
        prix_limite = round(float(nouveau_prix_stop) * (1 - STOP_BUFFER_SLIPPAGE_PCT), 8)
    else:
        prix_limite = round(float(nouveau_prix_stop) * (1 + STOP_BUFFER_SLIPPAGE_PCT), 8)
    path = "/api/v5/trade/amend-algos"
    body = json.dumps({
        "instId":              inst_id,
        "algoId":              algo_id,
        "newSlTriggerPx":      str(nouveau_prix_stop),
        "newSlOrdPx":          str(prix_limite),  # prix limite plafonné, cohérent avec la pose initiale
        "newSlTriggerPxType":  "mark",
    })
    try:
        async with session.post(
            OKX_BASE_URL + path, data=body,
            headers=_okx_headers("POST", path, body),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                items = data.get("data", [])
                sCode = items[0].get("sCode") if items else None
                log.warning(f"  ⚠️ [PLANCHER-AMEND] Échec amendement {inst_id} algoId={algo_id} "
                            f"vers {nouveau_prix_stop} (sCode={sCode}) : {data} — repli sur "
                            f"pose+annulation.")
                return False
            log.info(f"  🧱 [PLANCHER-AMEND] Plancher amendé en place : {inst_id} algoId={algo_id} "
                     f"→ {nouveau_prix_stop} (1 seul appel, aucune fenêtre sans protection)")
            return True
    except Exception as e:
        log.error(f"  ❌ [PLANCHER-AMEND] Exception amendement {inst_id} algoId={algo_id} : {e}")
        return False

async def okx_annuler_trailing_stop(session, inst_id, algo_id):
    """Annule un trailing stop natif — endpoint DIFFÉRENT du stop classique.
    La doc OKX précise explicitement que /trade/cancel-algos NE COUVRE PAS
    les ordres Trailing Stop (ni Iceberg, ni TWAP) : il faut
    /trade/cancel-advance-algos. Utiliser le mauvais endpoint laisserait
    le trailing stop actif sans que le code ne s'en aperçoive."""
    if not algo_id:
        return True
    path = "/api/v5/trade/cancel-advance-algos"
    body = json.dumps([{"instId": inst_id, "algoId": algo_id}])
    try:
        async with session.post(
            OKX_BASE_URL + path, data=body,
            headers=_okx_headers("POST", path, body),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                items = data.get("data", [])
                sCode = items[0].get("sCode") if items else None
                if sCode in ("51535", "51536", "51000"):
                    log.info(f"  ℹ️ [TRAILING] {inst_id} algoId={algo_id} déjà inexistant/traité "
                             f"— rien à annuler.")
                    return True
                log.warning(f"  ⚠️ [TRAILING] Échec annulation {inst_id} algoId={algo_id} : {data}")
                return False
            log.info(f"  🛡️ [TRAILING] Trailing stop annulé : {inst_id} algoId={algo_id}")
            return True
    except Exception as e:
        log.error(f"  ❌ [TRAILING] Exception annulation {inst_id} algoId={algo_id} : {e}")
        return False

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

async def telegram_document(session, nom_fichier, contenu_texte, legende=""):
    """Envoie un fichier texte (CSV notamment) en pièce jointe Telegram, via
    l'endpoint sendDocument (multipart). Utilisé pour le détail complet des
    trades du rapport quotidien (10/07, demandé par Damien) : le message
    texte seul dépassait la limite de 4096 caractères de Telegram sur une
    journée chargée ("rapport tronqué, trop de trades") — le détail
    ligne-par-ligne part maintenant en pièce jointe, ouvrable dans
    Excel/Google Sheets, sans limite de taille pratique."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
        form = aiohttp.FormData()
        form.add_field("chat_id", str(TELEGRAM_CHAT_ID))
        if legende:
            form.add_field("caption", legende)
        form.add_field("document", contenu_texte.encode("utf-8-sig"),
                        filename=nom_fichier, content_type="text/csv")
        await session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=20))
    except Exception as e:
        log.error(f"Erreur envoi document Telegram ({nom_fichier}) : {e}")

def construire_csv_trades(detail_trades):
    """Construit le contenu CSV du détail par trade — mêmes champs que le
    bloc texte 'DÉTAIL PAR TRADE' du rapport, mais sans limite de lignes.
    Noms de colonnes = noms de champs RÉELS de detail_trades dans ce
    fichier (pas de risque de colonne vide par mauvaise correspondance,
    contrairement à un générateur externe qui devinerait ces noms)."""
    import csv
    import io
    buffer = io.StringIO()
    colonnes = [
        "heure_ouverture", "marche", "direction", "resultat", "motif_sortie",
        "gain_eur", "pnl_max_eur", "pnl_max_pct", "frais_estimes",
        "prix_entree", "prix_sortie", "prix_stop", "objectif",
        "rsi", "volume_ratio", "variation_pct", "glissement_pct", "atr_pct",
        "breakeven_anticipe", "duree_min",
        "stop_bien_place", "suivi_post_stop_pct", "palier1_atteint_post_stop",
    ]
    writer = csv.writer(buffer, delimiter=';')
    writer.writerow(colonnes)
    for d in detail_trades:
        writer.writerow([
            d.get("heure_ouv", "?"),
            d.get("marche", "?"),
            d.get("direction", "?"),
            d.get("resultat", "?"),
            d.get("motif_sortie", "?"),
            d.get("gain", 0),
            d.get("pnl_max", ""),
            d.get("pnl_max_pct", ""),
            d.get("frais", ""),
            d.get("prix_entree", ""),
            d.get("prix_sortie", ""),
            d.get("prix_stop", ""),
            d.get("objectif", ""),
            d.get("rsi", ""),
            d.get("vol", ""),
            d.get("variation", ""),
            d.get("glissement", ""),
            d.get("atr", ""),
            d.get("breakeven", ""),
            d.get("duree", ""),
            d.get("stop_bien_place", ""),
            d.get("suivi_post_stop_pct", ""),
            d.get("palier1_post_stop", ""),
        ])
    return buffer.getvalue()

async def vider_file_erreurs_vers_telegram(session):
    """Regroupe toutes les erreurs (niveau ERROR/CRITICAL) accumulées depuis
    le dernier passage — voir HandlerErreursTelegram — en UN SEUL message
    Telegram, plutôt qu'un message par erreur (qui noierait le chat en cas
    de rafale). Appelée toutes les 60s depuis la boucle principale.

    La file est vidée AVANT l'envoi (pas après) : si l'envoi Telegram
    lui-même échoue, on ne rappelle pas log.error ici (juste un print sur
    stdout, visible dans Railway) — pour éviter que l'échec d'envoi ne
    s'auto-alimente indéfiniment dans sa propre file."""
    if not FILE_ERREURS_TELEGRAM:
        return
    lignes = FILE_ERREURS_TELEGRAM[:]
    FILE_ERREURS_TELEGRAM.clear()
    # Limite raisonnable par message Telegram (4096 caractères max côté
    # Telegram) — au-delà, on tronque plutôt que d'échouer silencieusement.
    corps = "\n".join(lignes)
    if len(corps) > 3500:
        corps = corps[:3500] + f"\n… ({len(lignes)} erreurs au total, tronqué)"
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        # Texte BRUT (pas de parse_mode) : les messages d'erreur/exceptions
        # peuvent contenir des caractères '<', '>', '&' (dicts Python, JSON
        # d'erreur OKX...) qui casseraient le parsing HTML de Telegram et
        # feraient échouer silencieusement l'envoi de tout le lot.
        await session.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text":    f"🪵 ERREURS DÉTECTÉES ({len(lignes)})\n\n{corps}",
        }, timeout=aiohttp.ClientTimeout(total=10))
    except Exception as e:
        print(f"[TELEGRAM-ERRORS] Échec envoi du lot d'erreurs : {e}")

# ═══════════════════════════════════════════════════════════════
#  DONNÉES MARCHÉ — OKX
# ═══════════════════════════════════════════════════════════════
async def get_klines(session, symbole, interval=15, limite=50):
    # REVENU au catalogue public (feed) après incident confirmé : OKX
    # rejette l'instId d'exécution sur le WebSocket public ("doesn't
    # exist", code 60018) — preuve que seul OKX_SYMBOLS est un instrument
    # public reconnu de bout en bout. Utiliser l'EXEC ici créerait une
    # incohérence : le cache WS (alimenté en feed) et les klines (en exec)
    # ne parleraient plus du même prix pendant le scan. La correction du
    # bug de PnL reste dans get_prix_reel_instid (dédiée à la surveillance
    # d'une position déjà ouverte, en REST, confirmée fonctionnelle).
    okx_symbol = OKX_SYMBOLS.get(symbole, symbole)
    bar = {15: "15m", 60: "1H"}.get(interval, "15m")
    # ── ROBUSTESSE (11/07) — les klines échouaient parfois sur un simple
    # timeout réseau transitoire vers OKX (asyncio.TimeoutError, dont le str()
    # est VIDE → log "Erreur klines ETHUSD :" sans le moindre détail). On
    # retente jusqu'à 3 fois avec une courte pause pour les erreurs réseau
    # transitoires, et si tout échoue on logue le TYPE de l'exception (jamais
    # un message vide, pour ne plus avoir d'erreur illisible dans le rapport).
    derniere_exc = None
    for tentative in range(3):
        try:
            async with session.get(
                "https://www.okx.com/api/v5/market/candles",
                params={"instId": okx_symbol, "bar": bar, "limit": str(limite)},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
                if data.get("code") != "0":
                    log.warning(f"  ⚠️ klines {symbole} : réponse OKX code={data.get('code')} "
                                f"msg={data.get('msg')}")
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
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            derniere_exc = e
            if tentative < 2:
                await asyncio.sleep(1.0)  # blip réseau transitoire — on retente
                continue
        except Exception as e:
            derniere_exc = e
            break  # erreur non réseau (ex: format inattendu) — inutile de retenter
    detail = f"{type(derniere_exc).__name__}: {derniere_exc}".rstrip(": ")
    log.error(f"Erreur klines {symbole} : {detail} (après {tentative+1} tentative(s))")
    return None

async def get_prix_rest(session, symbole):
    """Prix via l'API REST OKX (fallback tant que le WebSocket n'a pas encore
    poussé de tick, ou si le cache est périmé). Catalogue public (feed) —
    voir get_klines pour la raison du retour en arrière depuis l'exec."""
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

async def okx_pnl_reel_upl(session, inst_id):
    """Interroge /api/v5/account/positions pour lire le PnL non réalisé (upl)
    tel que calculé par OKX LUI-MÊME — utilisé comme garde-fou de
    confirmation avant de déclarer une SORTIE LOCK (jamais pour un STOP, déjà
    backé par le stop natif OKX qui, lui, se déclenche côté serveur sur la
    base du vrai prix, indépendamment de tout calcul interne).
    Retourne le upl en USDC (traité comme équivalent €, comme partout
    ailleurs dans ce fichier — pas de conversion FX explicite), ou None si
    indisponible. None doit être traité par l'appelant comme 'impossible à
    confirmer', pas comme une confirmation positive ou négative."""
    path  = "/api/v5/account/positions"
    query = f"?instId={inst_id}"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0" or not data.get("data"):
                return None
            return float(data["data"][0].get("upl", 0) or 0)
    except Exception as e:
        log.error(f"  [PNL-RÉEL] Exception lecture upl {inst_id} : {e}")
        return None

async def okx_recuperer_pos_id(session, inst_id):
    """Interroge /api/v5/account/positions pour capturer le posId — identifiant
    UNIQUE de la position (confirmé le 07/07 via la doc officielle OKX,
    présent dans la réponse de positions-history). Appelée une fois juste
    après l'ouverture réelle d'un trade, pour permettre ensuite à
    okx_recuperer_position_reelle de retrouver EXACTEMENT le bon dossier à
    la fermeture, plutôt que de deviner via une comparaison de prix
    approximative. Retourne le posId (str) si trouvé, sinon None — dans ce
    cas, la vérification à la fermeture retombe sur l'ancienne méthode
    (comparaison de prix, moins fiable mais fonctionnelle)."""
    path  = "/api/v5/account/positions"
    query = f"?instId={inst_id}"
    try:
        async with session.get(
            OKX_BASE_URL + path + query,
            headers=_okx_headers("GET", path + query, ""),
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0" or not data.get("data"):
                return None
            return data["data"][0].get("posId")
    except Exception as e:
        log.error(f"  [POS-ID] Exception lecture posId {inst_id} : {e}")
        return None

async def get_prix_reel_instid(session, inst_id):
    """Prix REST directement ancré sur l'instId RÉELLEMENT détenu (celui de
    OKX_SYMBOLS_EXEC), sans passer par le dictionnaire symbole→instId de
    price-feed (OKX_SYMBOLS). Existe pour une raison précise : OKX_SYMBOLS
    (catalogue PUBLIC, utilisé pour le WebSocket) et OKX_SYMBOLS_EXEC
    (catalogue scopé au COMPTE, utilisé pour la position réelle) sont
    résolus séparément et JAMAIS comparés — si OKX renvoie un instId
    différent entre les deux catalogues pour la même base (cas documenté :
    les comptes sont classés en catégories régionales avec des résultats
    parfois différents), le bot pourrait suivre le prix d'un contrat tout
    en détenant réellement une position sur un autre, avec un écart de prix
    non détecté. Utilisée pour la surveillance d'une position déjà ouverte,
    où la garantie de regarder le bon contrat prime sur la latence."""
    try:
        async with session.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": inst_id},
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
        log.error(f"Erreur prix REST (instId réel) {inst_id} : {e}")
        return None

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
        # REVENU au catalogue public (feed) seul — incident confirmé le
        # 07/07 08:31 : OKX rejette l'instId d'exécution sur ce canal
        # ("Wrong URL or channel... doesn't exist", code 60018), pour les 3
        # marchés simultanément. Le canal WebSocket public 'tickers' ne
        # reconnaît que les instId du catalogue PUBLIC — l'instId
        # d'exécution (compte/démo) n'existe pas dans son registre, même
        # s'il fonctionne très bien en REST (voir get_prix_reel_instid).
        args = [
            {"channel": "tickers", "instId": OKX_SYMBOLS[m]}
            for m in MARCHES if m in OKX_SYMBOLS
        ]
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
                            # Recherche dans le catalogue public uniquement
                            # — cohérent avec l'abonnement ci-dessus, qui
                            # n'utilise plus que OKX_SYMBOLS.
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
        # ── ATR en % du prix (10/07, demandé par Damien) : l'ATR brut n'est
        # pas comparable entre marchés (5.33 sur ETHUSD à ~1750$ vs 0.00077
        # sur ADAUSD à ~0.17$ — pas qu'ETHUSD soit 7000x plus volatil, juste
        # une différence d'échelle de prix). Exprimé en %, les marchés
        # deviennent comparables entre eux.
        "atr_pct":       round(atr_val / prix_actuel * 100, 3) if prix_actuel else 0.0,
        "vol_ratio":     vol_ratio,
        "rsi_1h":        rsi_1h,
        "variation_pct": abs(variation_pct),
        "prix_ref":      prix_ref,
        "prix_actuel":   prix_actuel,
    }

    # ── Filtre mouvement TROP violent (11/07) — voir SEUIL_MOUVEMENT_MAX_PCT.
    # Le signal serait éligible (|variation| >= seuil de déclenchement), mais le
    # mouvement dépasse le plafond : on n'entre pas. On ACQUITTE quand même le
    # mouvement (reset de la référence au niveau actuel) pour repartir chercher
    # une sur-réaction FRAÎCHE depuis ce nouveau niveau, plutôt que de rester
    # bloqué sur un écart énorme qui rejouerait "trop violent" en boucle.
    if abs(variation_pct) >= SEUIL_MOUVEMENT_MAX_PCT:
        prix_reference[symbole] = prix_actuel
        log.info(f"  {symbole} : Variation={variation_pct:+.2f}% >= plafond "
                 f"{SEUIL_MOUVEMENT_MAX_PCT}% → skip (mouvement trop violent pour du mean reversion)")
        return "NEUTRE", {}

    # Signal ACHAT : prix a chuté de >= 0.50%
    if variation_pct <= -SEUIL_MOUVEMENT_PCT:
        prix_reference[symbole] = prix_actuel
        if rsi_1h < RSI_SEUIL_BAS:
            direction = "VENTE"   # RSI bas → suit la tendance baissière (inverse le fade)
        else:
            direction = "ACHAT"   # fade classique de la baisse
        direction = _sens_effectif(direction)  # inversion expérimentale éventuelle
        log.info(f"  {symbole} {direction}{' [SENS INVERSÉ]' if INVERSER_SENS else ''} | "
                 f"Chute={variation_pct:.2f}% | RSI={rsi_1h} | Vol={vol_ratio:.2f}x")
        return direction, details

    # Signal VENTE : prix a monté de >= 0.50%
    if variation_pct >= SEUIL_MOUVEMENT_PCT:
        prix_reference[symbole] = prix_actuel
        if rsi_1h > RSI_SEUIL_HAUT:
            direction = "ACHAT"   # RSI haut → suit la tendance haussière (inverse le fade)
        else:
            direction = "VENTE"   # fade classique de la hausse
        direction = _sens_effectif(direction)  # inversion expérimentale éventuelle
        log.info(f"  {symbole} {direction}{' [SENS INVERSÉ]' if INVERSER_SENS else ''} | "
                 f"Montée={variation_pct:.2f}% | RSI={rsi_1h} | Vol={vol_ratio:.2f}x")
        return direction, details

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

    # ── Résolution de l'instrument et vérification de la taille — TOUJOURS
    # avant l'annonce Telegram. Avant ce correctif, le message "TRADE OUVERT"
    # partait systématiquement, même pour un trade annulé juste après (ex:
    # BTCUSD dont la taille calculée est < 1 contrat) — ce qui donnait
    # l'impression trompeuse d'un double trade sur le même marché.
    inst_id         = None
    taille_contrats = None
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

        # ── Vérification de cohérence instId feed vs exécution — les deux
        # catalogues (public pour le prix, scopé compte pour l'exécution)
        # sont résolus séparément et pourraient théoriquement diverger. Si
        # c'est le cas, la position réelle et le WebSocket de prix suivent
        # DEUX contrats différents — alerte immédiate, même si la boucle de
        # surveillance interroge désormais toujours inst_id directement
        # (protection réelle : stop natif OKX + vérification upl avant tout
        # LOCK, toutes deux confirmées fonctionnelles) et n'est donc plus
        # affectée par cet écart, quel qu'il soit.
        inst_id_feed = OKX_SYMBOLS.get(symbole)
        if inst_id_feed and inst_id_feed != inst_id:
            # ── WARNING (pas ERROR) volontairement (08/07) : message Telegram
            # dédié juste en dessous — éviter le doublon via le miroir d'erreurs.
            log.warning(f"  🚨 [INCOHÉRENCE INSTID] {symbole} : feed={inst_id_feed} "
                        f"vs exécution={inst_id} — DIFFÉRENTS. La position réelle et le "
                        f"prix suivi par WebSocket ne portent pas sur le même contrat.")
            await telegram(session,
                f"🚨 <b>ALERTE — INCOHÉRENCE INSTID</b>\n"
                f"{symbole} : le contrat de prix (WebSocket) et le contrat d'exécution "
                f"(position réelle) sont DIFFÉRENTS.\n"
                f"Feed : {inst_id_feed}\nExécution : {inst_id}\n"
                f"La surveillance continue sur le prix du flux public (seule source "
                f"fonctionnelle pour les données de marché) — protégée par le stop natif "
                f"OKX (posé sur le vrai contrat) et la vérification du PnL réel avant tout "
                f"LOCK. Pas d'action requise, juste une confirmation."
            )

        taille_contrats = round(position / (prix_entree * ct_val), 0)
        if taille_contrats < 1:
            log.error(f"  ❌ MODE_REEL actif mais taille calculée < 1 contrat pour {symbole} — trade annulé")
            await telegram(session, f"❌ <b>TRADE ANNULÉ</b>\n{symbole} : taille de position trop petite (<1 contrat).")
            async with trades_lock:
                trades_ouverts.pop(symbole, None)
            return

        # Vérification anti-doublon : une position existe-t-elle DÉJÀ
        # réellement sur OKX pour cet instrument ? Protège contre le cas
        # où plusieurs instances du bot tourneraient en parallèle (ex:
        # ancien déploiement Railway pas complètement arrêté) — le verrou
        # interne (trades_ouverts) ne peut pas détecter ce cas puisqu'il
        # ne connaît que l'état de SA PROPRE instance.
        position_deja_existante = await okx_position_existe_deja(session, inst_id)
        if position_deja_existante is True:
            log.error(f"  ❌ Position déjà existante sur OKX pour {symbole} — trade annulé "
                      f"(probable doublon d'instance)")
            await telegram(session,
                f"❌ <b>TRADE ANNULÉ</b>\n{symbole} : une position existe déjà réellement sur OKX "
                f"pour cet instrument — ouverture bloquée pour éviter un doublon "
                f"(possible instance du bot en double)."
            )
            async with trades_lock:
                trades_ouverts.pop(symbole, None)
            return

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
    # inchangé dans ce cas). instId/taille déjà validés ci-dessus.
    algo_id_stop   = None
    algo_type_stop = None
    pos_id         = None
    if MODE_REEL:
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
        avg_px_reel = await okx_diag_statut_ordre(session, inst_id, ord_id)

        # ── CORRECTIF CRITIQUE (07/07, 10:51) — un ordre au marché peut se
        # remplir à un prix différent du prix visé (glissement), parfois
        # significatif (confirmé en conditions réelles : visé 1777.0,
        # rempli 1696.39, écart de 80 points). Avant ce correctif, ce
        # glissement n'était JAMAIS répercuté : le stop, l'objectif, et
        # tout le calcul de PnL affiché continuaient à se baser sur le prix
        # visé avant l'ordre — le bot pouvait croire un trade équilibré ou
        # gagnant alors qu'il était réellement très en perte (ou l'inverse).
        # On recalcule donc TOUT (prix d'entrée, stop, objectif) à partir
        # du prix RÉELLEMENT rempli, avant de poser le stop natif — pour
        # que la protection elle-même se base sur la réalité, pas sur une
        # estimation périmée dès la première seconde du trade.
        if avg_px_reel and abs(avg_px_reel - prix_entree) / prix_entree > 0.0005:
            glissement_calc = (avg_px_reel - prix_entree) / prix_entree * 100
            details["glissement_pct"] = round(glissement_calc, 4)
            log.warning(f"  ⚠️ [GLISSEMENT] {symbole} : prix visé {prix_entree} → rempli "
                        f"{avg_px_reel} (écart {glissement_calc:.3f}%) — "
                        f"recalcul du stop/objectif sur le prix réel.")
            await telegram(session,
                f"⚠️ <b>GLISSEMENT D'EXÉCUTION</b>\n{symbole} : prix visé {prix_entree}, "
                f"rempli à {avg_px_reel} (écart {glissement_calc:.3f}%).\n"
                f"Stop et objectif recalculés sur le prix réel."
            )
        else:
            details["glissement_pct"] = 0.0
        if avg_px_reel:
            prix_entree = avg_px_reel
            if direction == "ACHAT":
                stop_initial   = round(prix_entree * (1 - ratio_prix), 8)
                objectif_final = round(prix_entree * (1 + ratio_prix * 2), 8)
            else:
                stop_initial   = round(prix_entree * (1 + ratio_prix), 8)
                objectif_final = round(prix_entree * (1 - ratio_prix * 2), 8)

        # ── HYBRIDE (07/07, 14:35) : stop FIXE classique à l'ouverture (comme
        # avant le passage au trailing) — protège large (STOP_LOSS_PCT=0.6%)
        # pendant que le trade n'a pas encore atteint de vrai gain. Le
        # basculement vers le trailing natif se fait UNE SEULE FOIS, dans
        # surveiller_et_fermer_trade, dès que le premier palier de lock est
        # franchi — voir plus bas. Ce compromis garde la protection rapide
        # des petits gains (comme l'ancien système de paliers) tout en
        # gardant la fiabilité du natif pour la suite du trade (un seul
        # basculement, pas 27 repositionnements répétés).
        algo_id_stop = await okx_placer_ordre_stop_algo(
            session, inst_id, side, taille_contrats, stop_initial
        )
        algo_type_stop = "fixe"
        if algo_id_stop is None:
            log.warning(f"  ⚠️ [STOP-ALGO] Pose du stop natif échouée pour {symbole} — "
                        f"la surveillance interne du bot reste l'unique filet de sécurité.")
            await telegram(session,
                f"⚠️ <b>STOP NATIF NON POSÉ</b>\n"
                f"{symbole} : la pose du stop natif OKX a échoué.\n"
                f"Pas d'inquiétude — la surveillance interne du bot reste active et "
                f"fermera la position normalement au niveau {stop_initial}."
            )
        else:
            await telegram(session,
                f"🛡️ <b>STOP NATIF ACTIVÉ</b>\n"
                f"{symbole} : OKX exécutera automatiquement la fermeture dès que le prix "
                f"atteint <b>{stop_initial}</b>. Un trailing stop natif prendra le relais "
                f"automatiquement dès le premier palier de gain atteint."
            )

        # ── Capture du posId — identifiant unique de la position (voir
        # okx_recuperer_pos_id) — permettra à la fermeture de retrouver EXACTEMENT
        # le bon dossier position-history, plutôt que de deviner via une
        # comparaison de prix approximative (voir surveiller_et_fermer_trade).
        pos_id = await okx_recuperer_pos_id(session, inst_id)
    else:
        # ── Glissement SIMULÉ (10/07) — voir constantes GLISSEMENT_SIMULE_*
        # plus haut. Symétrique au recalcul réel ci-dessus : même logique de
        # recalcul du stop/objectif à partir du prix simulé, pour que la
        # simulation se comporte comme le réel face au glissement.
        # ── Amplitude du glissement (TOUJOURS positive), calibrée sur le réel.
        ampleur_glissement = abs(random.gauss(GLISSEMENT_SIMULE_MOYEN_PCT,
                                              GLISSEMENT_SIMULE_ECART_TYPE))
        if random.random() < GLISSEMENT_SIMULE_PROBA_ACCIDENT:
            ampleur_glissement = abs(random.gauss(GLISSEMENT_SIMULE_ACCIDENT_MOYEN,
                                                  GLISSEMENT_SIMULE_ACCIDENT_ECART))
        ampleur_glissement = min(ampleur_glissement, GLISSEMENT_SIMULE_MAX_PCT)
        # ── CORRECTIF DIRECTION (11/07) — le glissement doit être DÉFAVORABLE au
        # sens du trade (c'était déjà l'intention, cf. commentaire des constantes
        # « toujours défavorable »), mais le code appliquait TOUJOURS un prix plus
        # bas : favorable à un ACHAT. Comme la stratégie ne génère quasiment que
        # des achats, ça gonflait tous les gains — un seul remplissage chanceux à
        # -2.8%/-3.6% pouvait fabriquer un +20€ qui n'existerait jamais en réel.
        # Un ordre au marché traverse le carnet CONTRE toi : un ACHAT se remplit
        # PLUS HAUT (+ampleur), une VENTE PLUS BAS (-ampleur). Convention de signe
        # identique au mode réel (glissement_pct = (prix rempli - prix visé)/prix
        # visé) : positif sur un achat défavorable, négatif sur une vente.
        if direction == "ACHAT":
            glissement_simule_pct = +ampleur_glissement   # rempli plus haut = contre l'acheteur
        else:
            glissement_simule_pct = -ampleur_glissement   # rempli plus bas = contre le vendeur
        prix_simule = round(prix_entree * (1 + glissement_simule_pct / 100), 8)
        details["glissement_pct"] = round(glissement_simule_pct, 4)
        if abs(glissement_simule_pct) > 0.05:
            log.info(f"  🎲 [GLISSEMENT SIMULÉ] {symbole} : prix visé {prix_entree} → "
                     f"simulé {prix_simule} (écart {glissement_simule_pct:.3f}%, calibré "
                     f"sur les données réelles observées cette semaine).")
            await telegram(session,
                f"🎲 <b>GLISSEMENT SIMULÉ</b>\n{symbole} : prix visé {prix_entree}, "
                f"simulé à {prix_simule} (écart {glissement_simule_pct:.3f}%).\n"
                f"Stop et objectif recalculés — glissement calibré sur les données réelles."
            )
        prix_entree = prix_simule
        if direction == "ACHAT":
            stop_initial   = round(prix_entree * (1 - ratio_prix), 8)
            objectif_final = round(prix_entree * (1 + ratio_prix * 2), 8)
        else:
            stop_initial   = round(prix_entree * (1 + ratio_prix), 8)
            objectif_final = round(prix_entree * (1 - ratio_prix * 2), 8)

    await surveiller_et_fermer_trade(
        session, symbole, direction, mise, capital, position,
        prix_entree, stop_initial, objectif_final, stop_loss_eur,
        rsi_1h, details, inst_id, etat_global, algo_id=algo_id_stop,
        taille_contrats=taille_contrats, pos_id=pos_id, algo_type=algo_type_stop
    )

async def suivre_prix_post_stop(session, symbole, direction, prix_stop_reel, prix_entree,
                                  pos_id, etat_global, position=None, capital=None,
                                  moment_fermeture_ts=None, cle_suivi=None):
    """Suit le prix pendant DUREE_SUIVI_POST_STOP_MIN après un stop-loss —
    sans position ouverte, en pure observation — pour savoir si le marché a
    continué dans le sens du stop (bien calibré) ou est reparti en sens
    inverse (stop trop serré), et s'il a franchi le niveau du 1er palier.

    RENDU PERSISTANT le 10/07 (demandé par Damien, suite à des colonnes
    vides dans le CSV — la tâche en mémoire était perdue à chaque
    redéploiement pendant la fenêtre de 15 min) : le suivi se base
    maintenant sur moment_fermeture_ts (horodatage RÉEL de la clôture,
    sauvegardé en base via suivis_post_stop_en_attente) plutôt que sur une
    simple attente en mémoire — si le bot redémarre en cours de route, il
    peut reprendre exactement où il en était plutôt que de tout perdre.
    cle_suivi identifie l'entrée en attente à retirer de l'état une fois
    terminée (pos_id n'est pas toujours unique/présent)."""
    moment_fermeture_ts = moment_fermeture_ts or time.time()
    moment_fin = moment_fermeture_ts + DUREE_SUIVI_POST_STOP_MIN * 60

    prix_palier1 = None
    if position and capital:
        lock1 = round(capital * LOCK_PALIERS_PCT[0] / 100, 2)
        if direction == "ACHAT":
            prix_palier1 = round(prix_entree * (1 + lock1 / position), 8)
        else:
            prix_palier1 = round(prix_entree * (1 - lock1 / position), 8)

    meilleur_prix = prix_stop_reel  # le plus favorable observé (sens inverse du stop)
    palier1_atteint = False
    intervalle_sec = 30

    while time.time() < moment_fin:
        await asyncio.sleep(min(intervalle_sec, max(1, moment_fin - time.time())))
        try:
            prix_tick = await get_prix_actuel(session, symbole)
        except Exception:
            continue
        if prix_tick is None:
            continue
        if direction == "ACHAT":
            if prix_tick > meilleur_prix:
                meilleur_prix = prix_tick
        else:
            if prix_tick < meilleur_prix:
                meilleur_prix = prix_tick
        if prix_palier1 is not None and not palier1_atteint:
            if direction == "ACHAT" and prix_tick >= prix_palier1:
                palier1_atteint = True
            elif direction == "VENTE" and prix_tick <= prix_palier1:
                palier1_atteint = True
    # ── Fenêtre déjà entièrement écoulée au moment de l'appel (reprise après
    # redémarrage, bot resté hors ligne plus de 15 min) : la boucle
    # ci-dessus ne s'exécute alors aucune fois — on passe directement à la
    # lecture finale, avec l'info honnête que la fenêtre d'observation a pu
    # être plus longue que prévu.

    try:
        prix_apres = await get_prix_actuel(session, symbole)
    except Exception as e:
        log.error(f"  ❌ [SUIVI-POST-STOP] Échec lecture prix final pour {symbole} : {e}")
        prix_apres = meilleur_prix
    if prix_apres is None or not prix_stop_reel:
        log.error(f"  ❌ [SUIVI-POST-STOP] {symbole} : impossible de conclure "
                  f"(prix_apres={prix_apres}, prix_stop_reel={prix_stop_reel}) — "
                  f"message non envoyé.")
        _retirer_suivi_post_stop_en_attente(etat_global, cle_suivi)
        return

    variation_post_pct = round((prix_apres - prix_stop_reel) / prix_stop_reel * 100, 3)
    if direction == "ACHAT":
        stop_bien_place    = prix_apres < prix_stop_reel
        a_recupere_entree  = prix_apres >= prix_entree
    else:
        stop_bien_place    = prix_apres > prix_stop_reel
        a_recupere_entree  = prix_apres <= prix_entree

    verdict = ("✅ Le prix a continué dans le sens du stop — stop bien placé"
               if stop_bien_place else
               "⚠️ Le prix est reparti en sens inverse — stop peut-être trop serré")
    if a_recupere_entree:
        verdict += " (et même repassé le prix d'entrée initial)"

    bloc_palier1 = ""
    if prix_palier1 is not None:
        if palier1_atteint:
            bloc_palier1 = ("\n🎯 Le prix a franchi le niveau du 1er palier de gain "
                             "à un moment de cette fenêtre — le trade serait devenu "
                             "gagnant si on avait tenu.")
        else:
            bloc_palier1 = ("\n🚫 Le prix n'a JAMAIS atteint le niveau du 1er palier "
                             "pendant cette fenêtre — tenir n'aurait pas suffi (pour "
                             "l'instant, sur la durée observée).")

    log.info(f"  📐 [SUIVI-POST-STOP] {symbole} : {DUREE_SUIVI_POST_STOP_MIN}min après le stop, "
             f"prix {prix_stop_reel} → {prix_apres} ({variation_post_pct:+.3f}%). {verdict} "
             f"| Palier1 atteint : {palier1_atteint if prix_palier1 is not None else 'N/A'}")
    await telegram(session,
        f"📐 <b>SUIVI POST-STOP — {symbole}</b>\n"
        f"{DUREE_SUIVI_POST_STOP_MIN} min après la fermeture : prix {prix_stop_reel} → "
        f"{prix_apres} ({variation_post_pct:+.3f}%).\n"
        f"{verdict}"
        f"{bloc_palier1}\n"
        f"<i>Donnée conservée pour calibrer la distance du stop lors des prochaines "
        f"mises à jour.</i>"
    )

    # ── CORRECTIF (10/07) — trouvé en analysant un CSV où le suivi post-stop
    # ne s'enregistrait JAMAIS dans l'historique, même pour des trades très
    # anciens : le matching se faisait uniquement sur pos_id, qui peut être
    # None (échec de capture à l'ouverture, position orpheline reprise sans
    # capture...) — dans ce cas, la condition "pos_id is not None" excluait
    # silencieusement TOUTE mise à jour, pour toujours. cle_suivi (généré à
    # la planification, toujours présent) devient la clé de correspondance
    # PRIORITAIRE, fiable même quand pos_id est absent.
    entree_trouvee = False
    if cle_suivi is not None:
        for entree in etat_global.get("historique", []):
            if entree.get("cle_suivi") == cle_suivi:
                entree["suivi_post_stop_pct"] = variation_post_pct
                entree["stop_bien_place"]     = stop_bien_place
                entree["palier1_atteint_post_stop"] = palier1_atteint if prix_palier1 is not None else None
                entree_trouvee = True
                break
    if not entree_trouvee and pos_id is not None:
        for entree in etat_global.get("historique", []):
            if entree.get("pos_id") == pos_id:
                entree["suivi_post_stop_pct"] = variation_post_pct
                entree["stop_bien_place"]     = stop_bien_place
                entree["palier1_atteint_post_stop"] = palier1_atteint if prix_palier1 is not None else None
                entree_trouvee = True
                break
    if not entree_trouvee:
        log.error(f"  ❌ [SUIVI-POST-STOP] {symbole} : aucune entrée d'historique "
                  f"correspondante trouvée (cle_suivi={cle_suivi}, pos_id={pos_id}) — "
                  f"donnée calculée mais non rattachée au trade dans l'historique.")
    _retirer_suivi_post_stop_en_attente(etat_global, cle_suivi)
    sauvegarder_etat(etat_global)


def _retirer_suivi_post_stop_en_attente(etat_global, cle_suivi):
    """Retire l'entrée correspondante de suivis_post_stop_en_attente une fois
    le suivi terminé (succès ou échec) — pour ne pas la reprendre en double
    au prochain redémarrage. Sans effet si cle_suivi est None (compatibilité)."""
    if cle_suivi is None:
        return
    liste = etat_global.get("suivis_post_stop_en_attente", [])
    etat_global["suivis_post_stop_en_attente"] = [
        s for s in liste if s.get("cle") != cle_suivi
    ]


async def surveiller_et_fermer_trade(session, symbole, direction, mise, capital, position,
                                      prix_entree, stop_initial, objectif_final, stop_loss_eur,
                                      rsi_1h, details, inst_id, etat_global, debut_override=None,
                                      algo_id=None, taille_contrats=None, pos_id=None, algo_type=None):
    """Boucle de surveillance stop/lock/durée + fermeture réelle + resynchronisation
    capital + bookkeeping. Extrait de executer_trade pour être réutilisable aussi bien
    après une ouverture normale (executer_trade) qu'après une REPRISE de surveillance
    sur une position orpheline retrouvée ouverte au démarrage (voir
    reprendre_surveillance_position_orpheline) — même logique stop/lock/durée dans les
    deux cas, pas de code dupliqué ni de comportement différent selon l'origine du trade.
    debut_override permet, pour une position orpheline, de faire partir la durée max (6h)
    depuis l'ouverture RÉELLE de la position (cTime OKX) plutôt que depuis l'instant de
    la reprise — sinon une position déjà ouverte depuis 5h se verrait accorder 6h de plus.
    algo_id (si fourni) est l'identifiant du trailing stop natif posé côté OKX à
    l'ouverture (voir okx_placer_trailing_stop_natif) — suit le prix en continu côté
    serveur, sans repositionnement manuel de notre part. Annulé automatiquement dès
    que la boucle se termine, quel que soit le chemin de sortie, pour ne jamais laisser
    un ordre actif traîner sur l'instrument après la fin du trade (voir
    okx_annuler_trailing_stop). taille_contrats n'est plus utilisée dans cette boucle
    depuis le passage au trailing natif (07/07, 13:20) — conservée dans la signature
    pour compatibilité, sans effet."""
    debut                   = debut_override if debut_override is not None else time.time()
    dernier_log             = 0
    dernier_statut_telegram = 0
    pnl_max_atteint = 0.0
    lock_actuel     = 0.0
    resultat_final  = "PERDU"
    gain_final      = -stop_loss_eur
    prix_sortie     = prix_entree
    pnl             = 0.0
    duree           = 0
    dernier_check_upl = 0.0   # timestamp du dernier appel réussi à okx_pnl_reel_upl
    dernier_upl_connu = None  # dernière valeur upl obtenue, réutilisée entre deux checks
    dernier_check_existence = 0.0  # 07/07 (16:42) — throttling du check "trailing a-t-il déjà
                                     # fermé ?" : même limite que le check upl (INTERVALLE_CHECK_UPL_SEC),
                                     # sinon un pnl qui oscille autour du palier verrouillé
                                     # (bruit de prix normal) déclenchait ce check à CHAQUE tick de
                                     # 1s — spam de logs + risque réel de dépasser le rate limit
                                     # OKX (10 req/2s par compte, confirmé via la doc officielle).
    dernier_check_algo_vivant = 0.0  # 07/07 (relecture 2) — throttling du nouveau check
                                       # "l'ordre de protection est-il encore vivant chez OKX ?"
                                       # (voir okx_algo_order_est_actif) — même fréquence que les
                                       # autres checks, pour rester sous le rate limit.
    dernier_check_plancher_vivant = 0.0  # 08/07 (00:39) — même check que ci-dessus, mais pour
                                           # le plancher dur (ordre séparé du trailing) — sans ça,
                                           # sa fermeture pourrait passer inaperçue (aucun message
                                           # Telegram envoyé), signalé concrètement par Damien.
    hard_floor_algo_id       = None  # 07/07 (23:11) — ordre FIXE indépendant du trailing,
                                       # repositionné vers le haut à chaque palier "dur" (1 sur 2).
                                       # Jamais touché par la logique du trailing/bascule — un
                                       # second filet totalement séparé.
    dernier_index_plancher_dur = 0    # index (1-based) du dernier palier "dur" déjà posé, pour ne
                                       # jamais reposer deux fois le même plancher.
    dernier_index_plancher_alerte = 0  # 08/07 — dernier palier pour lequel l'échec de pose a déjà
                                         # été signalé sur Telegram, pour ne pas spammer à chaque
                                         # tentative (le bloc ci-dessous retente automatiquement
                                         # à CHAQUE tick tant que dernier_index_plancher_dur n'a
                                         # pas avancé — voir commentaire plus bas).
    dernier_retry_stop  = 0.0   # 08/07 — throttle du retry de pose du stop natif initial (voir
                                  # bloc "RETRY POSE STOP NATIF" plus bas dans la boucle)
    alerte_stop_absent_envoyee = False  # une seule alerte "pas de protection native" par trade,
                                          # pas une par tentative de retry
    ratio_prix_retry = (stop_loss_eur / position) if position else STOP_LOSS_PCT  # 08/07 —
                        # reconstruit ratio_prix ici (non transmis tel quel à cette fonction),
                        # pour recalculer un stop depuis le prix ACTUEL à chaque retry.

    # ── Breakeven anticipé (11/07) — voir SEUIL_BREAKEVEN_ANTICIPE_PCT.
    breakeven_anticipe_pose = False   # True une fois le stop remonté au breakeven avant le palier 1
    dernier_essai_breakeven = 0.0     # throttle des tentatives d'amendement (idem autres checks OKX)

    # ── Traçabilité de sortie (12/07) — comment le trade s'est fermé, pour
    # diagnostiquer sur données réelles. prix_sortie est déjà suivi (voir plus
    # haut) et sert à mesurer le gap éventuel au moment du stop.
    motif_sortie = "?"                 # STOP_NATIF / STOP_INTERNE / TRAILING / LOCK / PLANCHER_DUR /
                                       # PALIER_NON_TENABLE / DUREE_MAX / PROTECTION_DISPARUE

    # ── Boucle de surveillance — jusqu'au stop, au lock, ou 6h max
    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        # Priorité au prix ancré sur l'instId RÉELLEMENT détenu (MODE_REEL) —
        # garantit qu'on surveille exactement le contrat sur lequel la
        # position existe, et non celui que le dictionnaire de price-feed
        # symbole→instId pourrait pointer par erreur. En simulation pure
        # (inst_id absent), on garde l'ancienne méthode (cache WebSocket).
        # get_prix_reel_instid a été RETIRÉE d'ici (07/07, 09:55) : confirmé
        # par les logs qu'elle échoue SYSTÉMATIQUEMENT pour l'instId
        # d'exécution (rejet propre de l'API, code != "0", pas une
        # exception réseau) — même symptôme que le rejet WebSocket (60018,
        # "doesn't exist"). L'instId d'exécution fonctionne pour les
        # endpoints privés (ordres, positions, stop natif) mais pas pour
        # les endpoints de marché publics (WS tickers ET REST ticker). La
        # tentative ne faisait que retomber sur le prix du flux public à
        # chaque tick, en ajoutant une requête inutile et du bruit dans les
        # logs. Protection réelle contre une éventuelle divergence de prix :
        # le stop natif OKX (server-side, ne dépend pas de notre prix) et
        # la vérification upl avant tout LOCK (endpoint privé, confirmé
        # fonctionnel toute la nuit) — toutes deux intactes, inchangées.
        prix_actuel = await get_prix_actuel(session, symbole)
        if prix_actuel is None:
            continue

        prix_sortie = prix_actuel
        duree       = int((time.time() - debut) / 60)

        # ── Calcul PnL — PRIORITÉ au vrai PnL OKX (upl), pas au prix du
        # flux public (07/07, 13:04) : confirmé en conditions réelles que
        # l'écart entre le flux public et le contrat d'exécution peut être
        # important et DURABLE (pas juste un glitch d'une seconde) — ex:
        # ETHUSD, bot voyait -1.90€ pendant que la position réelle était à
        # +4.83 USDC au même instant. Avec l'ancien calcul (basé sur le
        # flux public), un trade réellement très profitable pouvait ne
        # JAMAIS déclencher de LOCK, puisque le suivi interne ne le
        # croyait jamais gagnant. okx_pnl_reel_upl lit directement
        # /api/v5/account/positions — la même source que la vérification
        # existante, mais utilisée ici en direct plutôt qu'en confirmation
        # a posteriori. Repli sur le calcul basé sur le flux public
        # uniquement si cette requête échoue (réseau, ou simulation pure).
        pnl = None
        if MODE_REEL and inst_id:
            maintenant_upl = time.time()
            if maintenant_upl - dernier_check_upl >= INTERVALLE_CHECK_UPL_SEC:
                upl_reel = await okx_pnl_reel_upl(session, inst_id)
                if upl_reel is not None:
                    dernier_upl_connu = round(upl_reel, 2)
                    dernier_check_upl = maintenant_upl
            pnl = dernier_upl_connu
        if pnl is None:
            if direction == "ACHAT":
                pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
            else:
                pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)

        if pnl > pnl_max_atteint:
            pnl_max_atteint = pnl

        # ── GARDE-FOU CRITIQUE (07/07, relecture complète #2) — vérifier que
        # la protection native (fixe ou trailing) est encore VIVANTE chez
        # OKX, pas seulement que la position existe encore. Angle mort
        # identifié : si l'ordre de protection disparaissait silencieusement
        # côté OKX (annulation/expiration/rejet après coup, plausible en
        # démo) SANS fermer la position, le bot continuait de suivre les
        # paliers normalement (son propre calcul de PnL est indépendant) en
        # croyant être protégé — jusqu'à un effondrement de prix qu'aucun
        # stop ne rattrapait plus. Exactement le symptôme rapporté : paliers
        # 1, 2, 3 franchis puis chute brutale en négatif, sans fermeture.
        # PRIORITÉ ABSOLUE : vérifié avant toute autre logique stop/lock.
        if MODE_REEL and inst_id and algo_id:
            maintenant_algo = time.time()
            if maintenant_algo - dernier_check_algo_vivant >= INTERVALLE_CHECK_UPL_SEC:
                dernier_check_algo_vivant = maintenant_algo
                algo_vivant = await okx_algo_order_est_actif(session, inst_id, algo_id, algo_type)
                if algo_vivant is False:
                    # Ordre introuvable dans les ordres en attente : soit il
                    # a déclenché (position fermée), soit il a disparu sans
                    # fermer (danger). On distingue les deux cas.
                    position_encore_ouverte = await okx_position_existe_deja(
                        session, inst_id, contexte="trailing"
                    )
                    if position_encore_ouverte is True:
                        log.error(f"  🚨 [ALGO-VIVANT] {symbole} : la protection native "
                                  f"(algoId={algo_id}, type={algo_type}) a DISPARU sans "
                                  f"fermer la position — fermeture de sécurité immédiate.")
                        await telegram(session,
                            f"🚨 <b>ALERTE — PROTECTION DISPARUE</b>\n"
                            f"{symbole} : l'ordre de protection natif OKX n'existe plus, "
                            f"mais la position est toujours ouverte.\n"
                            f"Fermeture de sécurité immédiate au PnL connu actuel "
                            f"({'+' if pnl>=0 else ''}{pnl:.2f}€) plutôt que de rester "
                            f"exposé sans aucune protection."
                        )
                        frais   = calc_frais(position)
                        pnl_net = round(pnl - frais["total"], 4)
                        resultat_final = "GAGNE" if pnl_net > 0 else "PERDU"
                        gain_final     = pnl_net
                        algo_id        = None  # déjà disparu, rien à annuler plus bas
                        motif_sortie   = "PROTECTION_DISPARUE"
                        break
                    # Sinon : position_encore_ouverte est False (fermée
                    # normalement par la protection avant de disparaître de
                    # la liste) ou None (échec réseau) — rien à faire ici,
                    # les branches stop/lock plus bas gèrent déjà la
                    # fermeture normale via leur propre détection.

        # ── Check équivalent pour le PLANCHER DUR (08/07, 00:39) — ordre
        # séparé du trailing (voir plus bas), donc jamais couvert par le
        # check ci-dessus. Sans ce check dédié, une fermeture par le
        # plancher pouvait passer inaperçue : le check 'atteint_stop' (plus
        # bas) compare au stop FIXE d'ORIGINE, bien plus bas que le
        # plancher — ne se déclenche pas ; le check LOCK dépend du PnL
        # interne, qui peut mettre du temps à refléter la fermeture réelle.
        # Résultat concret signalé : trade fermé sur OKX sans aucun message
        # Telegram de fermeture. Si le plancher a disparu SANS fermer la
        # position, ce n'est pas une urgence (le trailing protège toujours
        # séparément) — on se contente de l'oublier proprement.
        if MODE_REEL and inst_id and hard_floor_algo_id:
            maintenant_plancher = time.time()
            if maintenant_plancher - dernier_check_plancher_vivant >= INTERVALLE_CHECK_UPL_SEC:
                dernier_check_plancher_vivant = maintenant_plancher
                plancher_vivant = await okx_algo_order_est_actif(
                    session, inst_id, hard_floor_algo_id, "fixe"
                )
                if plancher_vivant is False:
                    position_encore_ouverte = await okx_position_existe_deja(
                        session, inst_id, contexte="trailing"
                    )
                    if position_encore_ouverte is False:
                        log.info(f"  ℹ️ [PLANCHER-DUR] {symbole} fermé par le plancher dur — "
                                 f"réconciliation à partir du dernier PnL connu ({pnl:.2f}€).")
                        frais    = calc_frais(position)
                        gain_net = round(pnl - frais["total"], 4)
                        await telegram(session,
                            f"🧱 <b>SORTIE (plancher dur)</b>\n"
                            f"{symbole} | {direction}\n"
                            f"Fermée automatiquement par le plancher verrouillé.\n"
                            f"Dernier PnL connu avant fermeture : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                            f"Frais (ouv+ferm) : -{frais['total']}€\n"
                            f"Estimation avant vérification OKX : {'+' if gain_net>=0 else ''}{gain_net}€\n"
                            f"PnL max : +{pnl_max_atteint:.2f}€\n"
                            f"Durée : {duree} min"
                        )
                        resultat_final    = "GAGNE" if gain_net > 0 else "PERDU"
                        gain_final        = gain_net
                        hard_floor_algo_id = None  # déjà disparu, rien à annuler plus bas
                        motif_sortie       = "PLANCHER_DUR"
                        break
                    # Position encore ouverte : le plancher a disparu sans
                    # fermer (rare) — pas d'urgence, le trailing protège
                    # toujours séparément. On oublie juste ce plancher.
                    else:
                        log.warning(f"  ⚠️ [PLANCHER-DUR] {symbole} : le plancher a disparu sans "
                                    f"fermer la position — le trailing reste actif comme filet.")
                        hard_floor_algo_id = None


        nouveau_lock, index_lock = get_palier_lock_index(pnl_max_atteint, capital)
        if nouveau_lock > lock_actuel:
            etait_zero_avant = (lock_actuel == 0.0)  # vrai seulement au tout premier palier franchi
            lock_actuel = nouveau_lock
            log.info(f"  LOCK {lock_actuel}€ GARANTI [{symbole}] (PnL max={pnl_max_atteint:.2f}€)")
            await telegram(session,
                f"🔒 <b>{lock_actuel}€ garanti !</b>\n"
                f"{symbole} | PnL max : +{pnl_max_atteint:.2f}€\n"
                f"Gain verrouillé ✅"
            )

            # ── Bascule UNIQUE (07/07, 14:35) : au tout premier palier
            # franchi seulement (etait_zero_avant), on passe du stop fixe
            # au trailing natif, avec un écart plus serré
            # (TRAIL_RATIO_POST_PALIER1) qui protège le petit gain déjà
            # acquis. Une seule bascule pour tout le trade — pas de
            # repositionnement à chaque palier suivant, qui restent
            # purement informatifs désormais. Ordre des opérations
            # important : on POSE d'abord le nouveau trailing, et on
            # n'annule l'ancien stop fixe QUE si la pose a réussi — jamais
            # de fenêtre sans aucune protection active.
            if etait_zero_avant and MODE_REEL and inst_id and taille_contrats and algo_type == "fixe":
                side_ouverture = "buy" if direction == "ACHAT" else "sell"
                nouvel_algo_id = await okx_placer_trailing_stop_natif(
                    session, inst_id, side_ouverture, taille_contrats, TRAIL_RATIO_POST_PALIER1
                )
                if nouvel_algo_id:
                    if algo_id:
                        await okx_annuler_ordre_algo(session, inst_id, algo_id)
                    algo_id   = nouvel_algo_id
                    algo_type = "trailing"
                    log.info(f"  🛡️ [BASCULE] Trailing natif activé pour {symbole} après le premier "
                             f"palier (écart {TRAIL_RATIO_POST_PALIER1*100:.2f}%).")
                    await telegram(session,
                        f"🛡️ <b>TRAILING STOP ACTIVÉ</b>\n"
                        f"{symbole} : premier palier atteint — le stop fixe laisse place au "
                        f"trailing natif (écart {TRAIL_RATIO_POST_PALIER1*100:.2f}% depuis le "
                        f"meilleur niveau atteint), qui protège ce gain en continu pour le reste "
                        f"du trade."
                    )
                else:
                    log.warning(f"  ⚠️ [BASCULE] Échec de la bascule vers le trailing pour {symbole} "
                                f"— le stop fixe initial reste actif comme filet.")

            # ── PLANCHER DUR (07/07, 23:11 ; règle mise à jour le 08/07 à la
            # demande de Damien) : en plus du trailing natif ci-dessus (qu'on
            # ne touche JAMAIS ici), un SECOND ordre indépendant — un stop
            # FIXE classique — se repositionne vers le haut à chaque palier
            # concerné par palier_pose_plancher_dur() : les TROIS premiers
            # paliers (1, 2, 3) d'office, puis un palier sur deux à partir du
            # 4e (index impair : 5, 7, 9...). Le tout premier palier pose donc
            # à la fois la bascule vers le trailing (bloc ci-dessus) ET ce
            # plancher dur, au même niveau. Une fois posé, ce plancher ne
            # bouge plus tant que le palier suivant concerné n'est pas
            # atteint : le prix ne peut plus jamais redescendre en dessous,
            # quoi que fasse le trailing au-dessus. Ordre des opérations,
            # comme pour la bascule : on pose le NOUVEAU plancher d'abord, on
            # n'annule l'ANCIEN plancher (pas le trailing !) que si la pose a
            # réussi — jamais de fenêtre sans protection.
            if (palier_pose_plancher_dur(index_lock) and index_lock > dernier_index_plancher_dur
                    and MODE_REEL and inst_id and taille_contrats):
                # ── BREAKEVEN au 1er palier (10/07, demandé par Damien, suite
                # à l'analyse du ratio gain/perte 3,1x sur 26 trades) : au lieu
                # de verrouiller tout de suite le petit gain du palier 1
                # (~1€), on neutralise juste le RISQUE — le plancher se pose au
                # prix d'entrée (+ une petite marge pour couvrir les frais),
                # sans capturer le gain. Ça laisse la place au prix de
                # continuer vers un vrai gain proportionné au risque pris
                # (jusqu'au niveau du stop, 0.75%), au lieu de couper
                # systématiquement à la première petite avance. Les paliers
                # suivants (2+) reprennent le verrouillage normal du gain
                # réellement atteint.
                est_breakeven = (index_lock == 1)
                if est_breakeven:
                    tampon_frais = OKX_TAKER_FEE * 2 * 1.15  # petite marge au-dessus du coût réel des 2 frais
                    if direction == "ACHAT":
                        prix_plancher = round(prix_entree * (1 + tampon_frais), 8)
                        niveau_deja_depasse = prix_actuel <= prix_plancher
                    else:
                        prix_plancher = round(prix_entree * (1 - tampon_frais), 8)
                        niveau_deja_depasse = prix_actuel >= prix_plancher
                elif direction == "ACHAT":
                    prix_plancher = round(prix_entree * (1 + lock_actuel / position), 8)
                    niveau_deja_depasse = prix_actuel <= prix_plancher
                else:
                    prix_plancher = round(prix_entree * (1 - lock_actuel / position), 8)
                    niveau_deja_depasse = prix_actuel >= prix_plancher

                # ── CHANGEMENT DE CAP (08/07, demandé explicitement par Damien,
                # suite à l'incident HYPEUSD) : l'ancienne version plafonnait
                # discrètement le niveau visé quand il devenait injoignable (prix
                # déjà passé de l'autre côté), et annonçait quand même le montant
                # NOMINAL du palier — un plancher "5,45€" qui n'en garantissait
                # en réalité qu'une partie, jamais signalé clairement. Un
                # plancher qui peut être silencieusement réduit n'est pas un
                # vrai plancher. Nouvelle règle : si le niveau visé n'est plus
                # atteignable au moment de la pose, on ne pose PAS un plancher
                # affaibli — on ferme IMMÉDIATEMENT la position au marché, pour
                # capturer le meilleur prix encore disponible tout de suite,
                # plutôt que de laisser un ordre affaibli exposé à une
                # dégradation supplémentaire pendant qu'on attend son
                # déclenchement. Le nettoyage commun après la boucle (plus bas)
                # s'occupe de la fermeture réelle et de la vérification posId —
                # aucune duplication de logique de fermeture ici.
                if niveau_deja_depasse:
                    # ── CORRECTIF (08/07) — incident réel confirmé sur HYPEUSD : la
                    # fermeture d'urgence attendait le nettoyage générique après la
                    # boucle (annulation du stop, annulation du plancher, PUIS
                    # fermeture) — plusieurs appels réseau séquentiels, chacun
                    # ajoutant de la latence pendant laquelle le prix continue de
                    # bouger (précisément parce qu'on est dans le cas où il bouge
                    # déjà trop vite). Résultat observé : "PnL au moment de la
                    # décision" +1.09€, mais net réel seulement +0.21€ — l'écart
                    # venait en bonne partie de cette latence évitable. Priorité
                    # absolue maintenant : fermer la position TOUT DE SUITE, avant
                    # même d'envoyer le message Telegram — okx_fermer_position est
                    # sûre à appeler même si un ordre stop/plancher existe encore
                    # (reduce-only, jamais de double fermeture), et le nettoyage
                    # générique après la boucle annulera les ordres devenus inutiles
                    # sans effet néfaste (position déjà fermée = no-op silencieux).
                    log.error(f"  🚨 [PLANCHER-DUR] {symbole} : niveau du palier #{index_lock} "
                              f"({lock_actuel}€, prix cible={prix_plancher}) déjà dépassé par le "
                              f"prix actuel ({prix_actuel}) — fermeture immédiate au marché "
                              f"(priorité absolue, avant même les messages) plutôt que de poser un "
                              f"plancher affaibli ou d'attendre le nettoyage générique.")
                    if inst_id:
                        await okx_fermer_position(session, inst_id)
                    frais_urgence   = calc_frais(position)
                    gain_final      = round(pnl - frais_urgence["total"], 4)
                    resultat_final  = "GAGNE" if gain_final > 0 else "PERDU"
                    await telegram(session,
                        f"🚨 <b>FERMETURE IMMÉDIATE — PALIER NON TENABLE</b>\n"
                        f"{symbole} : le niveau du palier #{index_lock} ({lock_actuel}€) n'était "
                        f"déjà plus atteignable au moment de la pose (prix trop rapide).\n"
                        f"Fermeture envoyée immédiatement, en priorité absolue, pour capturer le "
                        f"meilleur prix encore disponible.\n"
                        f"PnL au moment de la décision (estimation, avant vérification OKX) : "
                        f"{'+' if pnl>=0 else ''}{pnl:.2f}€"
                    )
                    motif_sortie = "PALIER_NON_TENABLE"
                    break

                # ── Le gain réellement garanti correspond à la cible nominale
                # du palier (lock_actuel) pour les paliers 2+, ou à ~0€
                # (breakeven, tampon frais inclus) pour le tout premier —
                # voir est_breakeven ci-dessus.
                if est_breakeven:
                    if direction == "ACHAT":
                        gain_reel_plancher = round((prix_plancher / prix_entree - 1) * position, 2)
                    else:
                        gain_reel_plancher = round((1 - prix_plancher / prix_entree) * position, 2)
                else:
                    gain_reel_plancher = lock_actuel
                plancher_reduit    = False

                # ── Amendement en place EN PRIORITÉ (08/07) : si un plancher
                # existe déjà (pas le tout premier), on tente de modifier son
                # prix directement via /trade/amend-algos — 1 seul appel,
                # aucune fenêtre sans protection. Repli automatique sur
                # l'ancien mécanisme pose+annulation si l'amendement échoue
                # (ex: non supporté sur ce type de compte) ou s'il n'y a pas
                # encore de plancher à amender.
                plancher_amende = False
                if hard_floor_algo_id:
                    side_ouverture_amend = "buy" if direction == "ACHAT" else "sell"
                    plancher_amende = await okx_amender_stop_floor(
                        session, inst_id, hard_floor_algo_id, prix_plancher, side_ouverture_amend
                    )

                if plancher_amende:
                    # ── VALIDATION ACTIVE (08/07, demandé par Damien — "il ne doit
                    # pas avoir d'erreur, il faut qu'il soit validé") : un code
                    # "0" (succès) renvoyé par OKX à la requête d'amendement ne
                    # garantit pas que l'ordre est RÉELLEMENT vivant ensuite (rejet
                    # asynchrone possible côté OKX). On le reconfirme explicitement
                    # via okx_algo_order_est_actif avant de déclarer la protection
                    # posée — c'est cette vérification, pas seulement le code
                    # HTTP, qui déclenche le message "PLANCHER VERROUILLÉ".
                    plancher_confirme = await okx_algo_order_est_actif(
                        session, inst_id, hard_floor_algo_id, "fixe"
                    )
                else:
                    plancher_confirme = False

                if plancher_amende and plancher_confirme:
                    dernier_index_plancher_dur = index_lock
                    etat_global["nb_plancher_amende"] = etat_global.get("nb_plancher_amende", 0) + 1
                    log.info(f"  🧱 [PLANCHER-DUR] {symbole} : plancher amendé ET confirmé actif "
                             f"à {gain_reel_plancher}€ (cible palier #{index_lock}: {lock_actuel}€, "
                             f"prix={prix_plancher})")
                    avertissement_reduit = (
                        f"\n⚠️ <i>Réduit depuis la cible du palier ({lock_actuel}€) car le prix a "
                        f"bougé avant la pose — niveau plafonné pour rester valide.</i>"
                        if plancher_reduit else ""
                    )
                    titre_plancher = (
                        f"🧱 <b>RISQUE NEUTRALISÉ (breakeven, ~{gain_reel_plancher}€)</b>"
                        if est_breakeven else
                        f"🧱 <b>PLANCHER VERROUILLÉ : {gain_reel_plancher}€</b>"
                    )
                    texte_breakeven = (
                        f"\n<i>Palier 1 : le risque est neutralisé (pas de perte possible), le "
                        f"gain n'est PAS encore capturé — le trailing continue de suivre pour "
                        f"viser un vrai gain proportionné.</i>"
                        if est_breakeven else ""
                    )
                    await telegram(session,
                        f"{titre_plancher}\n"
                        f"{symbole} : ce niveau ne peut plus être franchi vers le bas, quoi qu'il "
                        f"arrive au trailing au-dessus.\n"
                        f"<i>Méthode : amendement en place, confirmé actif côté OKX</i>"
                        f"{avertissement_reduit}"
                        f"{texte_breakeven}"
                    )
                else:
                    side_ouverture_plancher = "buy" if direction == "ACHAT" else "sell"
                    nouveau_plancher_id = await okx_placer_ordre_stop_algo(
                        session, inst_id, side_ouverture_plancher, taille_contrats, prix_plancher
                    )
                    # ── Même principe de validation active pour la pose classique.
                    nouveau_plancher_confirme = False
                    if nouveau_plancher_id:
                        nouveau_plancher_confirme = await okx_algo_order_est_actif(
                            session, inst_id, nouveau_plancher_id, "fixe"
                        )

                    if nouveau_plancher_id and nouveau_plancher_confirme:
                        if hard_floor_algo_id:
                            # ── CORRECTIF (08/07) — cette annulation n'était jusqu'ici
                            # JAMAIS vérifiée : si elle échouait silencieusement côté
                            # OKX, l'ancien plancher restait actif et orphelin pendant
                            # que hard_floor_algo_id pointait déjà vers le nouveau —
                            # confirmé en conditions réelles (3 ordres SL identiques
                            # accumulés sur un seul trade ETHUSD après le passage à
                            # "paliers 1/2/3 posent tous un plancher"). Un essai
                            # supplémentaire avant d'abandonner, puis alerte explicite
                            # avec l'algoId orphelin si ça persiste — jamais silencieux.
                            ancien_annule = await okx_annuler_ordre_algo(session, inst_id, hard_floor_algo_id)
                            if not ancien_annule:
                                ancien_annule = await okx_annuler_ordre_algo(session, inst_id, hard_floor_algo_id)
                            if not ancien_annule:
                                log.error(f"  ⚠️ [PLANCHER-DUR] {symbole} : échec d'annulation de "
                                          f"l'ANCIEN plancher (algoId={hard_floor_algo_id}) après 2 "
                                          f"tentatives — probablement resté actif et orphelin côté "
                                          f"OKX, sans risque (reduce-only) mais à nettoyer manuellement.")
                                await telegram(session,
                                    f"⚠️ <b>ANCIEN PLANCHER PEUT-ÊTRE ORPHELIN</b>\n"
                                    f"{symbole} : l'ancien plancher (algoId={hard_floor_algo_id}) "
                                    f"n'a pas pu être annulé après 2 tentatives.\n"
                                    f"Sans danger (reduce-only, ne peut pas ouvrir de position ni "
                                    f"dépasser la taille réelle) mais pense à l'annuler manuellement "
                                    f"sur OKX si tu le vois encore traîner."
                                )
                        hard_floor_algo_id         = nouveau_plancher_id
                        dernier_index_plancher_dur = index_lock
                        etat_global["nb_plancher_repositionne"] = (
                            etat_global.get("nb_plancher_repositionne", 0) + 1
                        )
                        log.info(f"  🧱 [PLANCHER-DUR] {symbole} : plancher repositionné ET "
                                 f"confirmé actif (pose+annulation) à {gain_reel_plancher}€ "
                                 f"(cible palier #{index_lock}: {lock_actuel}€, prix={prix_plancher})")
                        avertissement_reduit = (
                            f"\n⚠️ <i>Réduit depuis la cible du palier ({lock_actuel}€) car le prix a "
                            f"bougé avant la pose — niveau plafonné pour rester valide.</i>"
                            if plancher_reduit else ""
                        )
                        titre_plancher2 = (
                            f"🧱 <b>RISQUE NEUTRALISÉ (breakeven, ~{gain_reel_plancher}€)</b>"
                            if est_breakeven else
                            f"🧱 <b>PLANCHER VERROUILLÉ : {gain_reel_plancher}€</b>"
                        )
                        texte_breakeven2 = (
                            f"\n<i>Palier 1 : le risque est neutralisé (pas de perte possible), le "
                            f"gain n'est PAS encore capturé — le trailing continue de suivre pour "
                            f"viser un vrai gain proportionné.</i>"
                            if est_breakeven else ""
                        )
                        await telegram(session,
                            f"{titre_plancher2}\n"
                            f"{symbole} : ce niveau ne peut plus être franchi vers le bas, quoi "
                            f"qu'il arrive au trailing au-dessus.\n"
                            f"<i>Méthode : repositionnement classique, confirmé actif côté OKX "
                            f"(l'amendement direct n'était pas possible ici)</i>"
                            f"{avertissement_reduit}"
                            f"{texte_breakeven2}"
                        )
                    else:
                        # ── Nettoyage (08/07) : si l'ordre a bien été accepté par OKX
                        # (nouveau_plancher_id non nul) mais que la vérification
                        # d'activité échoue ensuite (aléa réseau sur ce 2e appel,
                        # rejet asynchrone...), on l'annule explicitement plutôt que
                        # de le laisser traîner orphelin sur OKX pendant que le
                        # prochain tick en repose un autre.
                        if nouveau_plancher_id and not nouveau_plancher_confirme:
                            await okx_annuler_ordre_algo(session, inst_id, nouveau_plancher_id)

                        # ── CORRECTIF (08/07) — cet échec était auparavant SILENCIEUX
                        # (log.warning uniquement, jamais remonté sur Telegram). Passé
                        # en log.error (capturé aussi par le miroir d'erreurs vers
                        # Telegram) + alerte immédiate dédiée. IMPORTANT : comme
                        # dernier_index_plancher_dur n'avance PAS ici, ce bloc entier
                        # sera retenté automatiquement au TICK SUIVANT (CHECK_INTERVAL,
                        # 1s) — jusqu'à validation effective ou franchissement du
                        # palier suivant. dernier_index_plancher_alerte évite de
                        # spammer Telegram à chaque nouvelle tentative pour le MÊME
                        # palier : une seule alerte, puis silence jusqu'à validation
                        # ou nouveau palier.
                        if index_lock != dernier_index_plancher_alerte:
                            dernier_index_plancher_alerte = index_lock
                            log.error(f"  ⚠️ [PLANCHER-DUR] Échec de pose/validation pour {symbole} "
                                      f"sur le palier #{index_lock} ({lock_actuel}€) — nouvelle "
                                      f"tentative automatique au prochain tick. L'ancien plancher "
                                      f"(s'il existe) reste actif entretemps.")
                            await telegram(session,
                                f"⚠️ <b>ÉCHEC POSE PLANCHER — NOUVELLE TENTATIVE EN COURS</b>\n"
                                f"{symbole} : le plancher au palier #{index_lock} ({lock_actuel}€) "
                                f"n'a pas pu être posé/confirmé côté OKX.\n"
                                f"Le bot retente automatiquement à chaque seconde tant que ce "
                                f"n'est pas validé. L'ancien plancher (s'il existe) reste actif "
                                f"entretemps — une confirmation 🧱 suivra dès que ce sera bon."
                            )

        # ── BREAKEVEN ANTICIPÉ (11/07) — voir SEUIL_BREAKEVEN_ANTICIPE_PCT.
        # Dès que le PnL max a atteint le seuil (+0.15% du prix d'entrée) et TANT
        # QUE le palier 1 n'est pas encore franchi (lock_actuel == 0.0 — au-delà,
        # la bascule vers le trailing + le plancher dur ont déjà pris le relais
        # plus haut, avec leur propre breakeven), on remonte le stop au prix
        # d'entrée + tampon frais. But : un trade parti à contresens qui a quand
        # même montré un petit gain ressort à ~0€ au lieu d'une perte pleine.
        # Le prix de breakeven est calculé EXACTEMENT comme celui du palier 1
        # (même tampon_frais) pour une transition cohérente. On met aussi à jour
        # stop_initial, pour que le filet interne (atteint_stop, plus bas)
        # protège au même niveau que l'ordre natif amendé.
        if (not breakeven_anticipe_pose and lock_actuel == 0.0
                and pnl_max_atteint >= position * SEUIL_BREAKEVEN_ANTICIPE_PCT):
            tampon_frais_be = OKX_TAKER_FEE * 2 * 1.15  # identique au breakeven du palier 1
            if direction == "ACHAT":
                prix_breakeven_anticipe = round(prix_entree * (1 + tampon_frais_be), 8)
            else:
                prix_breakeven_anticipe = round(prix_entree * (1 - tampon_frais_be), 8)

            if MODE_REEL and inst_id and algo_id:
                # Amendement EN PLACE du stop fixe natif (1 seul appel, aucune
                # fenêtre sans protection) — réutilise le mécanisme du plancher
                # dur. Throttlé comme les autres appels OKX de la boucle : la
                # 1re tentative part immédiatement (dernier_essai_breakeven=0),
                # les éventuelles retentatives après échec sont espacées.
                maintenant_be = time.time()
                if maintenant_be - dernier_essai_breakeven >= INTERVALLE_CHECK_UPL_SEC:
                    dernier_essai_breakeven = maintenant_be
                    side_ouverture_be = "buy" if direction == "ACHAT" else "sell"
                    be_ok = await okx_amender_stop_floor(
                        session, inst_id, algo_id, prix_breakeven_anticipe, side_ouverture_be
                    )
                    if be_ok:
                        stop_initial = prix_breakeven_anticipe
                        breakeven_anticipe_pose = True
                        log.info(f"  🧱 [BREAKEVEN-ANTICIPÉ] {symbole} : +{pnl_max_atteint:.2f}€ atteint "
                                 f"(>= +{SEUIL_BREAKEVEN_ANTICIPE_PCT*100:.2f}%) — stop remonté au "
                                 f"breakeven ({prix_breakeven_anticipe}) avant le palier 1.")
                        await telegram(session,
                            f"🧱 <b>RISQUE NEUTRALISÉ (breakeven anticipé)</b>\n"
                            f"{symbole} : le trade a pris +{SEUIL_BREAKEVEN_ANTICIPE_PCT*100:.2f}% "
                            f"(PnL max +{pnl_max_atteint:.2f}€) — le stop remonte au prix d'entrée, "
                            f"avant même le 1er palier.\n"
                            f"Au pire, ce trade ressort maintenant à ~0€ au lieu d'une perte pleine."
                        )
                    else:
                        log.warning(f"  ⚠️ [BREAKEVEN-ANTICIPÉ] {symbole} : amendement du stop au "
                                    f"breakeven échoué — nouvelle tentative au prochain check (le stop "
                                    f"initial à -{STOP_LOSS_PCT*100:.2f}% reste actif entretemps).")
            elif not MODE_REEL:
                # Pure simulation (aucun ordre natif) : on remonte simplement le
                # filet interne — atteint_stop protégera au niveau breakeven.
                stop_initial = prix_breakeven_anticipe
                breakeven_anticipe_pose = True
                log.info(f"  🧱 [BREAKEVEN-ANTICIPÉ/SIM] {symbole} : +{pnl_max_atteint:.2f}€ atteint — "
                         f"stop interne remonté au breakeven ({prix_breakeven_anticipe}).")
            # (MODE_REEL mais algo_id encore None : stop natif pas encore posé —
            #  on ne fait rien ce tick, le bloc RETRY plus bas va le poser, puis
            #  ce bloc l'amendera au breakeven au tick suivant.)

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

        # Log toutes les minutes (détail interne, Railway)
        if time.time() - dernier_log >= 60:
            lock_flag = f" LOCK{lock_actuel}€" if lock_actuel > 0 else ""
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole} {prix_actuel} | "
                     f"PnL {'+' if pnl>=0 else ''}{pnl:.2f}€{lock_flag} | {duree}min")
            dernier_log = time.time()

        # ── Statut périodique sur Telegram (08/07, demandé par Damien — éviter
        # d'avoir à rouvrir Railway pour suivre un trade en cours) : même
        # information que le log ci-dessus, mais espacée de 15 min au lieu de
        # 1 min pour ne pas noyer le chat, même avec plusieurs trades ouverts
        # en simultané.
        if time.time() - dernier_statut_telegram >= INTERVALLE_STATUT_TELEGRAM_SEC:
            lock_flag_tg = f"\nPalier verrouillé : {lock_actuel}€" if lock_actuel > 0 else ""
            await telegram(session,
                f"📍 <b>STATUT — {symbole}</b>\n"
                f"Prix actuel : {prix_actuel} | Durée : {duree}min\n"
                f"PnL : {'+' if pnl>=0 else ''}{pnl:.2f}€ | PnL max : +{pnl_max_atteint:.2f}€"
                f"{lock_flag_tg}"
            )
            dernier_statut_telegram = time.time()

        # ── RETRY POSE STOP NATIF (08/07, demandé par Damien — "il ne doit pas
        # avoir d'erreur, il faut qu'il soit validé") : si la pose initiale a
        # échoué (algo_id est None — ex. rejet OKX 51302 "SL trigger price
        # cannot be lower than the mark price", confirmé en conditions
        # réelles lors d'un mouvement très rapide qui avait déjà dépassé le
        # niveau visé au moment de la pose), on ne se contente plus de la
        # seule surveillance interne (plus lente, exposée à plus de
        # slippage). On retente à intervalle régulier, en recalculant le
        # niveau depuis le prix ACTUEL (pas l'ancien, déjà obsolète) — avec
        # la même validation active que pour le plancher dur.
        if MODE_REEL and inst_id and taille_contrats and algo_id is None:
            maintenant_retry_stop = time.time()
            if maintenant_retry_stop - dernier_retry_stop >= INTERVALLE_CHECK_UPL_SEC:
                dernier_retry_stop = maintenant_retry_stop
                side_ouverture_retry = "buy" if direction == "ACHAT" else "sell"
                if direction == "ACHAT":
                    nouveau_stop_retry = round(prix_actuel * (1 - ratio_prix_retry), 8)
                else:
                    nouveau_stop_retry = round(prix_actuel * (1 + ratio_prix_retry), 8)
                nouvel_algo_id_retry = await okx_placer_ordre_stop_algo(
                    session, inst_id, side_ouverture_retry, taille_contrats, nouveau_stop_retry
                )
                retry_confirme = False
                if nouvel_algo_id_retry:
                    retry_confirme = await okx_algo_order_est_actif(
                        session, inst_id, nouvel_algo_id_retry, "fixe"
                    )
                if nouvel_algo_id_retry and retry_confirme:
                    algo_id      = nouvel_algo_id_retry
                    algo_type    = "fixe"
                    stop_initial = nouveau_stop_retry
                    log.info(f"  🛡️ [STOP-RETRY] {symbole} : stop natif posé ET confirmé actif "
                             f"({nouveau_stop_retry}, recalculé depuis le prix actuel après "
                             f"échec initial).")
                    await telegram(session,
                        f"🛡️ <b>STOP NATIF ACTIVÉ (après retry)</b>\n"
                        f"{symbole} : le stop a fini par se poser et être confirmé à "
                        f"{nouveau_stop_retry} (recalculé depuis le prix actuel, la pose "
                        f"initiale avait échoué)."
                    )
                else:
                    if nouvel_algo_id_retry and not retry_confirme:
                        # Accepté par OKX mais pas confirmé vivant ensuite — nettoyage,
                        # même principe que pour le plancher dur.
                        await okx_annuler_ordre_algo(session, inst_id, nouvel_algo_id_retry)
                    if not alerte_stop_absent_envoyee:
                        alerte_stop_absent_envoyee = True
                        log.error(f"  ⚠️ [STOP-RETRY] {symbole} : toujours aucune protection "
                                  f"native active — nouvelle tentative au prochain check. "
                                  f"Surveillance interne seule en attendant.")
                        await telegram(session,
                            f"⚠️ <b>AUCUNE PROTECTION NATIVE — TENTATIVES EN COURS</b>\n"
                            f"{symbole} : le stop natif n'a toujours pas pu être posé/confirmé.\n"
                            f"Le bot retente automatiquement toutes les {INTERVALLE_CHECK_UPL_SEC}s "
                            f"en recalculant depuis le prix actuel. La surveillance interne reste "
                            f"active entretemps comme filet de secours."
                        )

        if atteint_stop:
            # ── CORRECTIF (07/07, relecture complète) — exactement le même
            # principe que le correctif de la branche LOCK ci-dessous,
            # appliqué ici : atteint_stop se base sur prix_actuel, qui vient
            # du flux PUBLIC (get_prix_actuel), potentiellement divergent du
            # vrai prix que la protection native (fixe ou trailing) surveille
            # réellement. Sans ce garde-fou, un flux public qui s'effondre à
            # tort (la divergence confirmée cette nuit) aurait pu déclencher
            # une fermeture manuelle qui ANNULE une protection native saine
            # et clôture un trade qui allait très bien réellement. Comme pour
            # le LOCK : si un ordre natif est actif, on ne l'annule jamais
            # pour agir à sa place — on vérifie seulement s'il a déjà fermé.
            if algo_id and MODE_REEL and inst_id:
                maintenant_existence = time.time()
                if maintenant_existence - dernier_check_existence >= INTERVALLE_CHECK_UPL_SEC:
                    position_existe = await okx_position_existe_deja(session, inst_id, contexte="trailing")
                    dernier_check_existence = maintenant_existence
                    if position_existe is False:
                        frais   = calc_frais(position)
                        pnl_net = round(pnl - frais["total"], 4)
                        resultat_final = "GAGNE" if pnl_net > 0 else "PERDU"
                        log.info(f"\n  STOP [{symbole}] (natif, détecté) {'+' if pnl>=0 else ''}"
                                 f"{pnl:.2f}€ | net={pnl_net:+.4f}€ | {duree}min")
                        await telegram(session,
                            f"🛑 <b>STOP (natif)</b>\n"
                            f"{symbole} {direction}\n"
                            f"Fermée automatiquement par la protection native OKX.\n"
                            f"Dernier PnL connu : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                            f"Frais (ouv+ferm) : -{frais['total']}€\n"
                            f"Résultat net (estimation) : {'+' if pnl_net>=0 else ''}{pnl_net}€\n"
                            f"Durée : {duree} min"
                        )
                        gain_final = pnl_net
                        motif_sortie = "STOP_NATIF"
                        break
                # on ne l'annule jamais, on continue la boucle.
            else:
                # Pas de protection native active (les deux poses ont
                # échoué) — fermeture manuelle interne, seul filet
                # disponible dans ce cas précis.
                frais   = calc_frais(position)
                pnl_net = round(pnl - frais["total"], 4)
                resultat_final = "GAGNE" if pnl_net > 0 else "PERDU"
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
                motif_sortie = "STOP_INTERNE"
                break

        # Sortie lock : PnL redescend sous le palier verrouillé (mais le
        # stop n'est pas atteint, sinon on serait déjà sorti ci-dessus)
        if lock_actuel > 0 and pnl < lock_actuel:
            # ── CORRECTIF CRITIQUE (07/07, 15:58) — confirmé en conditions
            # réelles : quand un trailing natif protège déjà activement le
            # trade (côté serveur, sans latence), notre propre code
            # l'annulait pour fermer "à la main" via un ordre au marché sur
            # la base d'un pnl déjà vieux de quelques secondes. Pendant
            # l'annulation + le nouvel ordre, le prix a continué de
            # s'effondrer — résultat réel : -0,90€ net alors qu'on pensait
            # sécuriser +3,62€. Le trailing, lui, n'avait pas encore eu le
            # temps de se déclencher tout seul (son niveau était pourtant
            # meilleur) : on a désactivé la meilleure protection au pire
            # moment. Désormais, si un trailing est actif, on ne l'annule
            # JAMAIS pour agir à sa place — on vérifie seulement s'il a
            # DÉJÀ fait son travail (position fermée), et on se contente de
            # réconcilier dans ce cas. S'il n'a pas encore fermé, on ne
            # fait rien : il reste le seul et meilleur protecteur en
            # continu. Le stop fixe (avant la bascule) reste le seul cas
            # où la fermeture manuelle interne garde son utilité — sans
            # trailing actif, rien d'autre ne protège les paliers.
            if algo_type == "trailing" and MODE_REEL and inst_id:
                maintenant_existence = time.time()
                if maintenant_existence - dernier_check_existence >= INTERVALLE_CHECK_UPL_SEC:
                    position_existe = await okx_position_existe_deja(session, inst_id, contexte="trailing")
                    dernier_check_existence = maintenant_existence
                    if position_existe is False:
                        log.info(f"  ℹ️ [TRAILING] {symbole} déjà fermé par le trailing natif — "
                                 f"réconciliation à partir du dernier PnL connu ({pnl:.2f}€).")
                        frais    = calc_frais(position)
                        gain_net = round(pnl - frais["total"], 4)
                        await telegram(session,
                            f"🔒 <b>SORTIE (trailing natif)</b>\n"
                            f"{symbole} | {direction}\n"
                            f"Fermée automatiquement par le trailing stop natif.\n"
                            f"Dernier PnL connu avant fermeture : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                            f"Frais (ouv+ferm) : -{frais['total']}€\n"
                            f"Estimation avant vérification OKX : {'+' if gain_net>=0 else ''}{gain_net}€\n"
                            f"PnL max : +{pnl_max_atteint:.2f}€\n"
                            f"Durée : {duree} min"
                        )
                        resultat_final = "GAGNE" if gain_net > 0 else "PERDU"
                        gain_final     = gain_net
                        motif_sortie   = "TRAILING"
                        break
                # Position toujours ouverte (ou check pas encore dû à cause
                # du throttle) : le trailing n'a pas encore déclenché — on
                # ne fait RIEN, on continue simplement la boucle au
                # prochain tick, sans jamais l'annuler ni le remplacer par
                # une fermeture manuelle.
            elif lock_actuel > 0:
                # ── Pas de trailing actif ici (bascule échouée, ou stop
                # fixe encore en place) — fermeture manuelle interne, seul
                # filet disponible dans ce cas, avec le même garde-fou de
                # confirmation qu'avant.
                lock_confirme = True
                if MODE_REEL and inst_id:
                    upl_reel = await okx_pnl_reel_upl(session, inst_id)
                    if upl_reel is not None and upl_reel < -TOLERANCE_LOCK_UPL_EUR:
                        lock_confirme = False
                        log.warning(f"  ⚠️ [LOCK-BLOQUÉ] {symbole} : PnL interne={pnl:.2f}€ mais "
                                    f"upl RÉEL OKX={upl_reel:.2f} (négatif) — sortie LOCK annulée, "
                                    f"prix interne probablement désynchronisé. Stop toujours actif.")
                        await telegram(session,
                            f"⚠️ <b>LOCK BLOQUÉ PAR VÉRIFICATION</b>\n"
                            f"{symbole} : sortie LOCK à +{lock_actuel}€ envisagée, mais OKX indique "
                            f"un PnL réel négatif ({upl_reel:.2f}). Fermeture annulée par précaution — "
                            f"le stop reste actif en attendant."
                        )

                if lock_confirme:
                    frais    = calc_frais(position)
                    gain_net = round(pnl - frais["total"], 4)
                    log.info(f"\n  SORTIE LOCK [{symbole}] pnl={pnl:.2f}€ (lock garanti={lock_actuel}€, "
                             f"max={pnl_max_atteint:.2f}€) | {duree}min")
                    await telegram(session,
                        f"🔒 <b>SORTIE LOCK</b>\n"
                        f"{symbole} | {direction}\n"
                        f"Gain garanti (palier) : +{lock_actuel}€\n"
                        f"PnL réel au moment de la sortie : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                        f"Frais (ouv+ferm) : -{frais['total']}€\n"
                        f"Gain net : {'+' if gain_net>=0 else ''}{gain_net}€\n"
                        f"PnL max : +{pnl_max_atteint:.2f}€\n"
                        f"Durée : {duree} min"
                    )
                    resultat_final = "GAGNE" if gain_net > 0 else "PERDU"
                    gain_final     = gain_net
                    motif_sortie   = "LOCK"
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
            motif_sortie = "DUREE_MAX"
            break

    # ── Annulation du stop natif OKX actif (s'il en existe un) — AVANT
    # toute fermeture réelle, quel que soit le chemin de sortie emprunté.
    # HYBRIDE (07/07, 14:35) : le type d'ordre actif dépend de si la bascule
    # a eu lieu (voir plus haut) — 'fixe' utilise /trade/cancel-algos,
    # 'trailing' utilise /trade/cancel-advance-algos (endpoints DIFFÉRENTS,
    # confirmé via la doc officielle OKX : cancel-algos ne couvre pas les
    # ordres Trailing Stop). Un échec ici n'est pas bloquant (l'algo a déjà
    # pu se déclencher tout seul entre-temps, auquel cas il n'y a de toute
    # façon plus rien à annuler).
    if MODE_REEL and inst_id and algo_id:
        if algo_type == "trailing":
            await okx_annuler_trailing_stop(session, inst_id, algo_id)
        else:
            await okx_annuler_ordre_algo(session, inst_id, algo_id)

    # ── Annulation du plancher dur (07/07, 23:11) — ordre INDÉPENDANT du
    # trailing/stop ci-dessus, toujours de type 'conditional' (jamais
    # trailing), donc toujours annulé via okx_annuler_ordre_algo. Sans
    # cette annulation, un plancher resté actif pourrait se déclencher plus
    # tard sur un AUTRE trade ouvert ensuite sur le même marché — même
    # risque que pour l'algo principal.
    if MODE_REEL and inst_id and hard_floor_algo_id:
        await okx_annuler_ordre_algo(session, inst_id, hard_floor_algo_id)

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

    # ── Suivi post-stop CENTRALISÉ (09/07, demandé par Damien : "il faut que
    # je le poste stop de TOUS les trades qui ont été en négatif") — ici,
    # après la boucle, resultat_final est connu quelle que soit la CAUSE de
    # la fermeture (stop classique, plancher qui a quand même fini négatif,
    # durée max dépassée...). Couvre donc tous les cas de perte, pas
    # seulement la sortie stop-loss classique.
    cle_suivi = None
    if resultat_final == "PERDU":
        moment_fermeture_ts = time.time()
        cle_suivi = f"{symbole}_{pos_id}_{moment_fermeture_ts}"
        etat_global.setdefault("suivis_post_stop_en_attente", []).append({
            "cle":                 cle_suivi,
            "symbole":             symbole,
            "direction":           direction,
            "prix_stop_reel":      prix_actuel,
            "prix_entree":         prix_entree,
            "pos_id":              pos_id,
            "position":            position,
            "capital":             capital,
            "moment_fermeture_ts": moment_fermeture_ts,
        })
        sauvegarder_etat(etat_global)
        asyncio.create_task(suivre_prix_post_stop(
            session, symbole, direction, prix_actuel, prix_entree, pos_id, etat_global,
            position=position, capital=capital, moment_fermeture_ts=moment_fermeture_ts,
            cle_suivi=cle_suivi
        ))

    # ── Resynchronisation avec le VRAI solde OKX avant de mettre à jour le
    # capital — uniquement en MODE_REEL. Le calcul interne (capital +
    # gain_final) sert de solution de secours seulement si cette lecture
    # échoue (ex: coupure réseau), jamais comme source principale.
    solde_reel = None
    verif_reelle = None
    if MODE_REEL:
        await asyncio.sleep(1)  # laisse le temps à OKX de refléter la fermeture
        solde_brut = await okx_recuperer_solde_reel(session, "USDC")
        if solde_brut is not None:
            capital_actuel = etat_global["capital"]
            # Garde-fou de plausibilité : un solde qui s'écarte de plus de
            # 50% du capital actuel en un seul trade est presque certainement
            # une anomalie (ex: solde démo OKX par défaut sans rapport avec
            # les fonds réellement alloués), pas un vrai résultat de trade.
            # On le rejette plutôt que de laisser un chiffre aberrant fausser
            # toutes les mises futures.
            ecart_relatif = abs(solde_brut - capital_actuel) / capital_actuel if capital_actuel > 0 else 0
            if ecart_relatif > 0.5:
                log.error(f"  [SOLDE-RÉEL] ⚠️ Solde OKX invraisemblable rejeté : {solde_brut:.2f} USDC "
                          f"vs capital actuel {capital_actuel}€ (écart {ecart_relatif*100:.0f}%) — "
                          f"repli sur le calcul interne pour ce trade")
                await telegram(session,
                    f"⚠️ <b>SOLDE OKX INVRAISEMBLABLE IGNORÉ</b>\n"
                    f"OKX a renvoyé {solde_brut:.2f} USDC, très éloigné du capital actuel "
                    f"({capital_actuel}€) — probablement un solde démo par défaut sans rapport "
                    f"avec tes fonds réels. Ignoré par sécurité, calcul interne conservé."
                )
            else:
                solde_reel = solde_brut
                log.info(f"  [SOLDE-RÉEL] Capital resynchronisé sur OKX : {solde_reel:.2f} USDC "
                         f"(vs calcul interne : {round(capital_actuel + gain_final, 2)}€)")
        else:
            log.warning(f"  [SOLDE-RÉEL] Échec de lecture — repli sur le calcul interne pour ce trade")

        if inst_id:
            verif_reelle = await okx_recuperer_position_reelle(session, inst_id, pos_id_attendu=pos_id)
            if verif_reelle:
                # ── okx_recuperer_position_reelle vérifie déjà EXACTEMENT via
                # posId quand il a été capturé à l'ouverture (voir
                # okx_recuperer_pos_id) — si le posId ne correspond à aucun
                # dossier récent, la fonction renvoie déjà None et ce bloc
                # n'est jamais atteint. La comparaison de prix ci-dessous ne
                # s'applique donc plus QUE dans le cas restant : pos_id_attendu
                # absent (échec réseau à l'ouverture, ou position antérieure à
                # ce correctif) — voir matched_via_pos_id ci-dessous.
                open_px_dossier = verif_reelle.get("open_px")
                matched_via_pos_id = verif_reelle.get("matched_via_pos_id", False)
                dossier_correspond = True
                # ── CORRECTIF (08/07) : cette comparaison de prix est un second
                # filet APPROXIMATIF, utile uniquement quand le dossier a été pris
                # par approximation (pos_id_attendu absent ou non synchronisé). Si
                # matched_via_pos_id est True, le dossier est déjà confirmé EXACT
                # par l'identifiant unique OKX (posId) — le comparer quand même au
                # prix_entree interne du bot (qui peut lui-même être faussé par un
                # fort glissement ou une incohérence instId, comme observé sur un
                # trade ETHUSD réel : -21,59 USDC réels rapportés comme -0,54€)
                # revient à laisser une valeur approximative invalider une
                # certitude. Dans ce cas, on saute directement cette vérification.
                if not matched_via_pos_id and open_px_dossier:
                    try:
                        ecart_open_px = abs(float(open_px_dossier) - prix_entree) / prix_entree
                        if ecart_open_px > 0.005:
                            dossier_correspond = False
                    except (ValueError, TypeError, ZeroDivisionError):
                        dossier_correspond = False

                if not dossier_correspond:
                    log.error(f"  [VÉRIF-RÉELLE] ⚠️ Dossier position-history REJETÉ pour {symbole} "
                              f"(match approximatif, sans posId) : prix d'ouverture du dossier "
                              f"({open_px_dossier}) ne correspond pas au prix d'entrée interne "
                              f"({prix_entree}) — probablement le dossier d'un AUTRE trade sur ce "
                              f"marché. Calcul interne conservé, funding fee ignoré.")
                    await telegram(session,
                        f"⚠️ <b>VÉRIFICATION OKX IGNORÉE</b>\n"
                        f"{symbole} : dossier trouvé par approximation (posId indisponible), et "
                        f"son prix d'ouverture ne correspond pas à ce trade "
                        f"(prix d'ouverture {open_px_dossier} vs {prix_entree} attendu).\n"
                        f"Calcul interne conservé par précaution."
                    )
                    # ── CORRECTIF (relecture complète) — sans cette ligne, le
                    # dossier REJETÉ restait quand même dans verif_reelle, et
                    # le rapport "VÉRIFICATION RÉELLE OKX" plus bas (basé
                    # uniquement sur "if verif_reelle is not None") l'aurait
                    # quand même affiché — montrant les chiffres d'un AUTRE
                    # trade juste après avoir dit qu'on les ignorait.
                    verif_reelle = None
                else:
                    net_reel = round(verif_reelle["pnl"] - abs(verif_reelle["fee"])
                                      - abs(verif_reelle["funding_fee"]), 4)
                    gain_interne_original = gain_final  # conservé pour le message de comparaison, avant écrasement
                    log.info(f"  [VÉRIF-RÉELLE] {symbole} — OKX: pnl={verif_reelle['pnl']} "
                             f"frais_transaction={verif_reelle['fee']} "
                             f"frais_financement={verif_reelle['funding_fee']} "
                             f"net={net_reel} | Bot interne: {gain_final}")

                    # ── DIAGNOSTIC NON BLOQUANT (08/07) : même quand le dossier est
                    # confirmé exact via posId, on note l'écart entre le prix
                    # d'entrée interne (estimation post-fill, potentiellement
                    # faussée par un fort glissement ou une divergence instId
                    # feed/exécution) et le prix d'entrée réellement enregistré
                    # par OKX (open_px_dossier). Objectif : suivre dans le temps
                    # l'ampleur de ce phénomène récurrent SANS jamais lui
                    # permettre d'influencer le résultat officiel — seul un
                    # journal, jamais un filtre.
                    if matched_via_pos_id and open_px_dossier:
                        try:
                            ecart_diag = abs(float(open_px_dossier) - prix_entree) / prix_entree
                            if ecart_diag > 0.003:
                                log.warning(f"  🔬 [DIAG-PRIX] {symbole} : prix d'entrée interne "
                                            f"({prix_entree}) vs réel OKX confirmé posId "
                                            f"({open_px_dossier}) — écart {ecart_diag*100:.2f}% "
                                            f"(purement informatif, résultat officiel déjà basé "
                                            f"sur le dossier OKX).")
                        except (ValueError, TypeError, ZeroDivisionError):
                            pass

                    # Le résultat RÉEL OKX devient le résultat OFFICIEL du trade —
                    # élimine l'écart à la source plutôt que de juste le signaler.
                    # Garde-fou : rejeté si manifestement aberrant (gain plus grand
                    # que la position elle-même ne peut jamais arriver légitimement).
                    if abs(net_reel) <= position:
                        gain_final     = net_reel
                        resultat_final = "GAGNE" if net_reel > 0 else "PERDU"
                    else:
                        log.error(f"  [VÉRIF-RÉELLE] ⚠️ net_reel={net_reel} invraisemblable "
                                  f"(position={position}) — calcul interne conservé")

    # ── Libérer le marché + mise à jour état global dans un seul lock
    async with trades_lock:
        trades_ouverts.pop(symbole, None)
        cooldown_marches.pop(symbole, None)
        log.info(f"  [{symbole}] libéré")

        # Mise à jour capital et stats dans le même lock — pas de race condition
        etat_global["nb_trades"] = etat_global.get("nb_trades", 0) + 1
        numero_trade             = etat_global["nb_trades"]
        if solde_reel is not None:
            etat_global["capital"] = round(solde_reel, 2)  # source de vérité OKX
        else:
            etat_global["capital"] = round(etat_global["capital"] + gain_final, 2)  # repli
        etat_global["cumul_net"] = round(etat_global["capital"] - CAPITAL_INITIAL, 2)
        etat_global["pnl_jour"]  = round(etat_global.get("pnl_jour", 0) + gain_final, 2)

        # Alerte perte cumulée — se déclenche une seule fois au franchissement
        # du seuil (en % du capital initial), se réarme si le capital remonte
        # au-dessus (pour pouvoir réalerter en cas de nouvelle chute plus tard)
        alerte_perte_a_envoyer = False
        seuil_alerte_eur = round(CAPITAL_INITIAL * SEUIL_ALERTE_PERTE_PCT / 100, 2)
        if etat_global["capital"] <= CAPITAL_INITIAL - seuil_alerte_eur:
            if not etat_global.get("alerte_perte_envoyee", False):
                alerte_perte_a_envoyer = True
                etat_global["alerte_perte_envoyee"] = True
        else:
            etat_global["alerte_perte_envoyee"] = False

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

            if etat_global["pertes_consecutives"] >= SEUIL_PERTES_CONSECUTIVES_PAUSE:
                # ── CORRECTIF (09/07) — confirmé en conditions réelles : cette
                # alerte se redéclenchait en ENTIER à CHAQUE perte supplémentaire
                # (3, 4, 5, 6, 7, 8 pertes...), noyant Telegram de messages
                # identiques. Désormais : message complet seulement au premier
                # franchissement du seuil ; les pertes suivantes prolongent la
                # pause silencieusement (juste un log, pas de nouveau message).
                premiere_fois = etat_global["pertes_consecutives"] == SEUIL_PERTES_CONSECUTIVES_PAUSE
                etat_global["pause_ouverture_jusqua_ts"] = time.time() + DUREE_PAUSE_APRES_PERTES_MIN * 60
                log.warning(f"  ⏸️ [PAUSE-PERTES] {etat_global['pertes_consecutives']} pertes "
                            f"consécutives — pause {'de' if premiere_fois else 'prolongée de'} "
                            f"{DUREE_PAUSE_APRES_PERTES_MIN} min avant toute nouvelle ouverture. "
                            f"Les trades déjà ouverts continuent d'être surveillés normalement.")
                if premiere_fois:
                    await telegram(session,
                        f"⏸️ <b>PAUSE AUTOMATIQUE — {etat_global['pertes_consecutives']} PERTES D'AFFILÉE</b>\n"
                        f"Le bot arrête d'ouvrir de nouveaux trades pendant {DUREE_PAUSE_APRES_PERTES_MIN} "
                        f"minutes.\n"
                        f"Les positions déjà ouvertes restent surveillées normalement (stops/planchers "
                        f"actifs). Reprise automatique du scan ensuite."
                    )

        etat_global.setdefault("historique", []).append({
            'heure':           (datetime.utcnow() - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M'),
            'heure_ouverture': (datetime.fromtimestamp(debut) - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M'),
            'marche':          symbole,
            'direction':       direction,
            'resultat':        resultat_final,
            'motif_sortie':    motif_sortie,
            'gain':            round(gain_final, 2),
            'pnl_max':         round(pnl_max_atteint, 2),
            'pnl_max_pct':     round(pnl_max_atteint / position * 100, 3) if position else 0.0,
            'frais_estimes':   round(calc_frais(position)["total"], 4),
            'prix_entree':     prix_entree,
            'prix_sortie':     prix_sortie,
            'prix_stop':       stop_initial,
            'objectif':        objectif_final,
            'breakeven_anticipe': breakeven_anticipe_pose,
            'mise':            round(mise, 2),
            'capital':         etat_global["capital"],
            'duree_minutes':   duree,
            'rsi':             rsi_1h,
            'vol_ratio':       details.get("vol_ratio", 0.0),
            'variation_pct':   details.get("variation_pct", 0.0),
            'atr':             details.get("atr", None),
            'atr_pct':         details.get("atr_pct", None),
            'glissement_pct':  details.get("glissement_pct", 0.0),
            'pos_id':          pos_id,
            'cle_suivi':       cle_suivi,
        })

    # ── Pause automatique des marchés qui gappent (12/07) — voir GAP_* et
    # _enregistrer_gap_si_besoin. Si ce trade est un gap, on le mémorise ; s'il
    # vient de faire basculer le marché en pause, on notifie une seule fois.
    gap_a_bascule = _enregistrer_gap_si_besoin(symbole, round(gain_final, 2),
                                               motif_sortie, position, etat_global)
    if gap_a_bascule:
        nb_g, perte_g = _gaps_recents(symbole, etat_global)
        log.warning(f"  🚫 {symbole} MIS EN PAUSE (gaps) — {nb_g} gaps / {perte_g}€ sur "
                    f"{GAP_FENETRE_JOURS}j. Plus de nouveaux trades dessus jusqu'à ce qu'il se calme.")
        await telegram(session,
            f"🚫 <b>MARCHÉ EN PAUSE — {symbole}</b>\n"
            f"Trop de gaps récents : {nb_g} gaps pour {perte_g}€ sur {GAP_FENETRE_JOURS} jours.\n"
            f"Le bot arrête d'ouvrir des trades sur ce marché. Il le réactivera "
            f"automatiquement quand les gaps sortiront de la fenêtre (marché redevenu calme)."
        )

    if alerte_perte_a_envoyer:
        await telegram(session,
            f"🆘 <b>ALERTE PERTE CAPITAL</b>\n"
            f"Capital actuel : {etat_global['capital']}€\n"
            f"Capital de départ : {CAPITAL_INITIAL}€\n"
            f"Perte cumulée : {round(etat_global['capital'] - CAPITAL_INITIAL, 2)}€\n"
            f"Seuil d'alerte (-{SEUIL_ALERTE_PERTE_PCT:.0f}% = -{seuil_alerte_eur}€) franchi."
        )

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

    # Vérification réelle OKX vs calcul interne — copiable/envoyable tel quel
    if verif_reelle is not None:
        net_reel  = round(verif_reelle["pnl"] - abs(verif_reelle["fee"])
                           - abs(verif_reelle["funding_fee"]), 4)
        ecart     = round(net_reel - gain_interne_original, 4)
        flag      = "✅ Cohérent" if abs(ecart) < 0.05 else "⚠️ ÉCART (corrigé automatiquement)"
        msg_verif = (
            f"🔍 <b>VÉRIFICATION RÉELLE OKX</b> — {symbole}\n"
            f"PnL réel OKX : {verif_reelle['pnl']:+.4f} USDC\n"
            f"Frais de transaction (ouv+ferm) : -{abs(verif_reelle['fee']):.4f} USDC\n"
        )
        if abs(verif_reelle["funding_fee"]) > 0.0001:
            msg_verif += f"Frais de financement (funding) : -{abs(verif_reelle['funding_fee']):.4f} USDC\n"
        msg_verif += (
            f"Net réel OKX (retenu comme résultat officiel) : {net_reel:+.4f} USDC\n"
            f"Calcul interne bot (estimation avant vérif) : {gain_interne_original:+.4f}€\n"
            f"Écart : {ecart:+.4f} — {flag}"
        )
        if solde_reel is not None:
            msg_verif += f"\nSolde compte réel : {solde_reel:.2f} USDC"
        await telegram(session, msg_verif)

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
        f"<i>— Totaux cumulés depuis le début (pas seulement aujourd'hui) —</i>\n"
        f"Gagné : +{round(etat_global.get('total_gagne',0),2)}€ | "
        f"Perdu : -{round(etat_global.get('total_perdu',0),2)}€\n"
        f"<b>NET cumulé : {'+' if etat_global.get('cumul_net',0)>=0 else ''}"
        f"{round(etat_global.get('cumul_net',0),2)}€</b>"
    )

async def reprendre_surveillance_position_orpheline(session, symbole, inst_id, etat_global):
    """Relance une surveillance stop/lock/durée COMPLÈTE pour une position réelle
    retrouvée ouverte sur OKX au démarrage du bot (ex: crash ou redéploiement
    Railway alors qu'un trade était en cours). Avant ce correctif, ces positions
    étaient seulement protégées contre une double ouverture (trades_ouverts) mais
    plus aucune tâche ne surveillait leur stop, leur lock de profit ou leur durée
    max — elles restaient ouvertes indéfiniment sans filet de sécurité jusqu'à
    une intervention manuelle.

    Reconstruit direction, prix d'entrée et marge engagée depuis OKX (source de
    vérité — /api/v5/account/positions), recalcule stop/objectif avec la même
    formule que executer_trade, puis délègue à surveiller_et_fermer_trade : la
    position bénéficie exactement de la même logique stop/lock/durée qu'un
    trade normalement ouvert par le bot, sans code dupliqué.

    Autonomie : Damien n'est pas toujours devant son téléphone pour réagir à
    une alerte. En cas d'échec de lecture persistant chez OKX, cette fonction
    ne se contente donc plus d'alerter et d'attendre — après 3 tentatives
    espacées, elle FERME la position par sécurité (impossible de la
    surveiller correctement sans ses vraies données) plutôt que de la
    laisser ouverte et sans filet indéfiniment."""
    # ── Vérification de cohérence instId feed vs exécution — même garde-fou
    # qu'à l'ouverture normale (executer_trade). Une position orpheline
    # utilise le prix du flux public (seule source fonctionnelle pour les
    # données de marché — voir surveiller_et_fermer_trade), protégée par le
    # stop natif OKX et la vérification upl avant tout LOCK. Alerte gardée
    # pour la visibilité : confirmé en conditions réelles que ce catalogue
    # diverge (ex: ETHUSD feed=...-310404 vs exécution=...-310328 le 07/07
    # à 03:51).
    inst_id_feed = OKX_SYMBOLS.get(symbole)
    if inst_id_feed and inst_id_feed != inst_id:
        # ── Niveau WARNING (pas ERROR) volontairement (08/07) : ce cas a déjà
        # son propre message Telegram dédié juste en dessous — le passer en
        # ERROR le ferait aussi remonter en double via le miroir d'erreurs
        # (HandlerErreursTelegram), pour la même information.
        log.warning(f"  🚨 [INCOHÉRENCE INSTID] {symbole} (reprise orpheline) : "
                    f"feed={inst_id_feed} vs exécution={inst_id} — DIFFÉRENTS. "
                    f"Surveillance sur le flux public, protégée par le stop natif "
                    f"et la vérification upl — confirme la divergence des catalogues OKX.")
        await telegram(session,
            f"🚨 <b>INCOHÉRENCE INSTID</b> (position reprise)\n"
            f"{symbole} : feed={inst_id_feed} vs exécution={inst_id}.\n"
            f"Surveillance sur le flux public — protégée par le stop natif OKX et la "
            f"vérification du PnL réel avant tout LOCK. Pas d'action requise."
        )

    path  = "/api/v5/account/positions"
    query = f"?instId={inst_id}"
    data = None

    # 3 tentatives espacées (5s, 15s, 30s) avant de considérer l'échec comme
    # persistant — tolère un simple aléa réseau/API sans déclencher tout de
    # suite la fermeture de sécurité.
    delais_retry = [5, 15, 30]
    for tentative, delai in enumerate([0] + delais_retry, start=1):
        if delai:
            await asyncio.sleep(delai)
        try:
            async with session.get(
                OKX_BASE_URL + path + query,
                headers=_okx_headers("GET", path + query, ""),
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
        except Exception as e:
            log.error(f"  [REPRISE-ORPHELINE] Exception lecture position {inst_id} "
                      f"(tentative {tentative}/4) : {e}")
            data = None

        if data and data.get("code") == "0" and data.get("data"):
            break
        log.warning(f"  [REPRISE-ORPHELINE] {symbole} : lecture position échouée "
                    f"(tentative {tentative}/4)")

    if not data or data.get("code") != "0" or not data.get("data"):
        log.error(f"  [REPRISE-ORPHELINE] ❌ Lecture de {symbole} ({inst_id}) impossible après "
                  f"4 tentatives — fermeture de sécurité de la position (pas de surveillance "
                  f"possible sans ses données réelles).")
        succes_fermeture = await okx_fermer_position(session, inst_id)
        async with trades_lock:
            trades_ouverts.pop(symbole, None)
        if succes_fermeture:
            await telegram(session,
                f"🛡️ <b>FERMETURE DE SÉCURITÉ</b>\n"
                f"{symbole} : impossible de lire les données réelles de cette position sur OKX "
                f"après plusieurs tentatives, donc impossible de la surveiller correctement.\n"
                f"Position fermée par précaution pour éviter qu'elle reste exposée sans stop."
            )
        else:
            await telegram(session,
                f"🚨 <b>ALERTE — ACTION MANUELLE REQUISE</b>\n"
                f"{symbole} : impossible de lire ET impossible de fermer cette position "
                f"automatiquement après plusieurs tentatives.\n"
                f"Elle reste ouverte SANS aucune surveillance — vérifie sur OKX dès que possible."
            )
        return

    p        = data["data"][0]
    pos_size = float(p.get("pos", 0) or 0)
    avg_px   = float(p.get("avgPx", 0) or 0)
    pos_side = (p.get("posSide") or "").lower()
    pos_id   = p.get("posId")  # déjà présent dans cette réponse — aucun appel API en plus

    if pos_size == 0 or avg_px <= 0:
        log.warning(f"  [REPRISE-ORPHELINE] {symbole} : position déjà nulle ou prix d'entrée "
                    f"invalide au moment de la reprise — rien à surveiller, marché libéré.")
        async with trades_lock:
            trades_ouverts.pop(symbole, None)
        return


    # Direction : en mode long/short explicite, posSide le donne directement ;
    # en mode net, le signe de pos donne le sens.
    if pos_side == "long":
        direction = "ACHAT"
    elif pos_side == "short":
        direction = "VENTE"
    else:
        direction = "ACHAT" if pos_size > 0 else "VENTE"

    prix_entree = avg_px
    ratio_prix  = STOP_LOSS_PCT
    if direction == "ACHAT":
        stop_initial   = round(prix_entree * (1 - ratio_prix), 8)
        objectif_final = round(prix_entree * (1 + ratio_prix * 2), 8)
    else:
        stop_initial   = round(prix_entree * (1 + ratio_prix), 8)
        objectif_final = round(prix_entree * (1 - ratio_prix * 2), 8)

    capital = etat_global.get("capital", CAPITAL_INITIAL)

    # Marge réellement engagée sur OKX (isolé) — sert de base au calcul du
    # PnL dans la boucle de surveillance. Plusieurs libellés possibles selon
    # la version de l'API OKX ; on prend le premier disponible et non nul.
    marge_reelle = 0.0
    for champ in ("margin", "imr", "mmr"):
        val = float(p.get(champ, 0) or 0)
        if val > 0:
            marge_reelle = val
            break
    mise     = round(marge_reelle, 2) if marge_reelle > 0 else round(capital * 0.1, 2)
    position = round(mise * LEVIER, 2)
    stop_loss_eur = round(position * ratio_prix, 2)

    # Durée déjà écoulée depuis l'ouverture RÉELLE (cTime OKX, en ms) — pour
    # que la fermeture forcée à 6h parte du vrai début du trade, pas de
    # l'instant de la reprise (sinon une position ouverte depuis 5h se
    # verrait accorder 6h de plus après chaque redémarrage).
    debut_override = None
    c_time_ms = p.get("cTime")
    if c_time_ms:
        try:
            # cTime est un timestamp epoch en millisecondes (comme time.time()*1000,
            # mais en secondes) — simple conversion, pas un calcul de durée.
            debut_override = float(c_time_ms) / 1000.0
        except (ValueError, TypeError):
            debut_override = None

    log.warning(f"  [REPRISE-ORPHELINE] {symbole} ({direction}) — prix entrée OKX={prix_entree}, "
                f"stop={stop_initial}, marge≈{mise}€ — surveillance stop/lock/durée relancée.")

    # ── Trailing stop natif OKX pour cette position récupérée — elle n'en
    # avait AUCUN jusqu'ici. HYBRIDE (07/07, 14:35) : stop fixe classique
    # d'abord (comme à une ouverture normale) — le basculement vers le
    # trailing natif se fera automatiquement au premier palier franchi,
    # dans surveiller_et_fermer_trade.
    side_ouverture = "buy" if direction == "ACHAT" else "sell"
    taille_contrats_reelle = abs(pos_size)
    algo_id = await okx_placer_ordre_stop_algo(
        session, inst_id, side_ouverture, taille_contrats_reelle, stop_initial
    )
    algo_type = "fixe"
    if algo_id is None:
        log.warning(f"  ⚠️ [STOP-ALGO] Pose du stop natif échouée pour {symbole} "
                    f"(position reprise) — la surveillance interne du bot reste l'unique filet.")

    await telegram(session,
        f"🔄 <b>SURVEILLANCE REPRISE</b>\n"
        f"{symbole} ({'🟢 ACHAT' if direction == 'ACHAT' else '🔴 VENTE'})\n"
        f"Prix d'entrée (OKX) : {prix_entree} | Stop : {stop_initial}\n"
        f"Marge engagée (estimée) : {mise}€\n"
        f"Cette position, retrouvée ouverte au démarrage, est de nouveau "
        f"activement surveillée (stop / lock de profit / durée max).\n"
        + (f"🛡️ Stop natif OKX activé. Le trailing prendra le relais dès le premier "
           f"palier de gain atteint."
           if algo_id else
           f"⚠️ Stop natif NON posé — la surveillance interne reste l'unique filet.")
    )

    details_reconstruits = {
        "rsi_1h": 50.0, "vol_ratio": 0.0, "variation_pct": 0.0,
        "prix_ref": prix_entree, "prix_actuel": prix_entree, "atr": None,
    }

    await surveiller_et_fermer_trade(
        session, symbole, direction, mise, capital, position,
        prix_entree, stop_initial, objectif_final, stop_loss_eur,
        50.0, details_reconstruits, inst_id, etat_global,
        debut_override=debut_override, algo_id=algo_id,
        taille_contrats=taille_contrats_reelle, pos_id=pos_id, algo_type=algo_type
    )


async def reconcilier_trades_manques(session, etat):
    """Rattrape les trades qui se sont fermés côté OKX PENDANT que le bot était
    hors ligne (ex : entre l'arrêt et le redémarrage lors d'un redéploiement
    Railway) — incident réel confirmé le 08/07 : un trade DOGEUSD ouvert à
    12:14 s'est fermé à 12:50 avec une perte réelle (-3,29 USDC) sans JAMAIS
    être rapporté sur Telegram ni comptabilisé dans le capital suivi. Cause :
    le stop/plancher posé côté OKX continue de fonctionner même bot éteint,
    mais si la fermeture a lieu PENDANT cette fenêtre, la position n'est plus
    "ouverte" au redémarrage — la reprise de position orpheline (qui ne
    regarde que les positions ENCORE ouvertes) ne peut pas la voir.

    Compare l'historique récent OKX (positions-history, identifié par posId,
    LA source d'identité unique) à ce que le bot a déjà enregistré dans
    etat["historique"], et rattrape tout écart : capital, historique, ET
    message Telegram rétroactif.

    Fenêtre de recherche bornée par etat["reconciliation_depuis_ts"] (mise à
    jour à chaque exécution) plutôt que "tout l'historique OKX" : les trades
    d'avant l'existence de ce correctif n'ont pas de pos_id enregistré, donc
    TOUT semblerait "manquant" sans cette borne — un recomptage complet du
    capital historique serait une corruption bien pire que le problème
    d'origine. Au tout premier passage (clé absente), la fenêtre par défaut
    est volontairement courte (2h) pour rattraper un incident récent sans
    ressusciter d'anciens trades déjà comptabilisés."""
    if not MODE_REEL or RESET_TOUT:
        return
    maintenant_ts = time.time()
    depuis_ts = etat.get("reconciliation_depuis_ts")
    if depuis_ts is None:
        depuis_ts = maintenant_ts - 2 * 3600  # 2h en arrière au tout premier passage

    posids_connus = {h.get("pos_id") for h in etat.get("historique", []) if h.get("pos_id")}
    quelque_chose_rattrape = False

    for symbole, inst_id in list(OKX_SYMBOLS_EXEC.items()):
        path  = "/api/v5/account/positions-history"
        query = f"?instId={inst_id}&limit=10"
        try:
            async with session.get(
                OKX_BASE_URL + path + query,
                headers=_okx_headers("GET", path + query, ""),
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                data = await resp.json()
            if data.get("code") != "0":
                continue
            for p in data.get("data", []):
                pos_id_okx = p.get("posId")
                if not pos_id_okx or pos_id_okx in posids_connus:
                    continue
                u_time_ms = p.get("uTime")
                if not u_time_ms:
                    continue
                try:
                    u_time_sec = float(u_time_ms) / 1000.0
                except (ValueError, TypeError):
                    continue
                if u_time_sec < depuis_ts:
                    continue  # fermé avant la fenêtre de recherche — ignoré volontairement
                try:
                    pnl         = float(p.get("pnl", 0) or 0)
                    fee         = float(p.get("fee", 0) or 0)
                    funding_fee = float(p.get("fundingFee", 0) or 0)
                except (ValueError, TypeError):
                    continue
                gain_net = round(pnl - abs(fee) - abs(funding_fee), 4)
                resultat = "GAGNE" if gain_net > 0 else "PERDU"
                close_dt = datetime.utcfromtimestamp(u_time_sec)

                etat["capital"]   = round(etat["capital"] + gain_net, 2)
                etat["cumul_net"] = round(etat["capital"] - CAPITAL_INITIAL, 2)
                etat["pnl_jour"]  = round(etat.get("pnl_jour", 0) + gain_net, 2)
                etat["nb_trades"] = etat.get("nb_trades", 0) + 1
                if gain_net > 0:
                    etat["nb_wins"]     = etat.get("nb_wins", 0) + 1
                    etat["total_gagne"] = round(etat.get("total_gagne", 0) + gain_net, 2)
                else:
                    etat["nb_losses"]   = etat.get("nb_losses", 0) + 1
                    etat["total_perdu"] = round(etat.get("total_perdu", 0) + abs(gain_net), 2)

                etat.setdefault("historique", []).append({
                    'heure':         (close_dt - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M'),
                    'marche':        symbole,
                    'direction':     "?",
                    'resultat':      resultat,
                    'gain':          gain_net,
                    'mise':          None,
                    'capital':       etat["capital"],
                    'duree_minutes': None,
                    'rsi':           None,
                    'vol_ratio':     0.0,
                    'pos_id':        pos_id_okx,
                })
                posids_connus.add(pos_id_okx)
                quelque_chose_rattrape = True
                log.error(f"  🔁 [RECONCILIATION] {symbole} : trade fermé pendant que le bot "
                          f"était hors ligne, rattrapé (posId={pos_id_okx}, net={gain_net}€).")
                await telegram(session,
                    f"🔁 <b>TRADE RATTRAPÉ (fermé hors-ligne)</b>\n"
                    f"{symbole} : ce trade s'est fermé pendant que le bot était hors ligne "
                    f"(redémarrage/redéploiement) et n'avait jamais été rapporté.\n"
                    f"Résultat net : {'+' if gain_net>=0 else ''}{gain_net}€\n"
                    f"Clôturé le {close_dt.strftime('%d/%m %H:%M')} UTC\n"
                    f"Capital ajusté en conséquence : {etat['capital']}€"
                )
        except Exception as e:
            log.error(f"  ❌ [RECONCILIATION] Exception pour {symbole} ({inst_id}) : {e}")

    etat["reconciliation_depuis_ts"] = maintenant_ts
    if quelque_chose_rattrape:
        sauvegarder_etat(etat)


# ═══════════════════════════════════════════════════════════════
#  PROTECTIONS
# ═══════════════════════════════════════════════════════════════
def verifier_protections(etat, capital):
    if capital < SEUIL_RUINE:
        log.critical(f"SEUIL RUINE ! Capital {capital}€ → ARRET")
        return "RUINE"
    seuil_jour = seuil_kill_switch(capital)
    if etat.get("pnl_jour", 0.0) <= seuil_jour:
        log.warning(f"KILL SWITCH — PnL jour {etat.get('pnl_jour', 0)}€ "
                    f"(seuil : {seuil_jour:.2f}€, soit -{KILL_SWITCH_PCT*100:.0f}% du capital)")
        return "KILL_SWITCH"
    return "OK"

def reset_pnl_jour_si_nouveau_jour(etat):
    """Retourne True si le PnL du jour a été remis à 0 (changement de jour)."""
    maintenant_guyane = datetime.utcnow() - timedelta(hours=3)
    aujourd_hui = maintenant_guyane.strftime('%Y-%m-%d')
    if etat.get("date_jour", "") != aujourd_hui:
        etat["pnl_jour"]  = 0.0
        etat["date_jour"] = aujourd_hui
        etat["nb_plancher_amende"]        = 0
        etat["nb_plancher_repositionne"]  = 0
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
    detail_trades = []  # 09/07 — un enregistrement par trade (pas agrégé), pour
                          # que Damien puisse voir le RSI et le volume de CHAQUE
                          # trade de la journée, gagnant comme perdant, et calculer
                          # lui-même quel seuil retenir plutôt que d'ajuster à
                          # l'aveugle.

    for h in trades_jour:
        marche   = h.get("marche", "?")
        gain     = h.get("gain", 0)
        resultat = h.get("resultat", "")
        duree    = h.get("duree_minutes", 0)
        rsi      = h.get("rsi", 50.0)
        vol      = h.get("vol_ratio", 0.0)
        variation   = h.get("variation_pct", 0.0)
        atr_h       = h.get("atr_pct", None)
        glissement  = h.get("glissement_pct", 0.0)
        heure_ouv   = h.get("heure_ouverture", h.get("heure", ""))
        heure_str = h.get("heure", "")
        suivi_post_stop_pct = h.get("suivi_post_stop_pct", None)
        stop_bien_place     = h.get("stop_bien_place", None)
        palier1_post_stop   = h.get("palier1_atteint_post_stop", None)
        direction_h  = h.get("direction", "?")
        motif_sortie_h = h.get("motif_sortie", "?")
        pnl_max_h    = h.get("pnl_max", None)
        pnl_max_pct_h = h.get("pnl_max_pct", None)
        frais_h      = h.get("frais_estimes", None)
        prix_entree_h = h.get("prix_entree", None)
        prix_sortie_h = h.get("prix_sortie", None)
        prix_stop_h   = h.get("prix_stop", None)
        objectif_h    = h.get("objectif", None)
        breakeven_h   = h.get("breakeven_anticipe", None)

        gains_jour[marche]  = round(gains_jour.get(marche, 0) + gain, 2)
        rsi_jour.setdefault(marche, []).append(rsi)
        detail_trades.append({
            "marche": marche, "heure": heure_str[11:16] if len(heure_str) >= 16 else "?",
            "heure_ouv": heure_ouv[11:16] if len(heure_ouv) >= 16 else "?",
            "rsi": rsi, "vol": vol, "gain": gain, "resultat": resultat,
            "variation": variation, "atr": atr_h, "glissement": glissement,
            "suivi_post_stop_pct": suivi_post_stop_pct, "stop_bien_place": stop_bien_place,
            "palier1_post_stop": palier1_post_stop,
            "direction": direction_h, "motif_sortie": motif_sortie_h,
            "pnl_max": pnl_max_h, "pnl_max_pct": pnl_max_pct_h, "frais": frais_h,
            "prix_entree": prix_entree_h, "prix_sortie": prix_sortie_h,
            "prix_stop": prix_stop_h, "objectif": objectif_h, "breakeven": breakeven_h,
            "duree": duree,
        })

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

    # ── Moyennes TOUS trades confondus, gagnants ET perdants ensemble (09/07,
    # demandé par Damien : "au lieu de se baser à l'aveugle, on va se baser sur
    # des données"). Objectif : donner de quoi choisir un seuil RSI/volume à
    # partir de ce qui s'est réellement passé, pas d'une intuition.
    rsi_tous = [d["rsi"] for d in detail_trades]
    vol_tous = [d["vol"] for d in detail_trades]
    rsi_moy_tous = round(sum(rsi_tous) / len(rsi_tous), 1) if rsi_tous else 0
    vol_moy_tous = round(sum(vol_tous) / len(vol_tous), 2) if vol_tous else 0
    # ── Min/max (09/07, demandé en plus de la moyenne) — la fourchette
    # complète observée dans la journée, tous trades confondus.
    rsi_min_tous = round(min(rsi_tous), 1) if rsi_tous else 0
    rsi_max_tous = round(max(rsi_tous), 1) if rsi_tous else 0
    vol_min_tous = round(min(vol_tous), 2) if vol_tous else 0
    vol_max_tous = round(max(vol_tous), 2) if vol_tous else 0

    # ── Les 3 nouvelles données (09/07, demandé en plus de RSI/volume) :
    # variation d'entrée, ATR (volatilité), glissement d'exécution. Toutes
    # déjà calculées ailleurs dans le bot — jamais moyennées jusqu'ici.
    variation_tous = [d["variation"] for d in detail_trades]
    atr_tous       = [d["atr"] for d in detail_trades if d["atr"] is not None]
    glissement_tous = [d["glissement"] for d in detail_trades]
    variation_moy = round(sum(variation_tous) / len(variation_tous), 3) if variation_tous else 0
    variation_min = round(min(variation_tous), 3) if variation_tous else 0
    variation_max = round(max(variation_tous), 3) if variation_tous else 0
    atr_moy = round(sum(atr_tous) / len(atr_tous), 3) if atr_tous else None
    atr_min = round(min(atr_tous), 3) if atr_tous else None
    atr_max = round(max(atr_tous), 3) if atr_tous else None
    glissement_moy = round(sum(glissement_tous) / len(glissement_tous), 3) if glissement_tous else 0
    glissement_min = round(min(glissement_tous), 3) if glissement_tous else 0
    glissement_max = round(max(glissement_tous), 3) if glissement_tous else 0
    rsi_gagnants = [d["rsi"] for d in detail_trades if d["resultat"] == "GAGNE"]
    vol_gagnants = [d["vol"] for d in detail_trades if d["resultat"] == "GAGNE"]
    # "Seuil suggéré" = le pire cas (min) parmi les gagnants du jour : le
    # niveau le plus bas qui a quand même donné un trade gagnant aujourd'hui —
    # une indication concrète, pas une recommandation statistiquement fiable
    # sur un seul jour (échantillon trop petit pour ça).
    rsi_min_gagnant = round(min(rsi_gagnants), 1) if rsi_gagnants else None
    vol_min_gagnant = round(min(vol_gagnants), 2) if vol_gagnants else None

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

    nb_amende        = etat.get("nb_plancher_amende", 0)
    nb_repositionne  = etat.get("nb_plancher_repositionne", 0)
    nb_planchers_maj = nb_amende + nb_repositionne
    if nb_planchers_maj > 0:
        bloc_plancher = (
            f"\n🧱 <b>PLANCHERS DURS MIS À JOUR</b>\n"
            f"Amendés (1 appel) : {nb_amende} | Repositionnés (pose+annulation) : "
            f"{nb_repositionne}\n"
        )
    else:
        bloc_plancher = ""

    # ── Détail RSI/volume PAR TRADE (09/07) — un trade par ligne, gagnant
    # comme perdant, dans l'ordre chronologique. Sur beaucoup de trades dans
    # la journée, Telegram tronque au-delà d'un certain nombre de caractères
    # — on garde donc ce détail lisible même si la journée a été chargée.
    lignes_detail = []
    for d in detail_trades:
        emoji_d = "✅" if d["resultat"] == "GAGNE" else "❌"
        atr_aff = f"{d['atr']:.3f}%" if d['atr'] is not None else "?"
        lignes_detail.append(
            f"{emoji_d} <code>{d['heure_ouv']} {d['marche']:<10} RSI:{d['rsi']:<5} "
            f"Vol:{d['vol']:<5} Var:{d['variation']:.2f}% Gliss:{d['glissement']:+.2f}% "
            f"ATR:{atr_aff} {'+' if d['gain']>=0 else ''}{d['gain']}€</code>"
        )
    bloc_detail = "\n".join(lignes_detail)

    bloc_suggestion = ""
    if rsi_min_gagnant is not None and vol_min_gagnant is not None:
        bloc_atr = (
            f"ATR — moyenne : {atr_moy}% | min : {atr_min}% | max : {atr_max}%\n"
            if atr_moy is not None else ""
        )
        bloc_suggestion = (
            f"\n💡 <b>POUR CHOISIR UN SEUIL</b>\n"
            f"RSI — moyenne : {rsi_moy_tous} | min : {rsi_min_tous} | max : {rsi_max_tous}\n"
            f"Volume — moyenne : {vol_moy_tous}x | min : {vol_min_tous}x | max : "
            f"{vol_max_tous}x\n"
            f"Variation d'entrée — moyenne : {variation_moy}% | min : {variation_min}% | "
            f"max : {variation_max}%\n"
            f"Glissement d'exécution — moyenne : {glissement_moy}% | min : {glissement_min}% "
            f"| max : {glissement_max}%\n"
            f"{bloc_atr}"
            f"Pire cas encore gagnant aujourd'hui : RSI {rsi_min_gagnant} | Volume "
            f"{vol_min_gagnant}x\n"
            f"<i>(indicatif sur 1 seule journée — pas encore fiable statistiquement, "
            f"à confirmer sur plusieurs jours avant de changer un seuil)</i>\n"
        )

    # ── Récapitulatif SUIVI POST-STOP (09/07, demandé par Damien) — pour
    # chaque stop du jour où la donnée est disponible (15 min après la
    # fermeture, voir suivre_prix_post_stop) : est-ce que le prix a continué
    # dans le sens du stop (bien calibré) ou est reparti en sens inverse
    # (stop trop serré) ? Objectif : donner une vraie base pour ajuster
    # STOP_LOSS_PCT, trade par trade ET en tendance générale sur la journée.
    trades_avec_suivi = [d for d in detail_trades if d["suivi_post_stop_pct"] is not None]
    bloc_post_stop = ""
    if trades_avec_suivi:
        nb_bien_places = sum(1 for d in trades_avec_suivi if d["stop_bien_place"])
        trades_avec_palier1 = [d for d in trades_avec_suivi if d["palier1_post_stop"] is not None]
        nb_palier1_atteint  = sum(1 for d in trades_avec_palier1 if d["palier1_post_stop"])
        lignes_post_stop = []
        for d in trades_avec_suivi:
            # ── CORRECTIF (09/07) — l'ancienne version utilisait ↘️/↗️ pour le
            # VERDICT (bien placé / trop serré), pas la direction réelle du
            # prix, donnant des combinaisons trompeuses (ex: ↘️ affiché alors
            # que le prix était monté). Maintenant : la flèche suit le signe
            # réel de la variation, le verdict est un symbole séparé (✅/⚠️).
            fleche  = "📈" if d["suivi_post_stop_pct"] >= 0 else "📉"
            verdict = "✅" if d["stop_bien_place"] else "⚠️"
            # ── 10/07 (demandé par Damien) : marqueur 🎯 si le prix a franchi
            # le niveau du 1er palier de gain à un moment de la fenêtre —
            # aurait été un trade gagnant si on avait tenu au lieu de stopper.
            marqueur_palier1 = " 🎯" if d.get("palier1_post_stop") else ""
            lignes_post_stop.append(
                f"{verdict}{fleche} <code>{d['marche']:<10} {d['suivi_post_stop_pct']:+.3f}%</code>"
                f"{marqueur_palier1}"
            )
        bloc_palier1_resume = (
            f"🎯 Aurait franchi le 1er palier : {nb_palier1_atteint}/{len(trades_avec_palier1)}\n"
            if trades_avec_palier1 else ""
        )
        bloc_post_stop = (
            f"\n📐 <b>SUIVI POST-STOP</b> ({nb_bien_places}/{len(trades_avec_suivi)} bien placés)\n"
            f"{bloc_palier1_resume}"
            + "\n".join(lignes_post_stop) + "\n"
            f"<i>✅ = stop bien placé | ⚠️ = prix reparti en sens inverse (stop trop serré) "
            f"| 📈/📉 = direction réelle du prix ensuite | 🎯 = aurait franchi le 1er "
            f"palier de gain</i>\n"
        )

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
        f"{lignes_pertes_h}\n"
        f"{bloc_plancher}"
        f"{bloc_suggestion}"
        f"{bloc_post_stop}\n"
        f"<code>{'─'*40}</code>\n"
        f"📎 <i>Détail complet des {len(detail_trades)} trades du jour (RSI, volume, "
        f"variation, glissement, ATR, suivi post-stop) en pièce jointe ci-dessous.</i>\n"
        f"<code>{'─'*40}</code>\n"
        f"<b>CLASSEMENT MARCHÉS</b>\n"
        f"<code>{'MARCHÉ':<12} {'GAINS':<10} {'G/P':<6} RSI MOY</code>\n"
        f"{chr(10).join(lignes_marches)}"
    )
    # ── CORRECTIF (10/07, demandé par Damien) — l'ancien bloc "DÉTAIL PAR
    # TRADE" en texte dépassait régulièrement la limite Telegram (4096
    # caractères) sur une journée chargée, tronquant le rapport. Le détail
    # complet part maintenant en CSV joint (aucune limite pratique de
    # taille, ouvrable dans Excel/Google Sheets) — le message texte reste
    # un résumé, toujours largement sous la limite.
    if len(message) > 4000:
        message = message[:4000] + "\n\n… (résumé tronqué — voir le CSV joint pour le détail complet)"
    log.info("  Envoi rapport quotidien Telegram")
    await telegram(session, message)
    if detail_trades:
        csv_contenu = construire_csv_trades(detail_trades)
        await telegram_document(
            session, f"trades_{date_affich.replace('/', '-')}.csv", csv_contenu,
            legende=f"Détail des {len(detail_trades)} trades du {date_affich}"
        )

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
    global trades_lock, CAPITAL_INITIAL, arret_demande
    trades_lock = asyncio.Lock()

    # ── Arrêt propre (Graceful Shutdown) sur SIGTERM/SIGINT — Railway envoie
    # SIGTERM lors d'un redéploiement ou d'un arrêt manuel. Sans gestion
    # explicite, une tâche executer_trade en plein appel HTTP OKX pourrait
    # être interrompue à mi-chemin (requête envoyée, réponse jamais
    # traitée), créant la même ambiguïté qu'un timeout réseau — mais cette
    # fois sans aucune chance de rattrapage. On se contente de LEVER un
    # drapeau : la boucle de scan arrête d'ouvrir de NOUVEAUX trades, et on
    # laisse les tâches déjà en cours se terminer naturellement (avec un
    # délai raisonnable) avant de laisser le process s'arrêter.
    def _gestionnaire_arret(signum, frame):
        global arret_demande
        if not arret_demande:
            log.warning(f"  🛑 Signal d'arrêt reçu ({signum}) — arrêt de l'ouverture de nouveaux "
                        f"trades, les trades en cours se terminent normalement...")
            arret_demande = True

    try:
        signal.signal(signal.SIGTERM, _gestionnaire_arret)
        signal.signal(signal.SIGINT, _gestionnaire_arret)
    except (ValueError, OSError) as e:
        # Peut échouer si on n'est pas dans le thread principal — non bloquant,
        # le bot continue sans cette protection plutôt que de planter au démarrage.
        log.warning(f"  ⚠️ Impossible d'enregistrer les gestionnaires de signal : {e}")

    init_database()

    # ── Chargement ROBUSTE de l'état (11/07) — CORRECTIF du bug qui effaçait
    # l'historique. Cause exacte : charger_etat() renvoyait {} sur un simple
    # aléa de connexion DB au démarrage (fréquent sur Railway quand la base
    # n'est pas encore prête). Le bot croyait alors à un premier démarrage,
    # peuplait ses valeurs par défaut, et les SAUVEGARDAIT juste après (plus
    # bas) — écrasant DÉFINITIVEMENT l'historique réel. Désormais charger_etat()
    # LÈVE si la lecture échoue (au lieu de renvoyer {}), et on ne réinitialise
    # QUE sur un vrai RESET_TOUT ou une base réellement vide. Si la lecture
    # échoue malgré les retries : on N'ÉCRASE PAS — on sort, Railway redémarre
    # et réessaie, l'historique reste intact.
    if RESET_TOUT:
        log.warning("  🔄 RESET_TOUT activé sur Railway — remise à zéro complète de l'état du bot")
        etat = {}
        sauvegarder_etat(etat)
    else:
        etat = None
        for tentative in range(6):
            try:
                etat = charger_etat()   # {} seulement si base VRAIMENT vide ; lève si lecture impossible
                break
            except Exception as e:
                log.error(f"  ⚠️ Chargement de l'état échoué (tentative {tentative + 1}/6) : {e}")
                time.sleep(3)
        if etat is None:
            log.critical("  ❌ Base injoignable au démarrage après 6 tentatives — ARRÊT SANS "
                         "écraser l'état. Railway va redémarrer et réessayer. L'historique est "
                         "PRÉSERVÉ, pas effacé.")
            raise SystemExit(1)

    # Initialiser les champs manquants
    for champ, valeur in [
        ("capital", CAPITAL_INITIAL),
        ("pnl_jour", 0.0),
        ("date_jour", ""),
        ("wins_consecutifs", 0),
        ("nb_skips", 0),
        ("nb_plancher_amende", 0),
        ("nb_plancher_repositionne", 0),
        ("nb_trades", 0),
        ("nb_wins", 0),
        ("nb_losses", 0),
        ("total_gagne", 0.0),
        ("total_perdu", 0.0),
        ("cumul_net", 0.0),
        ("pertes_consecutives", 0),
        ("pause_ouverture_jusqua_ts", 0),
        ("suivis_post_stop_en_attente", []),
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

        # ── Récupération automatique du vrai capital OKX au démarrage (08/07,
        # CORRIGÉ suite à une clarification de Damien : le bot doit vérifier
        # lui-même le capital réel, pas se fier à une valeur codée en dur).
        # DEUX changements par rapport à la version précédente :
        # 1) S'exécute à CHAQUE démarrage en compte RÉEL (OKX_COMPTE_DEMO=0),
        #    pas seulement lors d'un RESET_TOUT — sur un compte réel, le solde
        #    OKX est TOUJOURS la source de vérité, il n'y a jamais de faux
        #    solde à filtrer (contrairement au compte démo).
        # 2) Le garde-fou "écart > 50% rejeté" est retiré pour un compte
        #    RÉEL : il n'avait de sens que pour filtrer le solde démo par
        #    défaut (~99 000 USDC, sans rapport avec les fonds alloués) — sur
        #    un compte réel, ce même garde-fou pouvait à tort REJETER le vrai
        #    solde s'il s'écartait de plus de 50% de la valeur codée en dur
        #    (543.65€), ce qui aurait précisément empêché la synchronisation
        #    automatique demandée. Le garde-fou reste actif uniquement pour
        #    le compte DÉMO (OKX_COMPTE_DEMO=1), où le faux solde existe
        #    réellement et doit continuer à être filtré.
        if MODE_REEL and not OKX_COMPTE_DEMO:
            log.info("  Compte RÉEL : récupération automatique du capital réel OKX...")
            solde_reel_demarrage = await okx_recuperer_solde_reel(session, "USDC")
            if solde_reel_demarrage is not None:
                capital_avant = etat.get("capital", CAPITAL_INITIAL)
                # La référence de performance (CAPITAL_INITIAL, base du %) n'est
                # fixée qu'une seule fois — au tout premier démarrage réel (ou
                # après un RESET_TOUT explicite) — pour ne pas faire bouger le
                # point de départ des calculs de performance à chaque redémarrage.
                if RESET_TOUT or etat.get("nb_trades", 0) == 0:
                    CAPITAL_INITIAL = round(solde_reel_demarrage, 2)
                etat["capital"] = round(solde_reel_demarrage, 2)
                sauvegarder_etat(etat)
                log.info(f"  ✅ Capital réel synchronisé automatiquement depuis OKX : "
                         f"{etat['capital']}€ (référence CAPITAL_INITIAL={CAPITAL_INITIAL}€, "
                         f"valeur avant sync : {capital_avant}€)")
                await telegram(session,
                    f"💰 <b>CAPITAL RÉEL SYNCHRONISÉ AUTOMATIQUEMENT</b>\n"
                    f"Lu directement sur ton compte OKX au démarrage : <b>{etat['capital']}€</b>\n"
                    f"Aucune valeur codée en dur utilisée pour un compte réel."
                )
            else:
                log.error("  ❌ Impossible de récupérer le vrai solde OKX au démarrage (compte "
                          "réel) — valeur précédente conservée. À VÉRIFIER avant de laisser "
                          "le bot ouvrir des trades.")
                await telegram(session,
                    f"⚠️ <b>ÉCHEC LECTURE SOLDE RÉEL AU DÉMARRAGE</b>\n"
                    f"Capital conservé : {etat.get('capital', CAPITAL_INITIAL)}€ (non "
                    f"resynchronisé). Vérifie que ça correspond à ton capital réel avant de "
                    f"laisser le bot trader."
                )

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

        # ── Synchronisation trades_ouverts avec les vraies positions OKX —
        # protège contre un redémarrage 'amnésique' qui tenterait de rouvrir
        # un marché déjà en position réelle. Ces positions retrouvées n'ont
        # PAS de tâche de surveillance stop/lock active au moment où elles
        # sont détectées (celle-ci tournait dans le process précédent, tuée
        # au redémarrage) — trades_ouverts les protège immédiatement contre
        # une double ouverture, puis reprendre_surveillance_position_orpheline
        # est lancée pour chacune afin de leur relancer une VRAIE tâche de
        # surveillance stop/lock/durée (voir cette fonction plus haut).
        if MODE_REEL:
            instids_ouverts = await okx_lister_toutes_positions_ouvertes(session)
            if instids_ouverts:
                reverse_map = {v: k for k, v in OKX_SYMBOLS_EXEC.items()}
                symboles_orphelins = []
                for inst_id_ouvert in instids_ouverts:
                    symb = reverse_map.get(inst_id_ouvert)
                    if symb:
                        trades_ouverts[symb] = True
                        symboles_orphelins.append((symb, inst_id_ouvert))
                if symboles_orphelins:
                    noms = [s for s, _ in symboles_orphelins]
                    log.warning(f"  ⚠️ Positions réelles déjà ouvertes détectées au démarrage : "
                                f"{noms} — protégées contre un doublon, reprise de la "
                                f"surveillance stop/lock/durée en cours pour chacune.")
                    await telegram(session,
                        f"⚠️ <b>POSITIONS ORPHELINES DÉTECTÉES AU DÉMARRAGE</b>\n"
                        f"Marchés concernés : {', '.join(noms)}\n"
                        f"Ces positions existent réellement sur OKX (probablement ouvertes avant "
                        f"un redémarrage). Reprise automatique de la surveillance stop/lock/durée "
                        f"en cours pour chacune — un message de confirmation suit pour chaque marché."
                    )
                    for symb, inst_id_ouvert in symboles_orphelins:
                        asyncio.create_task(
                            reprendre_surveillance_position_orpheline(session, symb, inst_id_ouvert, etat)
                        )

        # ── Rattrapage des trades fermés PENDANT que le bot était hors ligne
        # (08/07, incident réel confirmé — voir reconcilier_trades_manques) —
        # après la reprise des positions ENCORE ouvertes ci-dessus, on
        # regarde maintenant celles déjà CLOSES entretemps, jamais vues ni
        # comptabilisées. Capital corrigé avant le message de démarrage
        # ci-dessous, pour qu'il affiche déjà la valeur à jour.
        await reconcilier_trades_manques(session, etat)

        # ── Reprise des suivis post-stop en attente (10/07, demandé par
        # Damien) — voir suivre_prix_post_stop / cle_suivi. Chaque entrée
        # encore présente ici correspond à un suivi qui n'a pas eu le temps
        # de se terminer avant l'arrêt/redémarrage précédent (tâche en
        # mémoire perdue). On les relance toutes, avec leur horodatage
        # d'origine — celles déjà en retard reprennent immédiatement (la
        # boucle d'attente interne de suivre_prix_post_stop ne s'exécute
        # alors aucune fois, direct à la lecture finale).
        suivis_en_attente = list(etat.get("suivis_post_stop_en_attente", []))
        if suivis_en_attente:
            log.info(f"  📐 [SUIVI-POST-STOP] {len(suivis_en_attente)} suivi(s) en attente "
                      f"repris depuis le dernier redémarrage.")
            for s in suivis_en_attente:
                asyncio.create_task(suivre_prix_post_stop(
                    session, s["symbole"], s["direction"], s["prix_stop_reel"],
                    s["prix_entree"], s.get("pos_id"), etat,
                    position=s.get("position"), capital=s.get("capital"),
                    moment_fermeture_ts=s.get("moment_fermeture_ts"),
                    cle_suivi=s.get("cle")
                ))

        await telegram(session,
            (f"🔄 <b>RESET COMPLET EFFECTUÉ</b>\nCapital, PnL, compteurs et historique remis à zéro.\n\n" if RESET_TOUT else "")
            + f"🚀 <b>BOT DÉMARRÉ</b>\n"
            f"Capital : {round(etat['capital'],2)}€\n"
            f"Marchés x10 : {len(MARCHES)} cryptos | 24h/24 — 7j/7\n"
            + (f"{', '.join(MARCHES)}\n\n" if MARCHES else "\n")
            + f"Signal : mouvement >= {SEUIL_MOUVEMENT_PCT}%\n"
            f"Frais OKX : {OKX_TAKER_FEE*100:.2f}% ouv + {OKX_TAKER_FEE*100:.2f}% ferm (taker)\n"
            f"Kill switch : -{KILL_SWITCH_PCT*100:.0f}%/jour du capital\n"
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

        dernier_vidage_erreurs = 0.0  # 08/07 — voir vider_file_erreurs_vers_telegram

        while True:
            try:
                if arret_demande and not taches_trades_actives:
                    log.info("  ✅ Arrêt propre : plus aucun trade en cours, fermeture du bot.")
                    break

                if reset_pnl_jour_si_nouveau_jour(etat):
                    sauvegarder_etat(etat)

                # ── Vidage groupé des erreurs vers Telegram toutes les 60s
                # (08/07) — voir HandlerErreursTelegram / FILE_ERREURS_TELEGRAM
                # tout en haut du fichier.
                if time.time() - dernier_vidage_erreurs >= 60:
                    await vider_file_erreurs_vers_telegram(session)
                    dernier_vidage_erreurs = time.time()

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

                # ── Rapport HORAIRE (09/07, demandé par Damien) — même contenu
                # que le rapport quotidien (il lit l'état courant sans rien
                # réinitialiser), envoyé toutes les heures pendant la journée
                # pour suivre en direct RSI/volume/suivi post-stop, sans
                # attendre le rapport officiel de 19h.
                cle_heure_actuelle = maintenant_utc.strftime('%Y-%m-%d %H')
                if (maintenant_utc.minute < 1 and
                        etat.get("dernier_rapport_horaire", "") != cle_heure_actuelle):
                    await envoyer_rapport_quotidien(session, etat)
                    etat["dernier_rapport_horaire"] = cle_heure_actuelle
                    sauvegarder_etat(etat)

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
                    # Rechargement PRUDENT (11/07) — ne JAMAIS remplacer l'état
                    # en mémoire par un état vide si la lecture échoue (aléa DB
                    # passager) : sinon l'historique serait écrasé au prochain
                    # save. On garde l'état courant en cas d'échec ou de retour
                    # vide, et on ne remplace que par un rechargement réussi.
                    try:
                        etat_recharge = charger_etat()
                        if etat_recharge:
                            etat = etat_recharge
                    except Exception as e:
                        log.warning(f"  ⚠️ Rechargement post-kill-switch échoué : {e} — "
                                    f"état en mémoire conservé (historique préservé).")
                    etat.setdefault("capital", CAPITAL_INITIAL)
                    continue

                # ── Pause après pertes consécutives (08/07) — voir
                # SEUIL_PERTES_CONSECUTIVES_PAUSE / DUREE_PAUSE_APRES_PERTES_MIN.
                # Bloque uniquement l'OUVERTURE de nouveaux trades ; les positions
                # déjà ouvertes restent surveillées normalement par leurs propres
                # tâches (stops/planchers actifs, inchangés).
                pause_jusqua = etat.get("pause_ouverture_jusqua_ts", 0)
                if time.time() < pause_jusqua:
                    minutes_restantes = round((pause_jusqua - time.time()) / 60, 1)
                    log.info(f"  ⏸️ Pause après pertes consécutives — reprise dans "
                             f"{minutes_restantes} min")
                    await asyncio.sleep(min(PAUSE_SCAN, pause_jusqua - time.time()))
                    continue

                # ── Fenêtre anti-funding (08/07) — voir dans_fenetre_pre_funding.
                # Pas de Telegram ici (une simple info log toutes les 30s pendant
                # 20 min, 3x/jour, suffit — pas la peine de notifier un
                # comportement normal et prévu).
                if MODE_REEL and dans_fenetre_pre_funding():
                    log.info(f"  ⏸️ Fenêtre pré-funding ({FENETRE_PRE_FUNDING_MIN} min avant "
                             f"00h/08h/16h UTC) — aucune nouvelle ouverture pour éviter le "
                             f"prélèvement de financement sur une position fraîche.")
                    await asyncio.sleep(PAUSE_SCAN)
                    continue

                # Scan des marchés disponibles
                async with trades_lock:
                    slots_libres        = MAX_TRADES_SIMULTANES - len(trades_ouverts)
                    marches_actifs      = get_marches_actifs()
                    # ── Pause auto des marchés qui gappent (12/07) — voir _marche_en_pause_gap.
                    marches_en_pause    = [m for m in marches_actifs if _marche_en_pause_gap(m, etat)]
                    marches_disponibles = [
                        m for m in marches_actifs
                        if m not in trades_ouverts
                        and time.time() >= cooldown_marches.get(m, 0)
                        and not _marche_en_pause_gap(m, etat)   # exclut les marchés qui gappent trop
                        # BTCUSD retiré tant que le capital ne permet pas 1 contrat entier
                        # (ctVal=1 ≈ 1 BTC de notionnel par contrat) — uniquement en
                        # MODE_REEL, où cette contrainte existe réellement. Voir SEUIL_CAPITAL_BTC
                        and not (MODE_REEL and m == "BTCUSD" and etat["capital"] < SEUIL_CAPITAL_BTC)
                    ]

                if marches_en_pause:
                    log.info(f"  ⏸️ En pause (gaps récents) : {', '.join(marches_en_pause)}")

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

                # ── Priorité aux mouvements les PLUS MODÉRÉS (11/07) — quand il
                # y a plus de signaux que de slots libres, on privilégie les
                # sur-réactions modérées (proches du seuil de déclenchement),
                # PAS les mouvements les plus violents. Pour du mean reversion,
                # un mouvement plus large n'est pas un meilleur signal mais un
                # signal plus risqué (plus souvent une vraie tendance). L'ancien
                # tri (reverse=True) faisait l'inverse : il sélectionnait d'abord
                # les pires trades dès que les slots étaient limités. N'a d'effet
                # que si MAX_TRADES_SIMULTANES < nombre de signaux simultanés
                # (donc aucun effet tant que MAX_TRADES_SIMULTANES=999).
                meilleurs = sorted(
                    signaux.items(),
                    key=lambda x: x[1]["details"].get("variation_pct", 0),
                )[:slots_libres]

                if arret_demande:
                    log.info("  Arrêt demandé — aucun nouveau trade ne sera ouvert.")
                    await asyncio.sleep(PAUSE_SCAN)
                    continue

                for symbole, sig in meilleurs:
                    async with trades_lock:
                        if symbole in trades_ouverts:
                            continue
                        if len(trades_ouverts) >= MAX_TRADES_SIMULTANES:
                            break
                        trades_ouverts[symbole] = True

                    log.info(f"  {symbole} ({sig['direction']}) "
                             f"Variation={sig['details'].get('variation_pct', 0):.2f}%")

                    tache = asyncio.create_task(
                        executer_trade(
                            session, symbole, sig["direction"],
                            etat["capital"],
                            sig["details"], etat
                        )
                    )
                    taches_trades_actives.add(tache)
                    tache.add_done_callback(taches_trades_actives.discard)

                await asyncio.sleep(PAUSE_SCAN)

            except KeyboardInterrupt:
                log.info("Bot arrêté.")
                break
            except Exception as e:
                log.error(f"Erreur inattendue : {e}")
                await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(boucle_principale())
