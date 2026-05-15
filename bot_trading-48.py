"""
╔══════════════════════════════════════════════════════════════╗
║        BOT MEAN REVERSION V7.4 — MULTI-MARCHÉS             ║
║   Base exacte V7.4 + 3 trades simultanés                   ║
║   RSI < 30 → ACHAT | RSI > 70 → VENTE                     ║
║   13 marchés | H1 | Stop ATR×2.5 | Ratio 1:2              ║
║   Trailing Stop CONTINU + BREAK-EVEN VERROUILLÉ            ║
║   Paliers : +0.75€, +3€, +7.50€, +12€, +18€...            ║
╚══════════════════════════════════════════════════════════════╝
"""

import requests
import time
import os
import logging
import threading
import pandas as pd
from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Paramètres (identiques V7.4)
CAPITAL_INITIAL         = 200.0
LEVIER                  = 3
MISE_FIXE_PCT           = 0.05      # 5% du capital = 10€ par trade
MAX_TRADES_SIMULTANES   = 3         # nouveauté vs V7.4
ATR_MULTIPLIER          = 2.5
RATIO_RR                = 2.0
RATIO_PARTIEL           = 1.0
PAUSE                   = 120
CHECK_INTERVAL          = 15
TIMEOUT_TRADE           = 12 * 3600
RSI_ACHAT               = 30
RSI_VENTE               = 70
VOLUME_MINI             = 0.40
ADX_MAX                 = 40
MAX_PERTES_CONSECUTIVES = 4
PAUSE_DUREE             = 600       # 10 minutes
DAILY_KILL              = -5.0      # kill switch journalier

# ── Break-even (identique V7.4)
BREAK_EVEN_TRIGGER_PNL  = 0.75
BREAK_EVEN_BUFFER_PCT   = 0.001

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# ── Paliers trailing stop (identiques V7.4)
TRAILING_NIVEAUX = [
    (100,  0.05),
    ( 75,  0.07),
    ( 50,  0.10),
    ( 35,  0.15),
    ( 25,  0.20),
    ( 18,  0.30),
    ( 12,  0.50),
    (7.5,  0.80),
    (  3,  1.50),
    (0.75, 2.00),
    (  0,  2.50),
]

def get_multiplicateur_atr(pnl):
    for seuil, mult in TRAILING_NIVEAUX:
        if pnl >= seuil:
            return mult
    return 2.50

# ── Marchés (13 au lieu de 8)
MARCHES = [
    "XRPUSDT",  "ATOMUSDT", "LINKUSDT", "ADAUSDT",
    "SOLUSDT",  "AVAXUSDT", "DOTUSDT",  "ETHUSDT",
    "BNBUSDT",  "DOGEUSDT", "LTCUSDT",  "XBTUSDT",
    "MATICUSDT"
]

KRAKEN_SYMBOLS = {
    "XRPUSDT":   "XXRPZUSD",
    "ATOMUSDT":  "ATOMUSD",
    "LINKUSDT":  "LINKUSD",
    "ADAUSDT":   "ADAUSD",
    "SOLUSDT":   "SOLUSD",
    "AVAXUSDT":  "AVAXUSD",
    "DOTUSDT":   "DOTUSD",
    "ETHUSDT":   "XETHZUSD",
    "BNBUSDT":   "BNBUSD",
    "DOGEUSDT":  "XDGUSD",
    "LTCUSDT":   "XLTCZUSD",
    "XBTUSDT":   "XXBTZUSD",
    "MATICUSDT": "MATICUSD",
}

# ── État global
capital             = CAPITAL_INITIAL
pnl_journalier      = 0.0
nb_trades           = 0
nb_wins             = 0
nb_losses           = 0
total_gagne         = 0.0
total_perdu         = 0.0
pertes_consecutives = 0
pause_until         = 0
trades_actifs       = {}   # {symbole: thread}
lock                = threading.Lock()

