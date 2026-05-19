"""
╔══════════════════════════════════════════════════════════════════╗
║         BOT HUMAIN — OLIDE973 V4                                ║
║  Mean Reversion 0.50% | Surveillance prix temps réel            ║
║  Lock Profits Paliers | 20 trades simultanés                    ║
║  Capital 1000€ | Architecture async aiohttp                     ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import aiohttp
import os
import logging
import time
from datetime import datetime, timezone
import pandas as pd
from ta.volatility import AverageTrueRange
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
CAPITAL_INITIAL         = 1000.0
LEVIER                  = 10
MISE_BASE_PCT           = 0.10
MISE_MIN                = 10.0
MISE_MAX_PCT            = 0.25
CHECK_INTERVAL          = 10         # secondes entre chaque check prix
PAUSE_SCAN              = 30         # secondes entre chaque scan de nouveaux marchés
TIMEOUT_TRADE           = 12 * 3600  # 12h max par trade
MAX_TRADES_SIMULTANES   = 20

# ── Détection signal mean reversion — surveillance temps réel
SEUIL_MOUVEMENT_PCT     = 0.50   # dès que le prix bouge de 0.50% → signal
VOLUME_MINI             = 0.25   # volume min vs moyenne 24h
STOP_LOSS_PCT           = 2.0    # perte maximum = 2% du capital (comme les paliers)

# ── Lock profits par paliers proportionnels au capital
# Les paliers s'adaptent automatiquement selon le capital actuel
# Exprimés en % du capital
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
REDUCTION_PERTES        = 0.50
MIN_TRADES_KELLY        = 30
KELLY_FRACTION          = 0.25
KELLY_CAP               = 0.20

# ── Protections
KILL_SWITCH_JOUR        = -50.0
SEUIL_RUINE             = 600.0
MAX_PERTES_CONSECUTIVES = 2
COOLDOWN_PERTES         = 1800   # 30 min

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

MARCHES = [
    "ETHUSDT",  "XRPUSDT",  "SOLUSDT",  "ADAUSDT",
    "LINKUSDT", "ATOMUSDT", "AVAXUSDT", "NEARUSDT",
    "DOTUSDT",  "DOGEUSDT", "LTCUSDT",  "ALGOUSDT",
    "TRXUSDT",  "UNIUSDT",  "FILUSDT",  "AAVEUSDT",
    "POLUSDT",  "APEUSDT",  "ARBUSDT",  "FTMUSDT"
]

KRAKEN_SYMBOLS = {
    "ETHUSDT":   "XETHZUSD",
    "XRPUSDT":   "XXRPZUSD",
    "SOLUSDT":   "SOLUSD",
    "ADAUSDT":   "ADAUSD",
    "LINKUSDT":  "LINKUSD",
    "ATOMUSDT":  "ATOMUSD",
    "AVAXUSDT":  "AVAXUSD",
    "NEARUSDT":  "NEARUSD",
    "DOTUSDT":   "DOTUSD",
    "DOGEUSDT":  "XDGUSD",
    "LTCUSDT":   "XLTCZUSD",
    "ALGOUSDT":  "ALGOUSD",
    "TRXUSDT":   "TRXUSD",
    "UNIUSDT":   "UNIUSD",
    "FILUSDT":   "FILUSD",
    "AAVEUSDT":  "AAVEUSD",
    "POLUSDT":   "POLUSD",    # MATIC rebrandé POL sur Kraken
    "APEUSDT":   "APEUSD",
    "ARBUSDT":   "ARBUSD",    # Arbitrum — très liquide
    "FTMUSDT":   "FTMUSD",    # Fantom — bonne liquidité
}

# ═══════════════════════════════════════════════════════════════
#  ÉTAT GLOBAL
# ═══════════════════════════════════════════════════════════════
trades_ouverts  = {}    # { symbole: True }
prix_reference  = {}    # { symbole: prix_au_moment_du_scan }
trades_lock     = None  # initialisé dans boucle_principale()

log.info("=" * 60)
log.info("  BOT HUMAIN — OLIDE973 V4")
log.info(f"  Capital : {CAPITAL_INITIAL}€ | Levier x{LEVIER}")
log.info(f"  Marchés : {len(MARCHES)} cryptos | Max {MAX_TRADES_SIMULTANES} trades")
log.info(f"  Signal : mouvement ≥ {SEUIL_MOUVEMENT_PCT}% depuis le prix de référence")
log.info(f"  Surveillance temps réel — peu importe la durée")
log.info(f"  Lock paliers : {LOCK_PALIERS_PCT}% du capital")
log.info(f"  Kill switch : {KILL_SWITCH_JOUR}€/jour | Ruine : {SEUIL_RUINE}€")
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
#  DONNÉES MARCHÉ
# ═══════════════════════════════════════════════════════════════
async def get_klines(session, symbole, interval=15, limite=50):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url = "https://api.kraken.com/0/public/OHLC"
    try:
        async with session.get(
            url,
            params={"pair": kraken_symbol, "interval": interval},
            timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            data = await resp.json()
            if data.get("error"):
                return None
            result = data.get("result", {})
            keys = [k for k in result.keys() if k != "last"]
            if not keys:
                return None
            candles = result[keys[0]]
            df = pd.DataFrame(candles, columns=[
                'time','open','high','low','close','vwap','volume','count'
            ])
            df = df.astype({
                'open': float, 'high': float, 'low': float,
                'close': float, 'volume': float
            })
            return df.tail(limite).reset_index(drop=True)
    except Exception as e:
        log.error(f"Erreur klines {symbole} : {e}")
        return None

async def get_prix_actuel(session, symbole):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    try:
        async with session.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": kraken_symbol},
            timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            data = await resp.json()
            if data.get("error") and data["error"]:
                return None
            result = data.get("result", {})
            if not result:
                return None
            key = list(result.keys())[0]
            return float(result[key]["c"][0])
    except Exception as e:
        log.error(f"Erreur prix {symbole} : {e}")
        return None

# ═══════════════════════════════════════════════════════════════
#  INDICATEURS
# ═══════════════════════════════════════════════════════════════
def calc_atr(df, periode=14):
    try:
        val = AverageTrueRange(
            high=df['high'], low=df['low'], close=df['close'], window=periode
        ).average_true_range().iloc[-1]
        return round(float(val), 8) if not pd.isna(val) else 0.0
    except:
        return 0.0

def calc_volume_ratio(df):
    """Ratio bougie fermée vs moyenne 24h."""
    try:
        volumes = df['volume'].tolist()
        if len(volumes) < 10:
            return 0.0
        moyenne = sum(volumes[-25:-1]) / 24
        recent  = volumes[-2]
        return round(recent / moyenne, 2) if moyenne > 0 else 0.0
    except:
        return 0.0

# ═══════════════════════════════════════════════════════════════
#  DÉTECTION SIGNAL — SURVEILLANCE TEMPS RÉEL
#  1. Prix de référence enregistré au démarrage
#  2. Dès que variation ≥ 0.50% → signal
#  3. Filtre volume > 0.25x
# ═══════════════════════════════════════════════════════════════
async def analyser_marche(session, symbole):
    prix_actuel = await get_prix_actuel(session, symbole)
    if prix_actuel is None:
        return "NEUTRE", {}

    if symbole not in prix_reference:
        prix_reference[symbole] = prix_actuel
        log.info(f"  {symbole} : prix référence enregistré @ {prix_actuel}")
        return "NEUTRE", {}

    prix_ref = prix_reference[symbole]
    if prix_ref <= 0:
        prix_reference[symbole] = prix_actuel
        return "NEUTRE", {}

    variation_pct = (prix_actuel - prix_ref) / prix_ref * 100

    df_15m    = await get_klines(session, symbole, interval=15, limite=50)
    vol_ratio = 0.0
    atr_val   = 0.0
    if df_15m is not None and len(df_15m) >= 15:
        vol_ratio = calc_volume_ratio(df_15m)
        atr_val   = calc_atr(df_15m)

    if vol_ratio < VOLUME_MINI:
        log.info(f"  {symbole} : Vol {vol_ratio:.2f}x | Variation={variation_pct:+.2f}% → skip")
        return "NEUTRE", {}

    details = {
        "atr":           atr_val,
        "vol_ratio":     vol_ratio,
        "variation_pct": abs(variation_pct),
        "prix_ref":      prix_ref,
        "prix_actuel":   prix_actuel,
    }

    if variation_pct <= -SEUIL_MOUVEMENT_PCT:
        log.info(f"  {symbole} ✅ ACHAT | Chute={variation_pct:.2f}% | Vol={vol_ratio:.2f}x")
        prix_reference[symbole] = prix_actuel
        return "ACHAT", details

    if variation_pct >= SEUIL_MOUVEMENT_PCT:
        log.info(f"  {symbole} ✅ VENTE | Montée={variation_pct:.2f}% | Vol={vol_ratio:.2f}x")
        prix_reference[symbole] = prix_actuel
        return "VENTE", details

    log.info(f"  {symbole} : Variation={variation_pct:+.2f}% (seuil ±{SEUIL_MOUVEMENT_PCT}%)")
    return "NEUTRE", {}

# ═══════════════════════════════════════════════════════════════
#  GESTION MISE DYNAMIQUE
# ═══════════════════════════════════════════════════════════════
def calculer_mise(capital, etat, multiplicateur_session=1.0):
    nb_trades     = etat.get("nb_trades", 0)
    nb_wins       = etat.get("nb_wins", 0)
    wins_consec   = etat.get("wins_consecutifs", 0)
    pertes_consec = etat.get("pertes_consecutives", 0)
    avg_win_pct   = etat.get("avg_win_pct", 0)
    avg_loss_pct  = etat.get("avg_loss_pct", 0)

    mise = capital * MISE_BASE_PCT

    if nb_trades >= MIN_TRADES_KELLY and avg_loss_pct > 0 and avg_win_pct > 0:
        win_rate   = nb_wins / nb_trades
        b          = avg_win_pct / avg_loss_pct
        kelly_full = (win_rate * b - (1 - win_rate)) / b
        kelly_frac = max(0, min(kelly_full * KELLY_FRACTION, KELLY_CAP))
        mise       = capital * kelly_frac

    if pertes_consec >= 2:
        mise *= REDUCTION_PERTES
        log.info(f"  ⚠️ Mise réduite 50% ({pertes_consec} pertes)")
    elif wins_consec >= WINS_CONFIANCE:
        mise *= BOOST_CONFIANCE
        log.info(f"  💪 Mise boostée +20% ({wins_consec} wins)")

    mise *= multiplicateur_session
    mise  = max(mise, MISE_MIN)
    mise  = min(mise, capital * MISE_MAX_PCT)
    return round(mise, 2)

# ═══════════════════════════════════════════════════════════════
#  EXÉCUTION D'UN TRADE
# ═══════════════════════════════════════════════════════════════
async def executer_trade(session, symbole, direction, capital, details, etat, etat_global):
    prix_entree = await get_prix_actuel(session, symbole)
    if prix_entree is None:
        async with trades_lock:
            trades_ouverts.pop(symbole, None)
        return "ERREUR", 0, 0, {}

    mise = calculer_mise(capital, etat)

    # Stop loss proportionnel au capital — 2% du capital
    # Cohérent avec les paliers qui sont aussi en % du capital
    stop_loss_eur = round(capital * STOP_LOSS_PCT / 100, 2)

    # Stop loss initial — protection max proportionnelle
    if direction == "ACHAT":
        stop_initial   = round(prix_entree * (1 - stop_loss_eur / (mise * LEVIER)), 8)
        objectif_final = round(prix_entree * (1 + stop_loss_eur / (mise * LEVIER) * 2), 8)
    else:
        stop_initial   = round(prix_entree * (1 + stop_loss_eur / (mise * LEVIER)), 8)
        objectif_final = round(prix_entree * (1 - stop_loss_eur / (mise * LEVIER) * 2), 8)

    # Numéro de trade sous lock
    async with trades_lock:
        etat_global["nb_trades"] = etat_global.get("nb_trades", 0) + 1
        numero_trade = etat_global["nb_trades"]

    log.info(f"\n  {'='*55}")
    log.info(f"  TRADE #{numero_trade} [OLIDE973 V4] — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  {symbole} ({direction})")
    log.info(f"  Variation : {details.get('variation_pct', 0):.2f}% | "
             f"Ref={details.get('prix_ref')} → {details.get('prix_actuel')}")
    log.info(f"  Vol={details.get('vol_ratio', 0):.2f}x | Stop max : -{stop_loss_eur}€ ({STOP_LOSS_PCT}% capital)")
    log.info(f"  Prix entrée : {prix_entree} | Stop : {stop_initial}")
    log.info(f"  Mise : {mise}€ × x{LEVIER} = {round(mise*LEVIER,2)}€")
    log.info(f"  Trades ouverts : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}\n")

    await telegram(session,
        f"📊 <b>TRADE #{numero_trade} — OLIDE973 V4</b>\n"
        f"{'🟢 ACHAT' if direction == 'ACHAT' else '🔴 VENTE'} {symbole}\n"
        f"Variation : {details.get('variation_pct', 0):.2f}% depuis ref\n"
        f"Volume : {details.get('vol_ratio', 0):.2f}x\n"
        f"Prix : {prix_entree} | Stop : {stop_initial}\n"
        f"Mise : {mise}€ × x{LEVIER}\n"
        f"Trades : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}\n"
        f"🎯 Lock paliers : {LOCK_PALIERS_PCT[:4]}%..."
    )

    debut             = time.time()
    dernier_log       = 0
    prix_sortie       = prix_entree
    pnl_max_atteint   = 0.0
    lock_actuel       = 0.0    # gain garanti actuellement
    resultat_final    = None
    gain_final        = 0.0

    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        prix_actuel = await get_prix_actuel(session, symbole)
        if prix_actuel is None:
            continue

        prix_sortie = prix_actuel

        # ── Calcul PnL
        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)

        if pnl > pnl_max_atteint:
            pnl_max_atteint = pnl

        # ── Lock paliers — gain garanti progressif basé sur le capital
        nouveau_lock = get_palier_lock(pnl_max_atteint, capital)
        if nouveau_lock > lock_actuel:
            lock_actuel = nouveau_lock
            log.info(f"  🔒 LOCK {lock_actuel}€ GARANTI [{symbole}] "
                     f"(PnL max={pnl_max_atteint:.2f}€)")
            await telegram(session,
                f"🔒 <b>{lock_actuel}€ garanti !</b>\n"
                f"{symbole} | PnL max : +{pnl_max_atteint:.2f}€\n"
                f"Gain verrouillé ✅"
            )

        # ── Sortie lock : PnL redescend sous le dernier palier atteint
        if lock_actuel > 0 and pnl < lock_actuel and pnl_max_atteint >= lock_actuel:
            duree = int((time.time() - debut) / 60)
            log.info(f"\n  🔒 SORTIE LOCK [{symbole}] +{lock_actuel}€ "
                     f"(max={pnl_max_atteint:.2f}€) | {duree}min")
            await telegram(session,
                f"🔒 <b>SORTIE LOCK</b>\n"
                f"{symbole} {direction}\n"
                f"Gain : <b>+{lock_actuel}€</b>\n"
                f"PnL max : +{pnl_max_atteint:.2f}€\n"
                f"Durée : {duree} min"
            )
            resultat_final = "GAGNE"
            gain_final     = lock_actuel
            break

        # ── Stop loss initial — protection max -2% du capital
        atteint_stop = (prix_actuel <= stop_initial if direction == "ACHAT"
                        else prix_actuel >= stop_initial)

        duree = int((time.time() - debut) / 60)

        if time.time() - dernier_log >= 60:
            lock_flag = f" 🔒{lock_actuel}€" if lock_actuel > 0 else ""
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole} {prix_actuel} | "
                     f"PnL {'+' if pnl>=0 else ''}{pnl:.2f}€{lock_flag} | {duree}min")
            dernier_log = time.time()

        trade_info = {
            "prix_entree":   prix_entree,
            "prix_sortie":   prix_sortie,
            "stop_loss":     stop_initial,
            "objectif":      objectif_final,
            "duree_minutes": duree
        }

        if atteint_stop:
            resultat_final = "GAGNE" if pnl > 0 else "PERDU"
            log.info(f"\n  🛑 STOP [{symbole}] "
                     f"{'+' if pnl>=0 else ''}{pnl:.2f}€ | {duree}min")
            await telegram(session,
                f"🛑 <b>STOP</b>\n"
                f"{symbole} {direction}\n"
                f"Résultat : {'+' if pnl>=0 else ''}{pnl:.2f}€\n"
                f"Durée : {duree} min"
            )
            gain_final = pnl
            break

        if time.time() - debut >= TIMEOUT_TRADE:
            resultat_final = "GAGNE" if pnl > 0 else "PERDU"
            log.info(f"\n  ⏱ TIMEOUT [{symbole}] {'+' if pnl>=0 else ''}{pnl:.2f}€")
            await telegram(session,
                f"⏱ <b>TIMEOUT</b>\n{symbole} {'+' if pnl>=0 else ''}{pnl:.2f}€\nDurée : {duree} min"
            )
            gain_final = pnl
            break

    # Libérer le marché
    async with trades_lock:
        trades_ouverts.pop(symbole, None)

    # Mettre à jour l'état global sous lock
    async with trades_lock:
        etat_global["capital"]   = round(etat_global["capital"] + gain_final, 2)
        etat_global["cumul_net"] = round(etat_global["capital"] - CAPITAL_INITIAL, 2)
        etat_global["pnl_jour"]  = round(etat_global.get("pnl_jour", 0) + gain_final, 2)

        if resultat_final == "GAGNE":
            etat_global["nb_wins"]             = etat_global.get("nb_wins", 0) + 1
            etat_global["total_gagne"]         = round(etat_global.get("total_gagne", 0) + gain_final, 2)
            etat_global["pertes_consecutives"] = 0
            etat_global["wins_consecutifs"]    = etat_global.get("wins_consecutifs", 0) + 1
            n        = etat_global["nb_wins"]
            gain_pct = (gain_final / max(mise * LEVIER, 1)) * 100
            etat_global["avg_win_pct"] = round(
                (etat_global.get("avg_win_pct", 0) * (n - 1) + gain_pct) / n, 4
            )
        else:
            etat_global["nb_losses"]           = etat_global.get("nb_losses", 0) + 1
            etat_global["total_perdu"]         = round(etat_global.get("total_perdu", 0) + abs(gain_final), 2)
            etat_global["pertes_consecutives"] = etat_global.get("pertes_consecutives", 0) + 1
            etat_global["wins_consecutifs"]    = 0
            n         = etat_global["nb_losses"]
            perte_pct = (abs(gain_final) / max(mise * LEVIER, 1)) * 100
            etat_global["avg_loss_pct"] = round(
                (etat_global.get("avg_loss_pct", 0) * (n - 1) + perte_pct) / n, 4
            )

        etat_global.setdefault("historique", []).append({
            'heure':     datetime.now().strftime('%Y-%m-%d %H:%M'),
            'marche':    symbole,
            'direction': direction,
            'resultat':  resultat_final,
            'gain':      round(gain_final, 2),
            'mise':      round(mise, 2),
            'capital':   etat_global["capital"]
        })

    enregistrer_trade({
        'marche':        symbole,
        'direction':     direction,
        'resultat':      resultat_final,
        'prix_entree':   trade_info['prix_entree'],
        'prix_sortie':   trade_info['prix_sortie'],
        'stop_loss':     trade_info['stop_loss'],
        'objectif':      trade_info['objectif'],
        'mise':          mise,
        'gain':          round(gain_final, 2),
        'capital_apres': etat_global['capital'],
        'duree_minutes': trade_info['duree_minutes'],
        'score':         None,
        'adx':           None,
        'atr':           None,
        'rsi':           None,
    })
    sauvegarder_etat(etat_global)
    afficher_tableau_de_bord(etat_global)

    # ── Rapport Telegram après chaque trade
    nb_trades = etat_global.get("nb_trades", 0)
    nb_wins   = etat_global.get("nb_wins", 0)
    win_rate  = (nb_wins / nb_trades * 100) if nb_trades > 0 else 0
    perf      = (etat_global["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100
    await telegram(session,
        f"📈 <b>RAPPORT OLIDE973</b>\n"
        f"Capital : <b>{round(etat_global['capital'],2)}€</b> "
        f"({'+' if perf>=0 else ''}{round(perf,2)}%)\n"
        f"PnL jour : {'+' if etat_global.get('pnl_jour',0)>=0 else ''}"
        f"{round(etat_global.get('pnl_jour',0),2)}€\n"
        f"Trades : {nb_trades} | WR : {round(win_rate,1)}%\n"
        f"Gagné : +{round(etat_global.get('total_gagne',0),2)}€ | "
        f"Perdu : -{round(etat_global.get('total_perdu',0),2)}€\n"
        f"<b>NET : {'+' if etat_global.get('cumul_net',0)>=0 else ''}"
        f"{round(etat_global.get('cumul_net',0),2)}€</b>"
    )

    return resultat_final, gain_final, mise, trade_info

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
    cooldown_until = etat.get("cooldown_until", 0)
    if time.time() < cooldown_until:
        restant = int((cooldown_until - time.time()) / 60)
        log.info(f"  ❄️ Cooldown — {restant} min restantes")
        return "COOLDOWN"
    if cooldown_until > 0 and time.time() >= cooldown_until:
        etat["pertes_consecutives"] = 0
        etat["cooldown_until"]      = 0
        sauvegarder_etat(etat)
    if etat.get("pertes_consecutives", 0) >= MAX_PERTES_CONSECUTIVES:
        log.warning(f"  {MAX_PERTES_CONSECUTIVES} pertes → cooldown {COOLDOWN_PERTES//60} min")
        etat["cooldown_until"]      = int(time.time()) + COOLDOWN_PERTES
        etat["pertes_consecutives"] = 0
        sauvegarder_etat(etat)
        return "COOLDOWN"
    return "OK"

def reset_pnl_jour_si_nouveau_jour(etat):
    aujourd_hui = datetime.now().strftime('%Y-%m-%d')
    if etat.get("date_jour", "") != aujourd_hui:
        etat["pnl_jour"]  = 0.0
        etat["date_jour"] = aujourd_hui
        log.info("  📅 Nouveau jour — PnL remis à 0")

async def envoyer_rapport_hebdomadaire(session, etat):
    """
    Envoie chaque lundi matin :
    1. Le graphique de progression du capital (image PNG)
    2. Le classement des marchés par gain (texte)
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import io
    from datetime import timedelta

    historique = etat.get("historique", [])
    if not historique:
        return

    maintenant     = datetime.now()
    il_y_a_7_jours = (maintenant - timedelta(days=7)).strftime('%Y-%m-%d')
    date_debut     = (maintenant - timedelta(days=7)).strftime('%d/%m')
    date_fin       = maintenant.strftime('%d/%m/%Y')

    # ── Gains par marché sur 7 jours
    gains_par_marche = {}
    for h in historique:
        if h.get("heure", "") >= il_y_a_7_jours:
            marche = h.get("marche", "?")
            gain   = h.get("gain", 0)
            gains_par_marche[marche] = round(
                gains_par_marche.get(marche, 0) + gain, 2
            )

    # ── Capital par jour sur 7 jours
    capital_par_jour = {}
    for h in historique:
        if h.get("heure", "") >= il_y_a_7_jours:
            jour = h.get("heure", "")[:10]
            capital_par_jour[jour] = h.get("capital", etat["capital"])

    jours_tries  = sorted(capital_par_jour.keys())
    capitaux     = [capital_par_jour[j] for j in jours_tries]
    labels_jours = [j[5:] for j in jours_tries]  # MM-DD

    # ── Générer le graphique
    try:
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7),
                                        gridspec_kw={'height_ratios': [3, 1]})
        fig.patch.set_facecolor('#1a1a2e')

        # Courbe capital
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

        ax1.axhline(y=CAPITAL_INITIAL, color='#ffffff',
                    linewidth=1, linestyle='--', alpha=0.4)

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
            f'OLIDE973 V4 — Progression du capital\n'
            f'NET : {"+"+str(round(net,2))+"€" if net>=0 else str(round(net,2))+"€"}'
            f' ({"+"+str(round(perf,2))+"%" if perf>=0 else str(round(perf,2))+"%"})'
            f' | Capital : {etat["capital"]}€',
            color='white', fontsize=11, fontweight='bold', pad=12)

        # Barres PnL journalier
        ax2.set_facecolor('#16213e')
        pnl_valeurs = []
        for i, jour in enumerate(jours_tries):
            if i == 0:
                pnl_valeurs.append(capitaux[0] - CAPITAL_INITIAL)
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
                         ha='center', fontsize=8,
                         color=couleur, fontweight='bold')

        plt.tight_layout(pad=2.0)

        # Sauvegarder en mémoire et envoyer via Telegram
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150,
                    bbox_inches='tight', facecolor='#1a1a2e')
        buf.seek(0)
        plt.close()

        # Envoyer l'image via Telegram sendPhoto
        if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
            url_photo = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
            form_data = aiohttp.FormData()
            form_data.add_field('chat_id', TELEGRAM_CHAT_ID)
            form_data.add_field('caption', f'Progression semaine du {date_debut} au {date_fin}')
            form_data.add_field('photo', buf, filename='progression.png',
                                content_type='image/png')
            await session.post(url_photo, data=form_data,
                               timeout=aiohttp.ClientTimeout(total=30))
            log.info(f"  Graphique hebdomadaire envoyé sur Telegram")

    except Exception as e:
        log.error(f"Erreur graphique hebdomadaire : {e}")

    # ── Rapport texte — semaine + total depuis début
    if not gains_par_marche:
        return

    # Calcul total depuis le début pour chaque marché
    gains_total     = {}
    wins_total      = {}
    pertes_total    = {}
    wins_semaine    = {}
    pertes_semaine  = {}

    for h in etat.get("historique", []):
        marche    = h.get("marche", "?")
        gain      = h.get("gain", 0)
        resultat  = h.get("resultat", "")
        semaine   = h.get("heure", "") >= il_y_a_7_jours

        # Total depuis début
        gains_total[marche]  = round(gains_total.get(marche, 0) + gain, 2)
        if resultat == "GAGNE":
            wins_total[marche]   = wins_total.get(marche, 0) + 1
        else:
            pertes_total[marche] = pertes_total.get(marche, 0) + 1

        # Semaine uniquement
        if semaine:
            if resultat == "GAGNE":
                wins_semaine[marche]   = wins_semaine.get(marche, 0) + 1
            else:
                pertes_semaine[marche] = pertes_semaine.get(marche, 0) + 1

    # Trier par gain semaine décroissant
    classement    = sorted(gains_par_marche.items(), key=lambda x: x[1], reverse=True)
    total_semaine = round(sum(gains_par_marche.values()), 2)
    total_global  = round(sum(gains_total.values()), 2)

    lignes = []
    for marche, gain_sem in classement:
        emoji   = "✅" if gain_sem >= 0 else "❌"
        s_gain  = f"{'+' if gain_sem>=0 else ''}{gain_sem}€"
        s_wl    = f"{wins_semaine.get(marche,0)}G/{pertes_semaine.get(marche,0)}P"
        t_gain  = gains_total.get(marche, 0)
        t_s     = f"{'+' if t_gain>=0 else ''}{t_gain}€"
        t_wl    = f"{wins_total.get(marche,0)}G/{pertes_total.get(marche,0)}P"
        lignes.append(
            f"{emoji} <code>{marche:<10} {s_gain:<10} {s_wl:<8} | {t_s:<10} {t_wl}</code>"
        )

    message = (
        f"<b>RAPPORT HEBDOMADAIRE OLIDE973</b>\n"
        f"Semaine du {date_debut} au {date_fin}\n"
        f"<code>{'─'*44}</code>\n"
        f"<code>{'MARCHÉ':<10} {'SEMAINE':>8} {'G/P':>6}  | {'TOTAL':>8} {'G/P'}</code>\n"
        f"<code>{'─'*44}</code>\n"
        f"{chr(10).join(lignes)}\n"
        f"<code>{'─'*44}</code>\n"
        f"<b>Semaine : {'+' if total_semaine>=0 else ''}{total_semaine}€ | "
        f"Total : {'+' if total_global>=0 else ''}{total_global}€</b>"
    )

    log.info(f"  Envoi rapport hebdomadaire texte Telegram")
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
    log.info(f"  BOT HUMAIN — OLIDE973 V4")
    log.info(f"  {'='*55}")
    log.info(f"  Capital    : {round(etat['capital'],2)}€ ({'+' if perf>=0 else ''}{round(perf,2)}%)")
    log.info(f"  PnL jour   : {'+' if etat.get('pnl_jour',0)>=0 else ''}{round(etat.get('pnl_jour',0),2)}€")
    log.info(f"  Trades     : {nb_trades} | Wins : {nb_wins} ({win_rate:.1f}%)")
    log.info(f"  Ouverts    : {len(trades_ouverts)}/{MAX_TRADES_SIMULTANES}")
    log.info(f"  Pertes c.  : {etat.get('pertes_consecutives',0)}/{MAX_PERTES_CONSECUTIVES}")
    log.info(f"  Wins c.    : {etat.get('wins_consecutifs',0)}")
    log.info(f"  Gagné      : +{round(etat.get('total_gagne',0),2)}€")
    log.info(f"  Perdu      : -{round(etat.get('total_perdu',0),2)}€")
    log.info(f"  NET        : {'+' if etat.get('cumul_net',0)>=0 else ''}{round(etat.get('cumul_net',0),2)}€")
    if etat.get("historique"):
        log.info("  Derniers trades :")
        for h in etat["historique"][-5:]:
            icone = "✅" if h["resultat"] == "GAGNE" else "❌"
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

    for champ, valeur in [
        ("pnl_jour", 0.0), ("date_jour", ""), ("wins_consecutifs", 0),
        ("cooldown_until", 0), ("nb_skips", 0)
    ]:
        if champ not in etat:
            etat[champ] = valeur

    afficher_tableau_de_bord(etat)

    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector) as session:
        await telegram(session,
            f"🚀 <b>BOT HUMAIN OLIDE973 V4 DÉMARRÉ</b>\n"
            f"Capital : {round(etat['capital'],2)}€\n"
            f"Signal : mouvement ≥ {SEUIL_MOUVEMENT_PCT}% depuis prix référence\n"
            f"Lock paliers | Stop max -{STOP_LOSS_PCT}% du capital\n"
            f"Kill switch : {KILL_SWITCH_JOUR}€/jour\n"
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

        while True:
            try:
                reset_pnl_jour_si_nouveau_jour(etat)

                # ── Rapport hebdomadaire chaque lundi à 8h UTC
                maintenant = datetime.now(timezone.utc).replace(tzinfo=None)
                if (maintenant.weekday() == 0 and  # lundi
                    maintenant.hour == 8 and
                    maintenant.minute < 1 and
                    etat.get("derniere_semaine", "") != maintenant.strftime('%Y-%W')):
                    await envoyer_rapport_hebdomadaire(session, etat)
                    etat["derniere_semaine"] = maintenant.strftime('%Y-%W')
                    sauvegarder_etat(etat)

                statut = verifier_protections(etat, etat["capital"])
                if statut == "RUINE":
                    await telegram(session,
                        f"🚨 <b>SEUIL RUINE !</b>\nCapital : {etat['capital']}€\nBot arrêté !")
                    break
                if statut in ("KILL_SWITCH", "COOLDOWN"):
                    await asyncio.sleep(60)
                    etat = charger_etat()
                    continue

                async with trades_lock:
                    slots_libres        = MAX_TRADES_SIMULTANES - len(trades_ouverts)
                    marches_disponibles = [m for m in MARCHES if m not in trades_ouverts]

                if slots_libres <= 0:
                    log.info(f"  {MAX_TRADES_SIMULTANES}/{MAX_TRADES_SIMULTANES} trades — attente...")
                    await asyncio.sleep(PAUSE_SCAN)
                    continue

                log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan "
                         f"| Slots : {slots_libres}/{MAX_TRADES_SIMULTANES}")

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
                            sig["details"], etat, etat
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
