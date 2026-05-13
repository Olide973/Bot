"""
╔══════════════════════════════════════════════════════════════╗
║           BOT MEAN REVERSION V7.5 — ANTI-CHUTE              ║
║   RSI < 30 → ACHAT (sauf chute libre)                       ║
║   RSI > 70 → VENTE (sauf pump vertical)                     ║
║   Break-Even verrouillé + Filtres tendance                  ║
╚══════════════════════════════════════════════════════════════╝
"""

import requests
import time
import os
import logging
import pandas as pd

from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator

from datetime import datetime

from database import (
    init_database,
    charger_etat,
    sauvegarder_etat,
    enregistrer_trade
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)

log = logging.getLogger(__name__)

# =========================================================
# CONFIG
# =========================================================

CAPITAL_INITIAL         = 215.0
LEVIER                  = 10

MISE_FIXE_PCT           = 0.20

KELLY_FRACTION          = 0.25
KELLY_CAP               = 0.20
MIN_TRADES_KELLY        = 30

ATR_MULTIPLIER          = 2.5
RATIO_RR                = 2.0
RATIO_PARTIEL           = 1.0

PAUSE                   = 120
CHECK_INTERVAL          = 10
TIMEOUT_TRADE           = 12 * 3600

RSI_ACHAT               = 30
RSI_VENTE               = 70

VOLUME_MINI             = 0.40
ADX_MAX                 = 40

MAX_PERTES_CONSECUTIVES = 2

SEUIL_RUINE             = 0.30
PAUSE_DUREE             = 43200

BREAK_EVEN_TRIGGER_PNL  = 0.75
BREAK_EVEN_BUFFER_PCT   = 0.001

CHUTE_VARIATION_24H     = -2.0
PUMP_VARIATION_24H      = 2.0

MA_PERIODE              = 20

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

TRAILING_NIVEAUX = [
    (100, 0.05),
    (75, 0.07),
    (50, 0.10),
    (35, 0.15),
    (25, 0.20),
    (18, 0.30),
    (12, 0.50),
    (7.5, 0.80),
    (3, 1.50),
    (0.75, 2.00),
    (0, 2.50),
]

MARCHES = [
    "XRPUSDT",
    "ATOMUSDT",
    "LINKUSDT",
    "ADAUSDT",
    "SOLUSDT",
    "AVAXUSDT",
    "NEARUSDT",
    "DOTUSDT"
]

KRAKEN_SYMBOLS = {
    "XRPUSDT": "XXRPZUSD",
    "ATOMUSDT": "ATOMUSD",
    "LINKUSDT": "LINKUSD",
    "ADAUSDT": "ADAUSD",
    "SOLUSDT": "SOLUSD",
    "AVAXUSDT": "AVAXUSD",
    "NEARUSDT": "NEARUSD",
    "DOTUSDT": "DOTUSD"
}

log.info("=" * 55)
log.info("  BOT MEAN REVERSION V7.5 — ANTI-CHUTE / ANTI-PUMP")
log.info(f"  Capital : {CAPITAL_INITIAL}EUR | Levier x{LEVIER}")
log.info(f"  🛡️ Filtre chute : skip si var 24h < {CHUTE_VARIATION_24H}%")
log.info(f"  🛡️ Filtre pump  : skip si var 24h > {PUMP_VARIATION_24H}%")
log.info(f"  🔒 Break-even auto dès +{BREAK_EVEN_TRIGGER_PNL}€")
log.info("=" * 55)


# =========================================================
# UTILS
# =========================================================

def get_multiplicateur_atr(pnl):
    for seuil, mult in TRAILING_NIVEAUX:
        if pnl >= seuil:
            return mult
    return 2.50


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=10
        )

    except Exception as e:
        log.error(f"Erreur Telegram : {e}")


# =========================================================
# DATA
# =========================================================

def get_prix_actuel(symbole):

    ks = KRAKEN_SYMBOLS.get(symbole, symbole)

    try:
        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": ks},
            timeout=10
        )

        data = r.json()

        if data.get("error"):
            return None

        result = data.get("result", {})

        if not result:
            return None

        return float(result[list(result.keys())[0]]["c"][0])

    except:
        return None