log.info("=" * 55)
log.info("  BOT MEAN REVERSION V7.4 MULTI-MARCHÉS")
log.info(f"  Capital : {CAPITAL_INITIAL}€ | Levier x{LEVIER} | Mise {MISE_FIXE_PCT*100}%")
log.info(f"  RSI < {RSI_ACHAT} → ACHAT | RSI > {RSI_VENTE} → VENTE")
log.info(f"  Stop ATR×{ATR_MULTIPLIER} | Ratio 1:{RATIO_RR}")
log.info(f"  Max trades simultanés : {MAX_TRADES_SIMULTANES}")
log.info(f"  Break-even dès +{BREAK_EVEN_TRIGGER_PNL}€")
log.info(f"  Marchés : {len(MARCHES)}")
log.info("=" * 55)

def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Erreur Telegram : {e}")

def get_prix_actuel(symbole):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    try:
        r = requests.get("https://api.kraken.com/0/public/Ticker", params={"pair": kraken_symbol}, timeout=10)
        data = r.json()
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

def get_klines(symbole, limite=100):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    try:
        r = requests.get("https://api.kraken.com/0/public/OHLC",
                         params={"pair": kraken_symbol, "interval": 60}, timeout=15)
        data = r.json()
        if data.get("error") and data["error"]:
            return None
        result = data.get("result", {})
        keys = [k for k in result.keys() if k != "last"]
        if not keys:
            return None
        candles = result[keys[0]]
        df = pd.DataFrame(candles, columns=['time','open','high','low','close','vwap','volume','count'])
        df = df.astype({'high': float, 'low': float, 'close': float, 'volume': float})
        return df.tail(limite).reset_index(drop=True)
    except Exception as e:
        log.error(f"Erreur klines {symbole} : {e}")
        return None

def calculer_adx(df, periode=14):
    try:
        ind = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=periode)
        val = ind.adx().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 0
    except:
        return 0

def calculer_atr(df, periode=14):
    try:
        ind = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=periode)
        val = ind.average_true_range().iloc[-1]
        return round(float(val), 8) if not pd.isna(val) else 0
    except:
        return 0

def calculer_rsi(df, periode=14):
    try:
        ind = RSIIndicator(close=df['close'], window=periode)
        val = ind.rsi().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 50
    except:
        return 50

def verifier_volume(df):
    volumes = df['volume'].tolist()
    if len(volumes) < 10:
        return True, 0
    moyenne_24h   = sum(volumes[-24:]) / len(volumes[-24:])
    volume_recent = volumes[-1]
    ratio = volume_recent / moyenne_24h if moyenne_24h > 0 else 0
    return ratio >= VOLUME_MINI, round(ratio * 100, 1)

def analyser_marche(symbole):
    df = get_klines(symbole, limite=100)
    if df is None or len(df) < 30:
        return "NEUTRE", {}
    adx = calculer_adx(df)
    atr = calculer_atr(df)
    rsi = calculer_rsi(df)
    volume_ok, volume_ratio = verifier_volume(df)
    if not volume_ok:
        log.info(f"  {symbole} : Volume {volume_ratio}% < {VOLUME_MINI*100}% → skip")
        return "NEUTRE", {}
    if adx > ADX_MAX:
        log.info(f"  {symbole} : ADX {adx} > {ADX_MAX} → skip")
        return "NEUTRE", {}
    prix    = float(df['close'].iloc[-1])
    atr_pct = (atr / prix) * 100
    details = {"adx": adx, "atr": atr, "rsi": rsi, "atr_pct": atr_pct, "volume_ratio": volume_ratio}
    if rsi < RSI_ACHAT:
        log.info(f"  {symbole} : RSI {rsi} < {RSI_ACHAT} → SURVENDU → ACHAT")
        return "ACHAT", details
    elif rsi > RSI_VENTE:
        log.info(f"  {symbole} : RSI {rsi} > {RSI_VENTE} → SURACHETÉ → VENTE")
        return "VENTE", details
    else:
        log.info(f"  {symbole} : RSI {rsi} | ADX {adx} → pas de signal")
        return "NEUTRE", details

