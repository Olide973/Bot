"""
╔══════════════════════════════════════════════════════════════════╗
║         BOT REIVAX284 — V4 — OKX X-Perps x10                    ║
║  Mean Reversion 0.50% | Surveillance prix temps réel            ║
║  Lock Profits Paliers | 23 marchés (3 groupes horaires) | 24h/24 ║
║  Capital 500€ | Architecture async aiohttp | SIMULATION          ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import aiohttp
import os
import json
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
MISE_BASE_PCT           = 0.10
MISE_MIN                = 10.0
MISE_MAX_PCT            = 0.25
CHECK_INTERVAL          = 1          # secondes — lecture du cache WebSocket (quasi instantané, plus de coût REST)
PAUSE_SCAN              = 30         # secondes entre chaque scan de nouveaux marchés
MAX_TRADES_SIMULTANES   = 10         # cap fixe de trades parallèles (23 marchés dispo au total selon l'heure)

# ── Détection signal mean reversion — surveillance temps réel
SEUIL_MOUVEMENT_PCT     = 0.50   # dès que le prix bouge de 0.50% → signal
VOLUME_MINI             = 0.25   # volume min vs moyenne 24h
STOP_LOSS_FIXE          = 2.0    # stop fixe = -2€ par trade (avant frais), ni plus ni moins

# ── Frais réels OKX — identiques pour Swap Perpétuels ET X-Perps, palier standard
#    Maker 0.02% / Taker 0.05% du notionnel (confirmé pour les deux produits).
FRAIS_TAKER_PCT          = 0.05   # % du notionnel, par exécution (ouverture OU fermeture)

# ── Funding (financement) — X-Perps réglés toutes les 8h : 00h, 08h, 16h UTC
FUNDING_HEURES_UTC       = {0, 8, 16}

# ── Filtre RSI 1h
RSI_SEUIL_BAS           = 45     # RSI < 45 → marché baissier → inverser ACHAT en VENTE
RSI_SEUIL_HAUT          = 55     # RSI > 55 → marché haussier → inverser VENTE en ACHAT
RSI_PERIODE             = 14

# ── Protections
KILL_SWITCH_JOUR        = -10.0
SEUIL_RUINE             = 300.0

# ── Lock profits par paliers proportionnels au capital
LOCK_PALIERS_PCT = [0.15, 0.20, 0.30, 0.60, 1.00, 1.60, 2.40, 3.60, 5.00, 7.00, 10.00, 15.00, 20.00, 30.00, 40.00]

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

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# ── Clés API OKX — non utilisées en mode simulation.
#    Prévues pour le jour où le bot passera au trading réel (envoi d'ordres signés).
#    Permissions à créer sur OKX : Lecture (+ Trading plus tard) — JAMAIS Retrait.
OKX_API_KEY         = os.environ.get('OKX_API_KEY', '')
OKX_API_SECRET      = os.environ.get('OKX_API_SECRET', '')
OKX_API_PASSPHRASE  = os.environ.get('OKX_API_PASSPHRASE', '')
MODE_SIMULATION     = True   # passera à False le jour du live — aucun ordre réel tant que True

# ── Reset complet piloté depuis Railway (Variables → RESET_TOUT = 1, puis Deploy)
#    Remet à zéro : capital, PnL jour, kill switch, compteurs, historique.
#    Ne touche PAS à l'historique brut en base (table 'trades') — juste l'état
#    opérationnel du bot. Remettre à 0 (ou supprimer la variable) + redeploy
#    pour revenir en fonctionnement normal sans redéclencher un reset à chaque
#    redémarrage.
RESET_TOUT = os.environ.get('RESET_TOUT', '0').strip().lower() in ('1', 'true', 'oui', 'yes')

# ── 23 marchés en 3 groupes horaires (heure Guyane, UTC-3)
#    Groupe 24H  : toujours actif
#    Groupe NUIT : 00h-09h Guyane
#    Groupe JOUR : 09h-20h Guyane
#    Notes de substitution vs la liste d'origine :
#    - FTM a été rebrandé en Sonic (S) — OKX a délisté le swap FTMUSDT le 07/01/2025
#    - XMR a été délisté d'OKX en janvier 2024 (pression réglementaire MiCA/AMLR) → remplacé par BNB
MARCHES_24H = [
    "ATOMUSDT", "NEARUSDT", "BNBUSDT", "TRXUSDT",
    "UNIUSDT",  "ARBUSDT",  "SONICUSDT", "SUIUSDT",
]
MARCHES_NUIT = [
    "ETHUSDT",  "XRPUSDT",  "SOLUSDT",  "ADAUSDT",
    "LINKUSDT", "AVAXUSDT", "DOTUSDT",  "DOGEUSDT",
    "LTCUSDT",  "ALGOUSDT", "FILUSDT",  "AAVEUSDT",
    "POLUSDT",  "APEUSDT",
]
MARCHES_JOUR = ["TAOUSDT"]

# ── Liste complète (union des 3 groupes) — utilisée pour l'abonnement WebSocket
#    et la vérification des leviers : tous les marchés reçoivent des prix en continu,
#    seul get_marches_actifs() filtre lesquels peuvent ouvrir un nouveau trade
MARCHES = MARCHES_24H + MARCHES_NUIT + MARCHES_JOUR

# ── Valeurs INITIALES (placeholders) — écrasées au tout premier démarrage par
#    decouvrir_et_filtrer_marches_xperp_x10() avec le VRAI instId du X-Perp
#    (ex: BTC-USD-YYMMDD). Ces valeurs "-USDT-SWAP" ne servent qu'à identifier
#    le ticker de base avant la première découverte, et comme repli si jamais
#    l'API X-Perp est indisponible au démarrage (le bot tourne alors sur les
#    Swaps Perpétuels classiques plutôt que de ne pas démarrer du tout).
OKX_SYMBOLS = {
    # Groupe 24H
    "ATOMUSDT":   "ATOM-USDT-SWAP",
    "NEARUSDT":   "NEAR-USDT-SWAP",
    "BNBUSDT":    "BNB-USDT-SWAP",
    "TRXUSDT":    "TRX-USDT-SWAP",
    "UNIUSDT":    "UNI-USDT-SWAP",
    "ARBUSDT":    "ARB-USDT-SWAP",
    "SONICUSDT":  "S-USDT-SWAP",
    "SUIUSDT":    "SUI-USDT-SWAP",
    # Groupe NUIT
    "ETHUSDT":    "ETH-USDT-SWAP",
    "XRPUSDT":    "XRP-USDT-SWAP",
    "SOLUSDT":    "SOL-USDT-SWAP",
    "ADAUSDT":    "ADA-USDT-SWAP",
    "LINKUSDT":   "LINK-USDT-SWAP",
    "AVAXUSDT":   "AVAX-USDT-SWAP",
    "DOTUSDT":    "DOT-USDT-SWAP",
    "DOGEUSDT":   "DOGE-USDT-SWAP",
    "LTCUSDT":    "LTC-USDT-SWAP",
    "ALGOUSDT":   "ALGO-USDT-SWAP",
    "FILUSDT":    "FIL-USDT-SWAP",
    "AAVEUSDT":   "AAVE-USDT-SWAP",
    "POLUSDT":    "POL-USDT-SWAP",
    "APEUSDT":    "APE-USDT-SWAP",
    # Groupe JOUR
    "TAOUSDT":    "TAO-USDT-SWAP",
}

# ── Table inverse : instId OKX → symbole interne (pour router les ticks WebSocket)
OKX_SYMBOLS_INVERSE = {v: k for k, v in OKX_SYMBOLS.items()}

# ── Correspondance interval (minutes) → bar OKX
BAR_MAP = {
    15: "15m",
    60: "1H",
}

def get_marches_actifs():
    """Retourne les marchés actifs selon l'heure Guyane (UTC-3) :
    - Groupe 24H  (8 marchés)  : toujours actif
    - Groupe NUIT (14 marchés) : actif 00h-09h Guyane
    - Groupe JOUR (1 marché)   : actif 09h-20h Guyane
    De 20h à minuit Guyane, seul le groupe 24H reste actif."""
    heure_guyane = (datetime.utcnow() - timedelta(hours=3)).hour
    actifs = list(MARCHES_24H)
    if 0 <= heure_guyane < 9:
        actifs += MARCHES_NUIT
    if 9 <= heure_guyane < 20:
        actifs += MARCHES_JOUR
    return actifs


# ═══════════════════════════════════════════════════════════════
#  ÉTAT GLOBAL
# ═══════════════════════════════════════════════════════════════
trades_ouverts    = {}    # { symbole: True }
prix_reference    = {}    # { symbole: prix_au_moment_du_scan }
cooldown_marches  = {}    # { symbole: timestamp_fin_cooldown }
trades_lock       = None  # initialisé dans boucle_principale()
PRIX_LIVE         = {}    # { symbole: dernier prix reçu via WebSocket OKX }
WS_CONNEXION_ACTIVE = None  # référence à la connexion WebSocket en cours (pour forcer une resynchro)

log.info("=" * 60)
log.info("  BOT REIVAX284 — V4 — OKX X-Perps x10")
log.info(f"  Capital : {CAPITAL_INITIAL}€ | Levier x{LEVIER}")
log.info(f"  Marchés : {len(MARCHES)} cryptos | 24h/24 — 7j/7")
log.info(f"  Signal : mouvement ≥ {SEUIL_MOUVEMENT_PCT}% depuis le prix de référence")
log.info(f"  RSI 1h : seuil bas={RSI_SEUIL_BAS} | seuil haut={RSI_SEUIL_HAUT}")
log.info(f"  Stop : fixe {STOP_LOSS_FIXE}€ par trade (avant frais)")
log.info(f"  Frais OKX : -{FRAIS_TAKER_PCT}% taker par exécution (ouverture + fermeture)")
log.info(f"  Prix temps réel : WebSocket OKX (canal tickers)")
log.info(f"  Kill switch : {KILL_SWITCH_JOUR}€/jour | Ruine : {SEUIL_RUINE}€")
log.info(f"  Pas de timeout — trades ouverts jusqu'au stop ou au lock")
log.info(f"  Mode : {'SIMULATION' if MODE_SIMULATION else 'LIVE'} (aucun ordre réel envoyé à OKX tant que SIMULATION)")
log.info(f"  Clés API OKX : {'configurées' if (OKX_API_KEY and OKX_API_SECRET and OKX_API_PASSPHRASE) else 'non configurées (inutile en simulation)'}")
log.info(f"  Telegram : {'ON' if TELEGRAM_TOKEN else 'OFF'}")
log.info("=" * 60)

# ═══════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════
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
    bar = BAR_MAP.get(interval, "15m")
    url = "https://www.okx.com/api/v5/market/candles"
    try:
        async with session.get(
            url,
            params={"instId": okx_symbol, "bar": bar, "limit": str(limite)},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                return None
            candles = data.get("data", [])
            if not candles:
                return None
            # OKX renvoie les bougies du plus récent au plus ancien → on inverse
            candles = list(reversed(candles))
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
    """Prix via l'API REST OKX (fallback tant que le WebSocket n'a pas encore poussé de tick)."""
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
    Si aucun tick n'a encore été reçu pour ce marché (juste après démarrage
    ou pendant une reconnexion), on retombe sur un appel REST ponctuel."""
    prix_ws = PRIX_LIVE.get(symbole)
    if prix_ws is not None:
        return prix_ws
    return await get_prix_rest(session, symbole)

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
    Se reconnecte automatiquement en cas de coupure, et relit MARCHES à chaque
    reconnexion (donc prend en compte les marchés ajoutés/retirés entre-temps)."""
    global WS_CONNEXION_ACTIVE
    url = "wss://ws.okx.com:8443/ws/v5/public"

    while True:
        keepalive_task = None
        # Reconstruit l'abonnement à CHAQUE tentative de connexion — pas une
        # seule fois au démarrage — pour suivre les marchés ajoutés/retirés
        # par decouvrir_et_filtrer_marches_xperp_x10() entre deux connexions.
        args = [{"channel": "tickers", "instId": OKX_SYMBOLS[m]} for m in MARCHES]
        try:
            async with session.ws_connect(url, heartbeat=25) as ws:
                WS_CONNEXION_ACTIVE = ws
                await ws.send_json({"op": "subscribe", "args": args})
                keepalive_task = asyncio.create_task(_ws_keepalive(ws))
                log.info(f"  🔌 WebSocket OKX connecté — abonnement tickers ({len(MARCHES)} marchés)")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        if msg.data == "pong":
                            continue
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        if data.get("event") == "subscribe":
                            continue
                        if data.get("event") == "error":
                            log.error(f"  Erreur abonnement WebSocket : {data}")
                            continue
                        if "data" in data and "arg" in data:
                            inst_id = data["arg"].get("instId")
                            symbole = OKX_SYMBOLS_INVERSE.get(inst_id)
                            ticks   = data.get("data", [])
                            if symbole and ticks:
                                try:
                                    PRIX_LIVE[symbole] = float(ticks[0]["last"])
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

NOUVEAUX_MARCHES_MAX = 20  # garde-fou : évite une explosion du nombre de marchés scannés/appels API

async def get_funding_rate(session, inst_id):
    """Taux de financement actuel d'un instrument (public, sans clé API).
    Fonctionne pour les X-Perps comme pour les Swaps Perpétuels classiques."""
    try:
        async with session.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": inst_id},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("code") != "0":
                return None
            result = data.get("data", [])
            if not result:
                return None
            return float(result[0]["fundingRate"])
    except Exception as e:
        log.error(f"Erreur funding rate {inst_id} : {e}")
        return None

async def decouvrir_et_filtrer_marches_xperp_x10(session):
    """Construit la liste des marchés à partir de TOUS les X-Perps OKX supportant
    réellement le levier x{LEVIER} (API publique, instType=FUTURES, ruleType=xperp)
    — pas seulement les marchés déjà codés en dur dans le bot.

    IMPORTANT : à partir d'ici, OKX_SYMBOLS pointe vers le VRAI instId du X-Perp
    (ex: BTC-USD-YYMMDD), pas vers un Swap Perpétuel classique. C'est ce même
    instId qui alimente ensuite les prix, les chandelles, le WebSocket et le
    funding en simulation — pour coller au plus près du produit réellement
    tradable en live sur le compte France/EEA.

    Étapes :
    1) Retire les marchés déjà connus qui n'ont plus de X-Perp x{LEVIER} valide,
       et met à jour leur instId vers le X-Perp actuel (peut changer dans le temps).
    2) Découvre les bases qui ont un X-Perp x{LEVIER} mais qu'on ne suit pas
       encore, et les ajoute directement au groupe 24H.
    3) Plafonne les ajouts à NOUVEAUX_MARCHES_MAX pour ne pas saturer l'API OKX
       ni le nombre de scans par cycle.

    Si l'appel API échoue, la liste et les instId d'origine sont conservés
    intégralement par sécurité (repli sur le dernier état connu, pas de perte
    de marché à l'aveugle)."""
    global MARCHES_24H, MARCHES_NUIT, MARCHES_JOUR, MARCHES, OKX_SYMBOLS, OKX_SYMBOLS_INVERSE

    url_instruments = "https://www.okx.com/api/v5/public/instruments"
    try:
        async with session.get(
            url_instruments,
            params={"instType": "FUTURES"},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
    except Exception as e:
        log.error(f"Erreur découverte X-Perps x{LEVIER} : {e} — liste d'origine conservée")
        return

    if data.get("code") != "0" or not data.get("data"):
        log.warning("  ⚠️ Découverte X-Perps impossible (réponse OKX invalide) — liste d'origine conservée")
        return

    # Filet de sécurité supplémentaire : tickers TradFi confirmés (lancement
    # officiel OKX du 9 juin 2026 : Magnificent 7 + SPY/QQQ + 4 matières
    # premières = 13 marchés). C'est la protection PRINCIPALE désormais.
    TRADFI_DENYLIST = {
        "AAPL", "AMZN", "GOOGL", "GOOG", "META", "MSFT", "NVDA", "TSLA",  # Magnificent 7
        "SPY", "QQQ",                                                     # indices
        "GC", "SI", "CL", "BZ",                                          # or, argent, WTI, Brent
        "CRCL",                                                          # Circle Internet Group (action NYSE, pas l'USDC)
        "EWY", "EWJ", "EWZ", "EWG", "DIA", "IWM",                        # ETF pays/indices additionnels par précaution
    }
    # Valeurs CONNUES et CONFIRMÉES du champ instCategory pour du non-crypto.
    # On exclut seulement sur correspondance explicite — pas sur "champ non
    # vide", car les cryptos ont aussi une catégorie renseignée côté OKX
    # (leçon d'un bug précédent qui avait exclu TOUT, cryptos comprises).
    CATEGORIES_NON_CRYPTO = {"stocks", "commodities", "commodity", "etf", "indices", "index", "equity", "equities"}

    # Bases avec X-Perp CRYPTO valide : existe, pas taguée non-crypto, et pas
    # dans la liste de blocage manuelle, ET levier max >= LEVIER.
    xperps_crypto = {}   # base → instrument, TOUTES les cryptos avec X-Perp (quel que soit le levier)
    for d in data.get("data", []):
        if d.get("ruleType") != "xperp":
            continue
        categorie = str(d.get("instCategory", "")).strip().lower()
        if categorie in CATEGORIES_NON_CRYPTO:
            continue
        base = d.get("instId", "").split("-")[0]
        if not base or base in TRADFI_DENYLIST:
            continue
        xperps_crypto[base] = d

    xperps = {}   # sous-ensemble : crypto ET levier max >= LEVIER
    for base, d in xperps_crypto.items():
        try:
            lever_max = float(d.get("lever", 0))
        except (TypeError, ValueError):
            continue
        if lever_max >= LEVIER:
            xperps[base] = d

    # ── Garde-fou critique : si la découverte trouve anormalement peu de
    #    marchés crypto éligibles (bug de filtrage, réponse OKX incomplète...),
    #    on N'APPLIQUE AUCUN changement plutôt que de risquer de vider la liste
    #    active. Seuil arbitraire mais sûr : on avait déjà confirmé au moins
    #    10 marchés crypto x10 sur OKX, donc bien en dessous = anomalie.
    SEUIL_MIN_MARCHES_SURS = 5
    if len(xperps) < SEUIL_MIN_MARCHES_SURS:
        log.error(f"  ❌ ANOMALIE : seulement {len(xperps)} marché(s) crypto x{LEVIER} trouvé(s) "
                  f"(seuil de sécurité : {SEUIL_MIN_MARCHES_SURS}) — liste actuelle conservée intégralement")
        await telegram(session,
            f"🐉❌ <b>ANOMALIE DÉCOUVERTE X-PERP</b>\n"
            f"Seulement {len(xperps)} marché(s) crypto trouvé(s), en dessous du seuil de sécurité.\n"
            f"Aucun changement appliqué — liste actuelle conservée.\n"
            f"Vérifie les logs Railway."
        )
        return

    def base_actuelle(symbole):
        """Ticker de base déduit de l'instId actuellement connu (X-Perp d'un
        run précédent, ou Swap placeholder au tout premier démarrage)."""
        okx_symbol = OKX_SYMBOLS.get(symbole, "")
        return okx_symbol.split("-")[0] if okx_symbol else ""

    # 1) Retirer les marchés obsolètes + rebasculer les survivants sur leur
    #    instId X-Perp réel et à jour (le contrat peut changer dans le temps)
    avant = MARCHES_24H + MARCHES_NUIT + MARCHES_JOUR

    def filtrer_et_mettre_a_jour(groupe):
        conserves = []
        for symbole in groupe:
            base = base_actuelle(symbole)
            if base in xperps:
                OKX_SYMBOLS[symbole] = xperps[base]["instId"]
                conserves.append(symbole)
        return conserves

    MARCHES_24H  = filtrer_et_mettre_a_jour(MARCHES_24H)
    MARCHES_NUIT = filtrer_et_mettre_a_jour(MARCHES_NUIT)
    MARCHES_JOUR = filtrer_et_mettre_a_jour(MARCHES_JOUR)
    retires = [m for m in avant if m not in (MARCHES_24H + MARCHES_NUIT + MARCHES_JOUR)]

    # Détail de la raison du retrait : X-Perp absent, ou présent mais levier < x{LEVIER}
    retires_detail = []
    for m in retires:
        base = base_actuelle(m)
        if base in xperps_crypto:
            lever_reel = xperps_crypto[base].get("lever", "?")
            retires_detail.append(f"{m} (X-Perp existe mais levier max=x{lever_reel})")
        else:
            retires_detail.append(f"{m} (aucun X-Perp crypto trouvé)")

    # 2) Découvrir les bases éligibles qu'on ne suit pas encore
    bases_connues    = {base_actuelle(m) for m in (MARCHES_24H + MARCHES_NUIT + MARCHES_JOUR)}
    nouvelles_bases  = sorted(b for b in xperps if b not in bases_connues)
    ignorees_par_cap = nouvelles_bases[NOUVEAUX_MARCHES_MAX:]
    nouvelles_bases  = nouvelles_bases[:NOUVEAUX_MARCHES_MAX]

    ajoutes = []
    for base in nouvelles_bases:
        symbole = f"{base}USD"  # X-Perps sont cotés en USD, pas en USDT
        OKX_SYMBOLS[symbole] = xperps[base]["instId"]
        MARCHES_24H.append(symbole)
        ajoutes.append(symbole)

    OKX_SYMBOLS_INVERSE = {v: k for k, v in OKX_SYMBOLS.items()}
    MARCHES = MARCHES_24H + MARCHES_NUIT + MARCHES_JOUR

    if retires:
        log.warning(f"  🚫 Marchés retirés : {' | '.join(retires_detail)}")
    if ajoutes:
        log.info(f"  ➕ Nouveaux marchés découverts (X-Perp x{LEVIER} confirmé) : {', '.join(ajoutes)}")
    if ignorees_par_cap:
        log.info(f"  ⏭️ {len(ignorees_par_cap)} marché(s) supplémentaire(s) ignoré(s) (cap à {NOUVEAUX_MARCHES_MAX}/scan) : {', '.join(ignorees_par_cap)}")
    log.info(f"  Marchés actifs après découverte : {len(MARCHES)} (dont {len(ajoutes)} nouveaux, {len(retires)} retirés)")

    await telegram(session,
        f"🐉🔧 <b>DÉCOUVERTE + FILTRAGE X-PERP x{LEVIER}</b>\n"
        f"Total marchés actifs : <b>{len(MARCHES)}</b>\n\n"
        f"➕ Ajoutés ({len(ajoutes)}) : {', '.join(ajoutes) if ajoutes else 'aucun'}\n\n"
        f"🚫 Retirés ({len(retires)}) : {', '.join(retires) if retires else 'aucun'}"
        + (f"\n\n⏭️ {len(ignorees_par_cap)} ignoré(s) par le cap ({NOUVEAUX_MARCHES_MAX} max/scan)" if ignorees_par_cap else "")
        + f"\n\n📋 Liste complète actuelle ({len(MARCHES)}) :\n{', '.join(sorted(MARCHES))}"
    )

    # Si la liste de marchés a changé, force une reconnexion WebSocket pour que
    # les nouveaux marchés/instId reçoivent des prix temps réel immédiatement —
    # sinon ils resteraient sur le fallback REST jusqu'à la prochaine coupure.
    if (ajoutes or retires) and WS_CONNEXION_ACTIVE is not None and not WS_CONNEXION_ACTIVE.closed:
        log.info("  🔌 Resynchronisation WebSocket forcée (liste de marchés modifiée)")
        try:
            await WS_CONNEXION_ACTIVE.close()
        except Exception as e:
            log.error(f"Erreur lors de la resynchronisation WebSocket : {e}")

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

    # Signal ACHAT : prix a chuté de ≥ 0.50%
    if variation_pct <= -SEUIL_MOUVEMENT_PCT:
        prix_reference[symbole] = prix_actuel
        if rsi_1h < RSI_SEUIL_BAS:
            log.info(f"  {symbole} 🔄 ACHAT→VENTE | RSI={rsi_1h} < {RSI_SEUIL_BAS} | Vol={vol_ratio:.2f}x")
            return "VENTE", details
        else:
            log.info(f"  {symbole} ✅ ACHAT | Chute={variation_pct:.2f}% | RSI={rsi_1h} | Vol={vol_ratio:.2f}x")
            return "ACHAT", details

    # Signal VENTE : prix a monté de ≥ 0.50%
    if variation_pct >= SEUIL_MOUVEMENT_PCT:
        prix_reference[symbole] = prix_actuel
        if rsi_1h > RSI_SEUIL_HAUT:
            log.info(f"  {symbole} 🔄 VENTE→ACHAT | RSI={rsi_1h} > {RSI_SEUIL_HAUT} | Vol={vol_ratio:.2f}x")
            return "ACHAT", details
        else:
            log.info(f"  {symbole} ✅ VENTE | Montée={variation_pct:.2f}% | RSI={rsi_1h} | Vol={vol_ratio:.2f}x")
            return "VENTE", details

    log.info(f"  {symbole} : Variation={variation_pct:+.2f}% (seuil ±{SEUIL_MOUVEMENT_PCT}%) | RSI={rsi_1h}")
    return "NEUTRE", {}

# ═══════════════════════════════════════════════════════════════
#  GESTION MISE DYNAMIQUE
# ═══════════════════════════════════════════════════════════════
def calculer_mise(capital, etat):
    wins_consec  = etat.get("wins_consecutifs", 0)

    mise = capital * MISE_BASE_PCT

    # Boost après plusieurs gains consécutifs
    if wins_consec >= WINS_CONFIANCE:
        mise *= BOOST_CONFIANCE
        log.info(f"  💪 Mise boostée +20% ({wins_consec} wins consécutifs)")

    mise = max(mise, MISE_MIN)
    mise = min(mise, capital * MISE_MAX_PCT)
    return round(mise, 2)

# ═══════════════════════════════════════════════════════════════
#  EXÉCUTION D'UN TRADE (SIMULATION)
# ═══════════════════════════════════════════════════════════════
async def executer_trade(session, symbole, direction, capital, details, etat_global):
    prix_entree = await get_prix_actuel(session, symbole)
    if prix_entree is None or prix_entree <= 0:
        async with trades_lock:
            trades_ouverts.pop(symbole, None)
        return

    mise = calculer_mise(capital, etat_global)

    # Frais réels OKX (taker) — ouverture ET fermeture, sur le notionnel (mise × levier)
    notionnel        = mise * LEVIER
    frais_unitaire   = round(notionnel * FRAIS_TAKER_PCT / 100, 4)
    frais_total_prevu = round(frais_unitaire * 2, 2)  # ouverture + fermeture

    # Stop loss fixe : -2€ par trade (avant frais), ni plus ni moins
    stop_loss_eur = STOP_LOSS_FIXE

    rsi_1h = details.get("rsi_1h", 50.0)

    # Calcul stop et objectif en prix
    ratio_prix = stop_loss_eur / (mise * LEVIER) if (mise * LEVIER) > 0 else 0.001
    if direction == "ACHAT":
        stop_initial   = round(prix_entree * (1 - ratio_prix), 8)
        objectif_final = round(prix_entree * (1 + ratio_prix * 2), 8)
    else:
        stop_initial   = round(prix_entree * (1 + ratio_prix), 8)
        objectif_final = round(prix_entree * (1 - ratio_prix * 2), 8)

    # Numéro de trade — sera attribué dans le lock final
    numero_trade = 0

    log.info(f"\n  {'='*55}")
    log.info(f"  TRADE EN COURS [REIVAX284 V4 OKX] — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  {symbole} ({direction})")
    log.info(f"  Variation : {details.get('variation_pct', 0):.2f}% | "
             f"Ref={details.get('prix_ref')} → {details.get('prix_actuel')}")
    log.info(f"  Vol={details.get('vol_ratio', 0):.2f}x | RSI 1h={rsi_1h} | Stop fixe : -{stop_loss_eur}€")
    log.info(f"  Prix entrée : {prix_entree} | Stop : {stop_initial} | Obj : {objectif_final}")
    log.info(f"  Mise : {mise}€ × x{LEVIER} = {round(mise*LEVIER,2)}€ | Frais estimés (ouv+ferm) : -{frais_total_prevu}€ | Trades : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}\n")

    await telegram(session,
        f"🐉📊 <b>TRADE OUVERT — REIVAX284 V4 OKX</b>\n"
        f"{'🟢 ACHAT' if direction == 'ACHAT' else '🔴 VENTE'} {symbole}\n"
        f"Variation : {details.get('variation_pct', 0):.2f}% depuis ref\n"
        f"Volume : {details.get('vol_ratio', 0):.2f}x | RSI 1h : {rsi_1h}\n"
        f"Prix : {prix_entree} | Stop : {stop_initial}\n"
        f"Mise : {mise}€ × x{LEVIER} | Stop max : -{stop_loss_eur}€\n"
        f"Frais estimés (ouv+ferm) : -{frais_total_prevu}€\n"
        f"Trades : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}"
    )

    debut           = time.time()
    dernier_log     = 0
    pnl_max_atteint = 0.0
    lock_actuel     = 0.0
    resultat_final  = "PERDU"
    gain_final      = -STOP_LOSS_FIXE
    prix_sortie     = prix_entree
    pnl             = 0.0
    duree           = 0

    # ── Funding (financement) — réglé toutes les 8h (00h/08h/16h UTC) sur les
    #    X-Perps comme sur les Swaps Perpétuels classiques. C'est un paiement
    #    entre positions longues et courtes, pas un frais prélevé par OKX, mais
    #    il impacte directement le gain net si le trade reste ouvert à cheval
    #    sur un de ces horaires.
    dernier_funding_applique = None  # (date, heure UTC) du dernier funding déjà compté
    funding_cumule            = 0.0

    # ── Boucle de surveillance — sans timeout
    while True:
        await asyncio.sleep(CHECK_INTERVAL)

        prix_actuel = await get_prix_actuel(session, symbole)
        if prix_actuel is None:
            continue

        prix_sortie = prix_actuel

        # Calcul PnL
        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)

        if pnl > pnl_max_atteint:
            pnl_max_atteint = pnl

        # Funding : vérifie si on vient de franchir un horaire de règlement
        # (00h/08h/16h UTC) pendant que la position est ouverte
        maintenant_utc_check = datetime.utcnow()
        if maintenant_utc_check.hour in FUNDING_HEURES_UTC and maintenant_utc_check.minute == 0:
            cle_funding = (maintenant_utc_check.date(), maintenant_utc_check.hour)
            if cle_funding != dernier_funding_applique:
                dernier_funding_applique = cle_funding
                taux_funding = await get_funding_rate(session, OKX_SYMBOLS[symbole])
                if taux_funding is not None:
                    notionnel_actuel = mise * LEVIER
                    paiement = round(notionnel_actuel * taux_funding, 4)
                    # Taux positif → les longs paient les shorts (et inversement)
                    impact = -paiement if direction == "ACHAT" else paiement
                    funding_cumule += impact
                    log.info(f"  💰 Funding [{symbole}] taux={taux_funding*100:.4f}% | "
                             f"Impact={'+' if impact>=0 else ''}{round(impact,4)}€ | "
                             f"Cumulé={'+' if funding_cumule>=0 else ''}{round(funding_cumule,4)}€")

        # Lock paliers
        nouveau_lock = get_palier_lock(pnl_max_atteint, capital)
        if nouveau_lock > lock_actuel:
            lock_actuel = nouveau_lock
            log.info(f"  🔒 LOCK {lock_actuel}€ GARANTI [{symbole}] (PnL max={pnl_max_atteint:.2f}€)")
            await telegram(session,
                f"🐉🔒 <b>{lock_actuel}€ garanti !</b>\n"
                f"{symbole} | PnL max : +{pnl_max_atteint:.2f}€\n"
                f"Gain verrouillé ✅"
            )

        # Sortie lock : PnL redescend sous le palier verrouillé
        if lock_actuel > 0 and pnl < lock_actuel:
            duree = int((time.time() - debut) / 60)
            log.info(f"\n  🔒 SORTIE LOCK [{symbole}] +{lock_actuel}€ (max={pnl_max_atteint:.2f}€) | {duree}min")
            await telegram(session,
                f"🐉🔒 <b>SORTIE LOCK</b>\n"
                f"{symbole} | {direction}\n"
                f"Gain : <b>+{lock_actuel}€</b>\n"
                f"PnL max : +{pnl_max_atteint:.2f}€\n"
                f"Durée : {duree} min"
            )
            resultat_final = "GAGNE"
            gain_final     = lock_actuel
            break

        # Stop loss
        atteint_stop = (prix_actuel <= stop_initial if direction == "ACHAT"
                        else prix_actuel >= stop_initial)

        duree = int((time.time() - debut) / 60)

        # Log toutes les minutes
        if time.time() - dernier_log >= 60:
            lock_flag = f" 🔒{lock_actuel}€" if lock_actuel > 0 else ""
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole} {prix_actuel} | "
                     f"PnL {'+' if pnl>=0 else ''}{pnl:.2f}€{lock_flag} | {duree}min")
            dernier_log = time.time()

        if atteint_stop:
            log.info(f"\n  🛑 STOP [{symbole}] {'+' if pnl>=0 else ''}{pnl:.2f}€ | {duree}min")
            await telegram(session,
                f"🐉🛑 <b>STOP</b>\n"
                f"{symbole} {direction}\n"
                f"Résultat brut : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                f"Durée : {duree} min"
            )
            gain_final = pnl
            break

    # ── Déduction des frais réels OKX + funding cumulé sur le gain brut
    #    Le résultat GAGNE/PERDU est déterminé sur le gain NET, pas le brut :
    #    un petit gain brut peut devenir négatif une fois frais et funding déduits.
    gain_brut       = gain_final
    funding_cumule  = round(funding_cumule, 2)
    gain_final      = round(gain_final - frais_total_prevu + funding_cumule, 2)
    resultat_final  = "GAGNE" if gain_final > 0 else "PERDU"
    log.info(f"  💸 Frais -{frais_total_prevu}€ | Funding {'+' if funding_cumule>=0 else ''}{funding_cumule}€ | "
             f"Gain brut {'+' if gain_brut>=0 else ''}{gain_brut}€ → Net {'+' if gain_final>=0 else ''}{gain_final}€ ({resultat_final})")

    # ── Libérer le marché + mise à jour état global dans un seul lock
    async with trades_lock:
        trades_ouverts.pop(symbole, None)
        cooldown_marches.pop(symbole, None)
        log.info(f"  ✅ [{symbole}] libéré")

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
            'frais':         frais_total_prevu,
            'funding':       funding_cumule,
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
        'frais':         frais_total_prevu,
        'funding':       funding_cumule,
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
        f"🐉📈 <b>RAPPORT REIVAX284 OKX — Trade #{numero_trade}</b>\n"
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
        log.critical(f"🚨 SEUIL RUINE ! Capital {capital}€ → ARRÊT")
        return "RUINE"
    if etat.get("pnl_jour", 0.0) <= KILL_SWITCH_JOUR:
        log.warning(f"⚠️ KILL SWITCH — PnL jour {etat.get('pnl_jour', 0)}€")
        return "KILL_SWITCH"
    return "OK"

