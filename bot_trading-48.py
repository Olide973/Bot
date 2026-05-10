"""
╔══════════════════════════════════════════════════════════════╗
║           BOT MEAN REVERSION V7.2 — BOT 2                   ║
║   RSI < 30 → ACHAT | RSI > 70 → VENTE                      ║
║   10 marchés | H1 | Stop ATR×2.5 | Ratio 1:2               ║
║   Sortie partielle 50% | Capital 215€ | Levier x10          ║
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

CAPITAL_INITIAL         = 215.0
LEVIER                  = 10
MISE_FIXE_PCT           = 0.20
ATR_MULTIPLIER          = 2.5

# Trailing Stop Progressif — Bot 2
TRAILING_NIVEAUX = [
    (100, 0.05),   # PnL > +100€ → ATR × 0.05 → protège ~+97€
    ( 75, 0.07),   # PnL > +75€  → ATR × 0.07 → protège ~+72€
    ( 50, 0.10),   # PnL > +50€  → ATR × 0.10 → protège ~+47€
    ( 35, 0.15),   # PnL > +35€  → ATR × 0.15 → protège ~+32€
    ( 25, 0.20),   # PnL > +25€  → ATR × 0.20 → protège ~+22€
    ( 18, 0.30),   # PnL > +18€  → ATR × 0.30 → protège ~+15€
    ( 12, 0.50),   # PnL > +12€  → ATR × 0.50 → protège ~+10€
    ( 11, 0.80),   # PnL > +11€  → ATR × 0.80 → protège ~+9€
    (  8, 1.50),   # PnL > +8€   → ATR × 1.50 → protège ~+6€
    (  0, 2.50),   # Par défaut  → ATR × 2.50
]

def get_multiplicateur_atr(pnl):
    for seuil, mult in TRAILING_NIVEAUX:
        if pnl >= seuil:
            return mult
    return 2.50
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
PAUSE_DUREE             = 86400

import json
ETAT_FILE = "etat_bot2.json"

MARCHES = [
    # 10 marchés validés
    "BTCUSDT", "ETHUSDT", "XRPUSDT", "ATOMUSDT", "LINKUSDT",
    "ADAUSDT", "SOLUSDT", "AVAXUSDT", "NEARUSDT", "DOTUSDT",
    # 5 nouveaux marchés
    "DOGEUSDT", "BNBUSDT", "TRXUSDT", "LTCUSDT", "MATICUSDT"
]

KRAKEN_SYMBOLS = {
    "BTCUSDT":  "XXBTZUSD",
    "ETHUSDT":  "XETHZUSD",
    "XRPUSDT":  "XXRPZUSD",
    "ATOMUSDT": "ATOMUSD",
    "LINKUSDT": "LINKUSD",
    "ADAUSDT":  "ADAUSD",
    "SOLUSDT":  "SOLUSD",
    "AVAXUSDT": "AVAXUSD",
    "NEARUSDT": "NEARUSD",
    "DOTUSDT":  "DOTUSD",
    "DOGEUSDT": "XDGUSD",
    "BNBUSDT":  "BNBUSD",
    "TRXUSDT":  "TRXUSD",
    "LTCUSDT":  "XLTCZUSD",
    "MATICUSDT":"MATICUSD"
}

log.info("=" * 55)
log.info("  BOT MEAN REVERSION V7.2 — BOT 2")
log.info(f"  Capital : {CAPITAL_INITIAL}EUR | Levier x{LEVIER} | Mise {MISE_FIXE_PCT*100}%")
log.info(f"  RSI < {RSI_ACHAT} → ACHAT | RSI > {RSI_VENTE} → VENTE")
log.info(f"  Stop ATR×{ATR_MULTIPLIER} | Ratio 1:{RATIO_RR}")
log.info(f"  Marchés : {len(MARCHES)} cryptos (10 validés + 5 nouveaux)")
log.info("=" * 55)

# ══════════════════════════════════════════════════════════════
# DONNÉES
# ══════════════════════════════════════════════════════════════

def get_prix_actuel(symbole):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url = "https://api.kraken.com/0/public/Ticker"
    try:
        r = requests.get(url, params={"pair": kraken_symbol}, timeout=10)
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
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": kraken_symbol, "interval": 60}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        errors = data.get("error", [])
        if errors:
            return None
        result = data.get("result", {})
        keys = [k for k in result.keys() if k != "last"]
        if not keys:
            return None
        candles = result[keys[0]]
        df = pd.DataFrame(candles, columns=[
            'time','open','high','low','close','vwap','volume','count'
        ])
        df = df.astype({'high': float, 'low': float, 'close': float, 'volume': float})
        return df.tail(limite).reset_index(drop=True)
    except Exception as e:
        log.error(f"Erreur klines {symbole} : {e}")
        return None

# ══════════════════════════════════════════════════════════════
# INDICATEURS
# ══════════════════════════════════════════════════════════════

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

# ══════════════════════════════════════════════════════════════
# ANALYSE MEAN REVERSION
# ══════════════════════════════════════════════════════════════

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

    prix    = df['close'].iloc[-1]
    atr_pct = (atr / prix) * 100

    if adx > ADX_MAX:
        log.info(f"  {symbole} : ADX {adx} > {ADX_MAX} → skip")
        return "NEUTRE", {}

    details = {
        "adx": adx, "atr": atr, "rsi": rsi,
        "atr_pct": atr_pct, "volume_ratio": volume_ratio,
        "df": df
    }

    if rsi < RSI_ACHAT:
        log.info(f"  {symbole} : RSI {rsi} < {RSI_ACHAT} → SURVENDU → ACHAT ✅")
        return "ACHAT", details
    elif rsi > RSI_VENTE:
        log.info(f"  {symbole} : RSI {rsi} > {RSI_VENTE} → SURACHETÉ → VENTE ✅")
        return "VENTE", details
    else:
        log.info(f"  {symbole} : RSI {rsi} | ADX {adx} → pas de signal")
        return "NEUTRE", details

def choisir_meilleur_marche():
    log.info(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scan Mean Reversion — {len(MARCHES)} marchés...")
    signaux = {}

    for marche in MARCHES:
        direction, details = analyser_marche(marche)
        if direction != "NEUTRE":
            signaux[marche] = {"direction": direction, "details": details}
        time.sleep(0.5)

    if not signaux:
        log.info("  => Aucun signal. On attend...")
        return None, "NEUTRE", {}

    meilleur = max(signaux.items(),
                   key=lambda x: (abs(x[1]["details"].get("rsi", 50) - 50),
                                  x[1]["details"].get("atr_pct", 0)))[0]

    direction = signaux[meilleur]["direction"]
    rsi       = signaux[meilleur]["details"].get("rsi", 50)
    adx       = signaux[meilleur]["details"].get("adx", 0)

    log.info(f"\n  => MEILLEUR SIGNAL : {meilleur} ({direction})")
    log.info(f"     RSI {rsi} | ADX {adx}")

    return meilleur, direction, signaux[meilleur]["details"]

# ══════════════════════════════════════════════════════════════
# SIMULATION DU TRADE
# ══════════════════════════════════════════════════════════════

def simuler_trade(symbole, direction, numero_trade, details):
    prix_entree = get_prix_actuel(symbole)
    if prix_entree is None:
        return "ERREUR", 0

    atr  = details.get("atr", 0)
    mise = capital * MISE_FIXE_PCT  # Compoundage — mise basée sur capital actuel

    if direction == "ACHAT":
        stop_loss        = round(prix_entree - (atr * ATR_MULTIPLIER), 8)
        objectif_partiel = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
        objectif_final   = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_RR), 8)
    else:
        stop_loss        = round(prix_entree + (atr * ATR_MULTIPLIER), 8)
        objectif_partiel = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
        objectif_final   = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_RR), 8)

    distance_stop_pct = (abs(prix_entree - stop_loss) / prix_entree) * 100

    log.info(f"\n  {'='*50}")
    log.info(f"  TRADE #{numero_trade} [MEAN_REV] — {datetime.now().strftime('%H:%M:%S')}")
    log.info(f"  {'='*50}")
    log.info(f"  Symbole          : {symbole} ({direction})")
    log.info(f"  RSI              : {details.get('rsi', 0)}")
    log.info(f"  Prix entree      : {prix_entree}")
    log.info(f"  Stop ATR×{ATR_MULTIPLIER}     : {stop_loss} ({round(distance_stop_pct,2)}%)")
    log.info(f"  Objectif partiel : {objectif_partiel}")
    log.info(f"  Objectif final   : {objectif_final}")
    log.info(f"  Mise             : {mise}EUR | Levier x{LEVIER}\n")

    debut           = time.time()
    stop_actuel     = stop_loss
    meilleur_prix   = prix_entree
    dernier_log     = 0
    partiel_execute = False
    gain_partiel    = 0
    distance_stop   = abs(prix_entree - stop_loss)

    while True:
        time.sleep(CHECK_INTERVAL)

        prix_actuel = get_prix_actuel(symbole)
        if prix_actuel is None:
            continue

        # Calcul PnL
        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)

        # Trailing Stop Progressif
        multiplicateur    = get_multiplicateur_atr(pnl)
        distance_trailing = atr * multiplicateur

        if direction == "ACHAT":
            if prix_actuel > meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix - distance_trailing, 8)
            if nouveau_stop > stop_actuel:
                stop_actuel = nouveau_stop
                log.info(f"  [TRAILING] PnL {'+' if pnl>=0 else ''}{pnl}EUR → ATR×{multiplicateur} | Stop : {nouveau_stop}")
            atteint_partiel = not partiel_execute and prix_actuel >= objectif_partiel
            atteint_final   = prix_actuel >= objectif_final
            atteint_stop    = prix_actuel <= stop_actuel
        else:
            if prix_actuel < meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix + distance_trailing, 8)
            if nouveau_stop < stop_actuel:
                stop_actuel = nouveau_stop
                log.info(f"  [TRAILING] PnL {'+' if pnl>=0 else ''}{pnl}EUR → ATR×{multiplicateur} | Stop : {nouveau_stop}")
            atteint_partiel = not partiel_execute and prix_actuel <= objectif_partiel
            atteint_final   = prix_actuel <= objectif_final
            atteint_stop    = prix_actuel >= stop_actuel

        duree = int((time.time() - debut) / 60)

        if time.time() - dernier_log >= 60:
            log.info(f"  [{datetime.now().strftime('%H:%M:%S')}] {symbole}: {prix_actuel} | "
                     f"PnL: {'+' if pnl >= 0 else ''}{pnl}EUR | "
                     f"Stop: {stop_actuel} | {duree}min"
                     f"{' | PARTIEL ✅' if partiel_execute else ''}")
            dernier_log = time.time()

        if atteint_partiel:
            gain_partiel    = round(pnl * 0.5, 2)
            partiel_execute = True
            log.info(f"  SORTIE PARTIELLE 50% ! +{gain_partiel}EUR ✅")
            continue

        if atteint_final:
            gain_final = round(pnl * 0.5, 2) if partiel_execute else pnl
            gain_total = round(gain_partiel + gain_final, 2)
            log.info(f"\n  OBJECTIF FINAL ! +{gain_total}EUR 🎉")
            return "GAGNE", gain_total

        if atteint_stop:
            if partiel_execute:
                gain_reste = round(pnl * 0.5, 2)
                gain_total = round(gain_partiel + gain_reste, 2)
                resultat   = "GAGNE" if gain_total > 0 else "PERDU"
                log.info(f"\n  STOP (après partiel) — {'+' if gain_total>=0 else ''}{gain_total}EUR")
                return resultat, gain_total
            else:
                log.info(f"\n  STOP-LOSS ! {pnl}EUR")
                return "PERDU", pnl

        if time.time() - debut >= TIMEOUT_TRADE:
            if partiel_execute:
                gain_reste = round(pnl * 0.5, 2)
                gain_total = round(gain_partiel + gain_reste, 2)
            else:
                gain_total = pnl
            resultat = "GAGNE" if gain_total > 0 else "PERDU"
            log.info(f"\n  TIMEOUT — {'+' if gain_total>=0 else ''}{gain_total}EUR")
            return resultat, gain_total

# ══════════════════════════════════════════════════════════════
# GESTION ÉTAT (JSON simple)
# ══════════════════════════════════════════════════════════════

def charger_etat():
    if os.path.exists(ETAT_FILE):
        with open(ETAT_FILE, "r") as f:
            return json.load(f)
    return {
        "capital": CAPITAL_INITIAL,
        "total_gagne": 0.0, "total_perdu": 0.0,
        "cumul_net": 0.0, "nb_trades": 0,
        "nb_wins": 0, "nb_losses": 0,
        "pertes_consecutives": 0, "pause_until": 0,
        "historique": []
    }

def sauvegarder_etat(etat):
    with open(ETAT_FILE, "w") as f:
        json.dump(etat, f, indent=2, ensure_ascii=False)

def afficher_tableau_de_bord(etat):
    win_rate = (etat["nb_wins"] / etat["nb_trades"] * 100) if etat["nb_trades"] > 0 else 0
    perf     = ((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
    log.info(f"\n  {'='*55}")
    log.info(f"  BOT MEAN REVERSION V7.2 — TABLEAU DE BORD")
    log.info(f"  {'='*55}")
    log.info(f"  Capital actuel : {round(etat['capital'],2)}EUR ({'+' if perf>=0 else ''}{round(perf,2)}%)")
    log.info(f"  Trades total   : {etat['nb_trades']}")
    log.info(f"  Victoires      : {etat['nb_wins']} ({win_rate:.1f}%)")
    log.info(f"  Defaites       : {etat['nb_losses']}")
    log.info(f"  Pertes consec. : {etat['pertes_consecutives']}/{MAX_PERTES_CONSECUTIVES}")
    log.info(f"  Total gagne    : +{round(etat['total_gagne'],2)}EUR")
    log.info(f"  Total perdu    : -{round(etat['total_perdu'],2)}EUR")
    log.info(f"  BENEFICE NET   : {'+' if etat['cumul_net']>=0 else ''}{round(etat['cumul_net'],2)}EUR")
    if etat.get("historique"):
        log.info(f"\n  Derniers trades :")
        for h in etat["historique"][-5:]:
            icone = "OK" if h["resultat"] == "GAGNE" else "XX"
            log.info(f"    [{icone}] {h['heure']} | {h['marche']} | {h['direction']} | "
                     f"{'+' if h['gain']>=0 else ''}{h['gain']}EUR | Capital: {h['capital']}EUR")
    log.info(f"  {'='*55}")

# ══════════════════════════════════════════════════════════════
# KILL SWITCH
# ══════════════════════════════════════════════════════════════

def verifier_kill_switch(etat, capital):
    if capital < CAPITAL_INITIAL * SEUIL_RUINE:
        log.critical(f"SEUIL DE RUINE ! Capital {capital}EUR")
        return "RUINE"

    if time.time() < etat.get("pause_until", 0):
        restant = int((etat["pause_until"] - time.time()) / 60)
        log.info(f"  En pause — {restant} minutes restantes")
        time.sleep(60)
        return "PAUSE"

    if etat["pertes_consecutives"] >= MAX_PERTES_CONSECUTIVES:
        log.warning(f"KILL SWITCH — pause 24h !")
        etat["pause_until"]         = int(time.time()) + PAUSE_DUREE
        etat["pertes_consecutives"] = 0
        sauvegarder_etat(etat)
        return "PAUSE"

    return "OK"

# ══════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════════════

def demarrer_bot():
    log.info(f"DEMARRAGE BOT MEAN REVERSION V7.2 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    etat = charger_etat()
    afficher_tableau_de_bord(etat)

    while True:
        try:
            statut = verifier_kill_switch(etat, etat["capital"])
            if statut == "RUINE":
                break
            if statut == "PAUSE":
                etat = charger_etat()
                continue

            symbole, direction, details = choisir_meilleur_marche()

            if direction == "NEUTRE" or symbole is None:
                log.info(f"  Nouvelle analyse dans 2 minutes...")
                time.sleep(PAUSE)
                continue

            etat["nb_trades"] += 1
            resultat, gain = simuler_trade(symbole, direction, etat["nb_trades"], details)

            if resultat == "ERREUR":
                etat["nb_trades"] -= 1
                time.sleep(PAUSE)
                continue

            etat["capital"]   = round(etat["capital"] + gain, 2)
            etat["cumul_net"] = round(etat["capital"] - CAPITAL_INITIAL, 2)

            if resultat == "GAGNE":
                etat["nb_wins"]            += 1
                etat["total_gagne"]         = round(etat["total_gagne"] + gain, 2)
                etat["pertes_consecutives"] = 0
            else:
                etat["nb_losses"]          += 1
                etat["total_perdu"]         = round(etat["total_perdu"] + abs(gain), 2)
                etat["pertes_consecutives"] += 1

            etat["historique"].append({
                "heure":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                "marche":    symbole,
                "direction": direction,
                "resultat":  resultat,
                "gain":      round(gain, 2),
                "capital":   etat["capital"]
            })

            sauvegarder_etat(etat)
            afficher_tableau_de_bord(etat)
            log.info(f"  Pause 2 minutes avant prochain trade...")
            time.sleep(PAUSE)

        except KeyboardInterrupt:
            log.info("Bot arrete.")
            break
        except Exception as e:
            log.error(f"Erreur : {e}")
            time.sleep(60)

if __name__ == "__main__":
    demarrer_bot()