def executer_trade(symbole, direction, details):
    """Exécute un trade dans un thread séparé — logique identique V7.4."""
    global capital, pnl_journalier, nb_trades, nb_wins, nb_losses
    global total_gagne, total_perdu, pertes_consecutives, trades_actifs

    prix_entree = get_prix_actuel(symbole)
    if prix_entree is None:
        with lock:
            trades_actifs.pop(symbole, None)
        return

    atr  = details.get("atr", 0)
    with lock:
        mise = round(capital * MISE_FIXE_PCT, 2)
    mise = max(mise, 5.0)

    if direction == "ACHAT":
        stop_loss        = round(prix_entree - (atr * ATR_MULTIPLIER), 8)
        objectif_partiel = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
        objectif_final   = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_RR), 8)
    else:
        stop_loss        = round(prix_entree + (atr * ATR_MULTIPLIER), 8)
        objectif_partiel = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
        objectif_final   = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_RR), 8)

    with lock:
        nb_trades += 1
        num = nb_trades

    log.info(f"\n  {'='*50}")
    log.info(f"  TRADE #{num} — {symbole} ({direction}) — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  Prix: {prix_entree} | Stop: {stop_loss} | Obj: {objectif_final} | Mise: {mise}€×{LEVIER}")
    log.info(f"  Break-even dès +{BREAK_EVEN_TRIGGER_PNL}€")
    telegram(f"📊 <b>TRADE #{num} OUVERT</b>\n{'🟢' if direction=='ACHAT' else '🔴'} {symbole}\n"
             f"RSI: {details.get('rsi',0)} | Prix: {prix_entree}\nStop: {stop_loss} | Obj: {objectif_final}\n"
             f"Mise: {mise}€×{LEVIER}")

    debut             = time.time()
    stop_actuel       = stop_loss
    meilleur_prix     = prix_entree
    dernier_log       = 0
    partiel_execute   = False
    gain_partiel      = 0
    niveau_actuel     = 2.50
    break_even_locked = False
    pnl_max_atteint   = 0

    while True:
        time.sleep(CHECK_INTERVAL)
        prix_actuel = get_prix_actuel(symbole)
        if prix_actuel is None:
            continue

        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)

        if pnl > pnl_max_atteint:
            pnl_max_atteint = pnl

        multiplicateur    = get_multiplicateur_atr(pnl)
        distance_trailing = atr * multiplicateur

        # Break-even
        if not break_even_locked and pnl_max_atteint >= BREAK_EVEN_TRIGGER_PNL:
            if direction == "ACHAT":
                stop_be = round(prix_entree * (1 + BREAK_EVEN_BUFFER_PCT), 8)
                if stop_be > stop_actuel:
                    stop_actuel = stop_be
            else:
                stop_be = round(prix_entree * (1 - BREAK_EVEN_BUFFER_PCT), 8)
                if stop_be < stop_actuel:
                    stop_actuel = stop_be
            break_even_locked = True
            log.info(f"  🔒 BREAK-EVEN {symbole} → Stop: {stop_actuel} (PnL max: +{pnl_max_atteint}€)")
            telegram(f"🔒 <b>Break-even</b>\n{symbole} | Stop: {stop_actuel} | PnL max: +{pnl_max_atteint}€")

        # Trailing stop
        stop_modifie = False
        if direction == "ACHAT":
            if prix_actuel > meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix - distance_trailing, 8)
            if nouveau_stop > stop_actuel:
                stop_actuel = nouveau_stop
                stop_modifie = True
            # Protection break-even — stop ne descend jamais sous break-even
            if break_even_locked:
                stop_be_min = round(prix_entree * (1 + BREAK_EVEN_BUFFER_PCT), 8)
                if stop_actuel < stop_be_min:
                    stop_actuel = stop_be_min
        else:
            if prix_actuel < meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix + distance_trailing, 8)
            if nouveau_stop < stop_actuel:
                stop_actuel = nouveau_stop
                stop_modifie = True
            # Protection break-even — stop ne remonte jamais au-dessus break-even
            if break_even_locked:
                stop_be_max = round(prix_entree * (1 - BREAK_EVEN_BUFFER_PCT), 8)
                if stop_actuel > stop_be_max:
                    stop_actuel = stop_be_max

        if multiplicateur != niveau_actuel and stop_modifie:
            if direction == "ACHAT":
                gain_protege = round((stop_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
            else:
                gain_protege = round((prix_entree - stop_actuel) / prix_entree * mise * LEVIER, 2)
            log.info(f"  [TRAILING] {symbole} PnL {pnl:+.2f}€ → ATR×{multiplicateur} | Stop: {stop_actuel} | Protège: ~{gain_protege}€")
            niveau_actuel = multiplicateur

        # Conditions de sortie
        if direction == "ACHAT":
            atteint_partiel = not partiel_execute and prix_actuel >= objectif_partiel
            atteint_final   = prix_actuel >= objectif_final
            atteint_stop    = prix_actuel <= stop_actuel
        else:
            atteint_partiel = not partiel_execute and prix_actuel <= objectif_partiel
            atteint_final   = prix_actuel <= objectif_final
            atteint_stop    = prix_actuel >= stop_actuel

        duree = int((time.time() - debut) / 60)
        if time.time() - dernier_log >= 60:
            be_flag = " 🔒" if break_even_locked else ""
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole}: {prix_actuel} | "
                     f"PnL: {pnl:+.2f}€ | Stop: {stop_actuel} (ATR×{multiplicateur}){be_flag} | {duree}min")
            dernier_log = time.time()

        if atteint_partiel:
            gain_partiel    = round(pnl * 0.5, 2)
            partiel_execute = True
            log.info(f"  ⚡ PARTIEL {symbole} : +{gain_partiel}€")
            telegram(f"⚡ <b>PARTIEL</b> {symbole} | +{gain_partiel}€")
            continue

        gain_total = None
        resultat   = None

        if atteint_final:
            gain_final = round(pnl * 0.5, 2) if partiel_execute else pnl
            gain_total = round(gain_partiel + gain_final, 2)
            resultat   = "GAGNE"
            log.info(f"  🎯 OBJECTIF {symbole} : +{gain_total}€ ({duree}min)")
            telegram(f"🎯 <b>OBJECTIF</b> {symbole}\nGain: +{gain_total}€ | {duree}min")

        elif atteint_stop:
            if partiel_execute:
                gain_reste = round(pnl * 0.5, 2)
                gain_total = round(gain_partiel + gain_reste, 2)
                resultat   = "GAGNE" if gain_total > 0 else "PERDU"
            else:
                gain_total = pnl
                resultat   = "PERDU"
            if break_even_locked and gain_total >= 0:
                log.info(f"  🔒 STOP BREAK-EVEN {symbole} : +{gain_total}€ ({duree}min)")
                telegram(f"🔒 <b>BREAK-EVEN</b> {symbole} | +{gain_total}€ | {duree}min")
            else:
                log.info(f"  🛑 STOP {symbole} : {gain_total:+.2f}€ ({duree}min)")
                telegram(f"🛑 <b>STOP</b> {symbole} | {gain_total:+.2f}€ | {duree}min")

        elif time.time() - debut >= TIMEOUT_TRADE:
            if partiel_execute:
                gain_total = round(gain_partiel + round(pnl * 0.5, 2), 2)
            else:
                gain_total = pnl
            resultat = "GAGNE" if gain_total > 0 else "PERDU"
            log.info(f"  ⏱ TIMEOUT {symbole} : {gain_total:+.2f}€")
            telegram(f"⏱ <b>TIMEOUT</b> {symbole} | {gain_total:+.2f}€")

        if gain_total is not None:
            with lock:
                capital          = round(capital + gain_total, 2)
                pnl_journalier   = round(pnl_journalier + gain_total, 2)
                if resultat == "GAGNE":
                    nb_wins             += 1
                    total_gagne         = round(total_gagne + gain_total, 2)
                    pertes_consecutives  = 0
                else:
                    nb_losses           += 1
                    total_perdu         = round(total_perdu + abs(gain_total), 2)
                    pertes_consecutives += 1
                trades_actifs.pop(symbole, None)

            win_rate = nb_wins / nb_trades * 100 if nb_trades > 0 else 0
            log.info(f"\n  Capital: {capital:.2f}€ | PnL jour: {pnl_journalier:+.2f}€ | "
                     f"WR: {win_rate:.1f}% | Trades: {nb_trades}")
            break