def reset_pnl_jour_si_nouveau_jour(etat):
    maintenant_guyane = datetime.utcnow() - timedelta(hours=3)
    aujourd_hui = maintenant_guyane.strftime('%Y-%m-%d')
    if etat.get("date_jour", "") != aujourd_hui:
        etat["pnl_jour"]         = 0.0
        etat["wins_consecutifs"] = 0
        etat["date_jour"]        = aujourd_hui
        log.info("  📅 Nouveau jour — PnL et wins consécutifs remis à 0")

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

    gains_jour  = {}
    wins_jour   = {}
    pertes_jour = {}
    rsi_jour    = {}
    duree_wins  = []
    duree_pertes = []
    vol_wins    = []
    vol_pertes  = []
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
                f'REIVAX284 V4 OKX — Journee du {date_affich}\n'
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
        f"🐉📊 <b>RAPPORT QUOTIDIEN REIVAX284 OKX</b>\n"
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
            f'REIVAX284 V4 OKX — Progression du capital\n'
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
        f"🐉 <b>RAPPORT HEBDOMADAIRE REIVAX284 OKX</b>\n"
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
    log.info(f"  BOT REIVAX284 — V4 — OKX")
    log.info(f"  {'='*55}")
    log.info(f"  Capital    : {round(etat['capital'],2)}€ ({'+' if perf>=0 else ''}{round(perf,2)}%)")
    log.info(f"  PnL jour   : {'+' if etat.get('pnl_jour',0)>=0 else ''}{round(etat.get('pnl_jour',0),2)}€")
    log.info(f"  Trades     : {nb_trades} | Wins : {nb_wins} ({win_rate:.1f}%)")
    log.info(f"  Ouverts    : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}")
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
        ("dernier_scan_xperp", ""),
    ]:
        if champ not in etat:
            etat[champ] = valeur

    afficher_tableau_de_bord(etat)

    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector) as session:
        log.info("  🔧 Découverte et filtrage des marchés X-Perp x10 (OKX complet)...")
        await decouvrir_et_filtrer_marches_xperp_x10(session)

        # Lance le flux WebSocket temps réel en tâche de fond (reconnexion auto en cas de coupure)
        asyncio.create_task(websocket_prix(session))
        log.info("  ⏳ Attente des premiers ticks WebSocket (3s)...")
        await asyncio.sleep(3)

        await telegram(session,
            (f"🐉🔄 <b>RESET COMPLET EFFECTUÉ</b>\nCapital, PnL, compteurs et historique remis à zéro.\n\n" if RESET_TOUT else "")
            + f"🐉🚀 <b>BOT REIVAX284 V4 OKX DÉMARRÉ (SIMULATION)</b>\n"
            f"Capital : {round(etat['capital'],2)}€\n"
            f"{len(MARCHES)} marchés X-Perps x{LEVIER} (3 groupes horaires Guyane) | 24h/24 — 7j/7\n"
            f"Prix temps réel : WebSocket OKX (instId X-Perp réels)\n"
            f"Frais : -{FRAIS_TAKER_PCT}% taker (ouv+ferm) | Funding simulé (00h/08h/16h UTC)\n"
            f"Signal : mouvement ≥ {SEUIL_MOUVEMENT_PCT}%\n"
            f"Kill switch : {KILL_SWITCH_JOUR}€/jour\n"
            f"{(datetime.utcnow() - timedelta(hours=3)).strftime('%Y-%m-%d %H:%M:%S')}"
        )

        while True:
            try:
                reset_pnl_jour_si_nouveau_jour(etat)

                maintenant_utc = datetime.utcnow()

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

                # Découverte + filtrage X-Perp x10 — une fois par jour à minuit Guyane (3h UTC)
                if (maintenant_utc.hour == 3 and
                    maintenant_utc.minute < 1 and
                    etat.get("dernier_scan_xperp", "") != maintenant_utc.strftime('%Y-%m-%d')):
                    log.info("  🔧 Vérification quotidienne des marchés X-Perp x10...")
                    await decouvrir_et_filtrer_marches_xperp_x10(session)
                    etat["dernier_scan_xperp"] = maintenant_utc.strftime('%Y-%m-%d')
                    sauvegarder_etat(etat)

                # Vérification protections
                statut = verifier_protections(etat, etat["capital"])
                if statut == "RUINE":
                    await telegram(session,
                        f"🐉🚨 <b>SEUIL RUINE !</b>\nCapital : {etat['capital']}€\nBot arrêté !")
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
                         f"| Marchés dispo : {len(marches_disponibles)}")

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

                    log.info(f"  ✅ {symbole} ({sig['direction']}) "
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