def get_klines(symbole, limite=100):

    ks = KRAKEN_SYMBOLS.get(symbole, symbole)

    try:
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={
                "pair": ks,
                "interval": 60
            },
            timeout=15
        )

        data = r.json()

        if data.get("error"):
            return None

        result = data.get("result", {})

        keys = [k for k in result.keys() if k != "last"]

        if not keys:
            return None

        df = pd.DataFrame(
            result[keys[0]],
            columns=[
                'time',
                'open',
                'high',
                'low',
                'close',
                'vwap',
                'volume',
                'count'
            ]
        )

        df = df.astype({
            'open': float,
            'high': float,
            'low': float,
            'close': float,
            'volume': float
        })

        return df.tail(limite).reset_index(drop=True)

    except:
        return None


# =========================================================
# INDICATEURS
# =========================================================

def calculer_adx(df, periode=14):

    try:
        v = ADXIndicator(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=periode
        ).adx().iloc[-1]

        return round(float(v), 2) if not pd.isna(v) else 0

    except:
        return 0


def calculer_atr(df, periode=14):

    try:
        v = AverageTrueRange(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=periode
        ).average_true_range().iloc[-1]

        return round(float(v), 8) if not pd.isna(v) else 0

    except:
        return 0


def calculer_rsi(df, periode=14):

    try:
        v = RSIIndicator(
            close=df['close'],
            window=periode
        ).rsi().iloc[-1]

        return round(float(v), 2) if not pd.isna(v) else 50

    except:
        return 50


def verifier_volume(df):

    vols = df['volume'].tolist()

    if len(vols) < 10:
        return True, 0

    moy = sum(vols[-24:]) / len(vols[-24:])

    ratio = vols[-1] / moy if moy > 0 else 0

    return ratio >= VOLUME_MINI, round(ratio * 100, 1)


# =========================================================
# FILTRES
# =========================================================