def afficher_tableau_de_bord():
    win_rate = (nb_wins / nb_trades * 100) if nb_trades > 0 else 0
    perf     = ((capital - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
    log.info(f"\n  {'='*55}")
    log.info(f"  BOT V7.4 MULTI-MARCHÉS — TABLEAU DE BORD")
    log.info(f"  {'='*55}")
    log.info(f"  💰 Capital      : {capital:.2f}€ ({perf:+.2f}%)")
    log.info(f"  📊 PnL journalier: {pnl_journalier:+.2f}€")
    log.info(f"  🔒 Trades actifs : {len(trades_actifs)}/{MAX_TRADES_SIMULTANES}")
    log.info(f"  📈 Trades total  : {nb_trades} | WR: {win_rate:.1f}%")
    log.info(f"  ✅ Gagné        : +{total_gagne:.2f}€")
    log.info(f"  ❌ Perdu        : -{total_perdu:.2f}€")
    log.info(f"  💎 NET          : {capital - CAPITAL_INITIAL:+.2f}€")
    log.info(f"  {'='*55}")

def demarrer_bot():
    global pause_until, pertes_consecutives, pnl_journalier

    while True:
        try:
            # Kill switch journalier
            if pnl_journalier <= DAILY_KILL:
                log.warning(f"[KILL SWITCH] PnL jour {pnl_journalier:.2f}€ ≤ {DAILY_KILL}€ — arrêt")
                break

            # Cooldown
            if time.time() < pause_until:
                restant = int((pause_until - time.time()) / 60)
                log.info(f"  [COOLDOWN] {restant} min restantes")
                time.sleep(60)
                continue
            elif pause_until > 0:
                pertes_consecutives = 0
                pause_until = 0

            # Déclenchement cooldown
            if pertes_consecutives >= MAX_PERTES_CONSECUTIVES:
                pause_until = time.time() + PAUSE_DUREE
                pertes_consecutives = 0
                log.warning(f"[COOLDOWN] {MAX_PERTES_CONSECUTIVES} pertes → pause {PAUSE_DUREE//60} min")
                telegram(f"⚠️ <b>COOLDOWN</b>\n{MAX_PERTES_CONSECUTIVES} pertes consécutives\nPause {PAUSE_DUREE//60} min")
                continue

            # Dashboard toutes les 20 cycles
            afficher_tableau_de_bord()

            # Scanner les marchés si place disponible
            with lock:
                nb_actifs = len(trades_actifs)

            if nb_actifs < MAX_TRADES_SIMULTANES:
                log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan — {len(MARCHES)} marchés "
                         f"({nb_actifs}/{MAX_TRADES_SIMULTANES} trades actifs)...")

                signaux = []
                for marche in MARCHES:
                    with lock:
                        if marche in trades_actifs:
                            continue
                    direction, details = analyser_marche(marche)
                    if direction != "NEUTRE":
                        signaux.append((marche, direction, details))
                    time.sleep(0.5)

                # Trier par RSI le plus extrême (comme V7.4)
                signaux.sort(key=lambda x: abs(x[2].get("rsi", 50) - 50), reverse=True)

                for marche, direction, details in signaux:
                    with lock:
                        if len(trades_actifs) >= MAX_TRADES_SIMULTANES:
                            break
                        if marche in trades_actifs:
                            continue
                        t = threading.Thread(
                            target=executer_trade,
                            args=(marche, direction, details),
                            daemon=True
                        )
                        trades_actifs[marche] = t
                        t.start()
                        log.info(f"  => TRADE LANCÉ : {marche} {direction}")

                if not signaux:
                    log.info("  => Aucun signal. Prochaine analyse dans 2 min...")

            time.sleep(PAUSE)

        except KeyboardInterrupt:
            log.info("Arrêt demandé.")
            break
        except Exception as e:
            log.error(f"Erreur : {e}")
            time.sleep(60)

if __name__ == "__main__":
    demarrer_bot()