def detecter_chute_libre(df, periode=MA_PERIODE):

    if len(df) < periode:
        return False, 0

    closes = df['close']

    ma_actuelle = closes.tail(periode).mean()

    ma_ancienne = closes.iloc[-periode:-(periode // 2)].mean()

    prix = closes.iloc[-1]

    prix_24h = closes.iloc[-24] if len(closes) >= 24 else closes.iloc[0]

    variation = ((prix - prix_24h) / prix_24h) * 100

    chute = (
        ma_actuelle < ma_ancienne
        and prix < ma_actuelle
        and variation < CHUTE_VARIATION_24H
    )

    return chute, round(variation, 2)


def detecter_pump_vertical(df, periode=MA_PERIODE):

    if len(df) < periode:
        return False, 0

    closes = df['close']

    ma_actuelle = closes.tail(periode).mean()

    ma_ancienne = closes.iloc[-periode:-(periode // 2)].mean()

    prix = closes.iloc[-1]

    prix_24h = closes.iloc[-24] if len(closes) >= 24 else closes.iloc[0]

    variation = ((prix - prix_24h) / prix_24h) * 100

    pump = (
        ma_actuelle > ma_ancienne
        and prix > ma_actuelle
        and variation > PUMP_VARIATION_24H
    )

    return pump, round(variation, 2)


# =========================================================
# ANALYSE
# =========================================================

def analyser_marche(symbole):

    df = get_klines(symbole, 100)

    if df is None or len(df) < 30:
        return "NEUTRE", {}

    adx = calculer_adx(df)
    atr = calculer_atr(df)
    rsi = calculer_rsi(df)

    vol_ok, vol_ratio = verifier_volume(df)

    if not vol_ok:
        log.info(f"  {symbole} : Volume {vol_ratio}% < {VOLUME_MINI*100}% → skip")
        return "NEUTRE", {}

    prix = df['close'].iloc[-1]

    atr_pct = (atr / prix) * 100

    if adx > ADX_MAX:
        log.info(f"  {symbole} : ADX {adx} > {ADX_MAX} → skip")
        return "NEUTRE", {}

    details = {
        "adx": adx,
        "atr": atr,
        "rsi": rsi,
        "atr_pct": atr_pct,
        "volume_ratio": vol_ratio,
        "df": df
    }

    if rsi < RSI_ACHAT:

        chute, var = detecter_chute_libre(df)

        if chute:
            log.info(f"  {symbole} : RSI {rsi} mais CHUTE LIBRE ({var}% 24h) → SKIP")
            return "NEUTRE", details

        log.info(f"  {symbole} : RSI {rsi} → ACHAT")

        return "ACHAT", details

    elif rsi > RSI_VENTE:

        pump, var = detecter_pump_vertical(df)

        if pump:
            log.info(f"  {symbole} : RSI {rsi} mais PUMP VERTICAL (+{var}% 24h) → SKIP")
            return "NEUTRE", details

        log.info(f"  {symbole} : RSI {rsi} → VENTE")

        return "VENTE", details

    else:
        log.info(f"  {symbole} : RSI {rsi} | ADX {adx} → pas de signal")

        return "NEUTRE", details


def choisir_meilleur_marche():

    log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan {len(MARCHES)} marchés...")

    signaux = {}

    for m in MARCHES:

        d, det = analyser_marche(m)

        if d != "NEUTRE":
            signaux[m] = {
                "direction": d,
                "details": det
            }

        time.sleep(0.5)

    if not signaux:
        log.info("  => Aucun signal valide.")
        return None, "NEUTRE", {}

    meilleur = max(
        signaux.items(),
        key=lambda x: (
            abs(x[1]["details"].get("rsi", 50) - 50),
            x[1]["details"].get("atr_pct", 0)
        )
    )[0]

    d = signaux[meilleur]["direction"]

    log.info(
        f"  => SIGNAL : {meilleur} ({d}) | RSI {signaux[meilleur]['details']['rsi']}"
    )

    return meilleur, d, signaux[meilleur]["details"]


# =========================================================
# MONEY MANAGEMENT
# =========================================================

def calculer_mise(capital, nb, wr, aw, al):

    if nb < MIN_TRADES_KELLY:
        m = capital * MISE_FIXE_PCT

    else:

        if al <= 0:
            m = capital * MISE_FIXE_PCT

        else:
            b = aw / al

            kf = ((wr * b - (1 - wr)) / b) * KELLY_FRACTION

            kf = max(0, min(kf, KELLY_CAP))

            m = capital * kf

    return round(max(5.0, min(m, capital * 0.30)), 2)


# =========================================================
# TRADE
# =========================================================

def simuler_trade(symbole, direction, num, capital, details, etat):

    pe = get_prix_actuel(symbole)

    if pe is None:
        return "ERREUR", 0, 0, {}

    atr = details.get("atr", 0)

    if direction == "ACHAT":

        sl = round(pe - atr * ATR_MULTIPLIER, 8)

        op = round(pe + atr * ATR_MULTIPLIER * RATIO_PARTIEL, 8)

        of = round(pe + atr * ATR_MULTIPLIER * RATIO_RR, 8)

    else:

        sl = round(pe + atr * ATR_MULTIPLIER, 8)

        op = round(pe - atr * ATR_MULTIPLIER * RATIO_PARTIEL, 8)

        of = round(pe - atr * ATR_MULTIPLIER * RATIO_RR, 8)

    ds = abs(pe - sl)

    dsp = (ds / pe) * 100

    wr = etat["nb_wins"] / etat["nb_trades"] if etat["nb_trades"] > 0 else 0.50

    aw = etat["avg_win_pct"] if etat["avg_win_pct"] > 0 else dsp * RATIO_RR

    al = etat["avg_loss_pct"] if etat["avg_loss_pct"] > 0 else dsp

    mise = calculer_mise(capital, etat["nb_trades"], wr, aw, al)

    log.info(
        f"\n  TRADE #{num} — {symbole} {direction} @ {pe} | Stop {sl} | Mise {mise}€"
    )

    debut = time.time()

    sa = sl
    mp = pe
    dl = 0
    ps = pe

    pe_done = False

    gp = 0

    na = 2.50

    be_lock = False

    pnl_max = 0

    while True:

        time.sleep(CHECK_INTERVAL)

        pa = get_prix_actuel(symbole)

        if pa is None:
            time.sleep(5)
            continue

        ps = pa

        if direction == "ACHAT":
            pnl = round((pa - pe) / pe * mise * LEVIER, 2)
        else:
            pnl = round((pe - pa) / pe * mise * LEVIER, 2)

        mult = get_multiplicateur_atr(pnl)

        dt = atr * mult

        if pnl > pnl_max:
            pnl_max = pnl

        if not be_lock and pnl_max >= BREAK_EVEN_TRIGGER_PNL:

            if direction == "ACHAT":

                sbe = round(pe * (1 + BREAK_EVEN_BUFFER_PCT), 8)

                if sbe > sa:
                    sa = sbe

            else:

                sbe = round(pe * (1 - BREAK_EVEN_BUFFER_PCT), 8)

                if sbe < sa:
                    sa = sbe

            be_lock = True

            log.info(f"  🔒 BREAK-EVEN → Stop {sa}")

        modif = False

        if direction == "ACHAT":

            if pa > mp:
                mp = pa

            ns = round(mp - dt, 8)

            if ns > sa:
                sa = ns
                modif = True

        else:

            if pa < mp:
                mp = pa

            ns = round(mp + dt, 8)

            if ns < sa:
                sa = ns
                modif = True

        if direction == "ACHAT":

            ap = not pe_done and pa >= op

            af = pa >= of

            ast = pa <= sa

        else:

            ap = not pe_done and pa <= op

            af = pa <= of

            ast = pa >= sa

        d = int((time.time() - debut) / 60)

        if time.time() - dl >= 60:

            log.info(
                f"  [{datetime.now().strftime('%H:%M:%S')}] "
                f"{symbole}: {pa} | PnL: {pnl}€ | Stop: {sa}"
            )

            dl = time.time()

        ti = {
            "prix_entree": pe,
            "prix_sortie": ps,
            "stop_loss": sl,
            "objectif": of,
            "duree_minutes": d
        }

        if ap:

            gp = round(pnl * 0.5, 2)

            pe_done = True

            log.info(f"  ⚡ SORTIE PARTIELLE : +{gp}€")

            continue

        if af:

            gf = round(pnl * 0.5, 2) if pe_done else pnl

            gt = round(gp + gf, 2)

            log.info(f"  🎯 OBJECTIF : +{gt}€")

            return "GAGNE", gt, mise, ti

        if ast:

            if pe_done:

                gr = round(pnl * 0.5, 2)

                gt = round(gp + gr, 2)

                res = "GAGNE" if gt > 0 else "PERDU"

                return res, gt, mise, ti

            else:

                res = "GAGNE" if pnl > 0 else "PERDU"

                return res, pnl, mise, ti

        if time.time() - debut >= TIMEOUT_TRADE:

            gt = round(gp + pnl * 0.5, 2) if pe_done else pnl

            res = "GAGNE" if gt > 0 else "PERDU"

            return res, gt, mise, ti


# =========================================================
# SECURITE
# =========================================================

def verifier_kill_switch(etat, capital):

    if capital < CAPITAL_INITIAL * SEUIL_RUINE:

        telegram(f"🚨 SEUIL RUINE\nCapital {capital}€")

        return "RUINE"

    pu = etat.get("pause_until", 0)

    if time.time() < pu:
        time.sleep(60)
        return "PAUSE"

    else:

        if etat.get("pertes_consecutives", 0) >= MAX_PERTES_CONSECUTIVES:

            etat["pertes_consecutives"] = 0

            sauvegarder_etat(etat)

    if etat["pertes_consecutives"] >= MAX_PERTES_CONSECUTIVES:

        telegram("⚠️ KILL SWITCH — pause 12h")

        etat["pause_until"] = int(time.time()) + PAUSE_DUREE

        etat["pertes_consecutives"] = 0

        sauvegarder_etat(etat)

        return "PAUSE"

    return "OK"


# =========================================================
# DASHBOARD
# =========================================================

def afficher_dashboard(etat):

    wr = (
        etat["nb_wins"] / etat["nb_trades"] * 100
    ) if etat["nb_trades"] > 0 else 0

    perf = (
        (etat["capital"] - CAPITAL_INITIAL)
        / CAPITAL_INITIAL
        * 100
    )

    log.info(
        f"\n  Capital {etat['capital']}€ "
        f"({'+' if perf>=0 else ''}{round(perf,2)}%) "
        f"| Trades {etat['nb_trades']} "
        f"| WR {wr:.1f}% "
        f"| Net {etat['cumul_net']}€"
    )


# =========================================================
# MAIN
# =========================================================

def demarrer_bot():

    log.info(f"DEMARRAGE V7.5 — {datetime.now()}")

    init_database()

    etat = charger_etat()

    afficher_dashboard(etat)

    telegram(
        f"🚀 <b>BOT V7.5</b>\n"
        f"Capital {etat['capital']}€\n"
        f"🛡️ Filtres anti-chute/pump actifs"
    )

    while True:

        try:

            s = verifier_kill_switch(etat, etat["capital"])

            if s == "RUINE":
                break

            if s == "PAUSE":
                etat = charger_etat()
                continue

            sym, d, det = choisir_meilleur_marche()

            if d == "NEUTRE" or sym is None:

                etat["nb_skips"] += 1

                sauvegarder_etat(etat)

                time.sleep(PAUSE)

                continue

            etat["nb_trades"] += 1

            res, gain, mise, ti = simuler_trade(
                sym,
                d,
                etat["nb_trades"],
                etat["capital"],
                det,
                etat
            )

            if res == "ERREUR":

                etat["nb_trades"] -= 1

                time.sleep(PAUSE)

                continue

            etat["capital"] = round(etat["capital"] + gain, 2)

            etat["cumul_net"] = round(
                etat["capital"] - CAPITAL_INITIAL,
                2
            )

            if res == "GAGNE":

                etat["nb_wins"] += 1

                etat["total_gagne"] = round(
                    etat["total_gagne"] + gain,
                    2
                )

                etat["pertes_consecutives"] = 0

                gp = (
                    gain / max(mise * LEVIER, 1)
                ) * 100

                etat["avg_win_pct"] = (
                    gp
                    if etat["avg_win_pct"] == 0
                    else round(
                        (
                            etat["avg_win_pct"]
                            * (etat["nb_wins"] - 1)
                            + gp
                        ) / etat["nb_wins"],
                        4
                    )
                )

            else:

                etat["nb_losses"] += 1

                etat["total_perdu"] = round(
                    etat["total_perdu"] + abs(gain),
                    2
                )

                etat["pertes_consecutives"] += 1

                pp = (
                    abs(gain) / max(mise * LEVIER, 1)
                ) * 100

                etat["avg_loss_pct"] = (
                    pp
                    if etat["avg_loss_pct"] == 0
                    else round(
                        (
                            etat["avg_loss_pct"]
                            * (etat["nb_losses"] - 1)
                            + pp
                        ) / etat["nb_losses"],
                        4
                    )
                )

            enregistrer_trade({
                'marche': sym,
                'direction': d,
                'resultat': res,
                'prix_entree': ti['prix_entree'],
                'prix_sortie': ti['prix_sortie'],
                'stop_loss': ti['stop_loss'],
                'objectif': ti['objectif'],
                'mise': mise,
                'gain': round(gain, 2),
                'capital_apres': etat['capital'],
                'duree_minutes': ti['duree_minutes'],
                'score': None,
                'adx': det.get('adx'),
                'atr': det.get('atr'),
                'rsi': det.get('rsi')
            })

            sauvegarder_etat(etat)

            etat['historique'].append({
                'heure': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'marche': sym,
                'direction': d,
                'resultat': res,
                'gain': round(gain, 2),
                'mise': round(mise, 2),
                'capital': etat['capital']
            })

            afficher_dashboard(etat)

            time.sleep(PAUSE)

        except KeyboardInterrupt:
            break

        except Exception as e:

            log.error(f"Erreur : {e}")

            time.sleep(60)


if __name__ == "__main__":
    demarrer_bot()
