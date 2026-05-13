"""
BOT MEAN REVERSION V7.2 - BOT 2
"""

import requests
import time
import os
import logging
import json
import pandas as pd
from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

CAPITAL_INITIAL = 215.0
LEVIER = 10
MISE_FIXE_PCT = 0.20
ATR_MULTIPLIER = 2.5

TRAILING_NIVEAUX = [
    (100, 0.05),
    ( 75, 0.07),
    ( 50, 0.10),
    ( 35, 0.15),
    ( 25, 0.20),
    ( 18, 0.30),
    ( 12, 0.50),
    (7.5, 0.80),
    (  3, 1.50),
    (0.75, 2.00),
    (  0, 2.50),
]

def get_multiplicateur_atr(pnl):
    for seuil, mult in TRAILING_NIVEAUX:
        if pnl >= seuil:
            return mult
    return 2.50

RATIO_RR = 2.0
RATIO_PARTIEL = 1.0
PAUSE = 120
CHECK_INTERVAL = 10
TIMEOUT_TRADE = 12 * 3600
RSI_ACHAT = 30
RSI_VENTE = 70
VOLUME_MINI = 0.40
ADX_MAX = 40
MAX_PERTES_CONSECUTIVES = 2
SEUIL_RUINE = 0.30
PAUSE_DUREE = 43200

ETAT_FILE = "etat_bot2.json"

MARCHES = ["BTCUSDT","ETHUSDT","XRPUSDT","ATOMUSDT","LINKUSDT","ADAUSDT","SOLUSDT","AVAXUSDT","NEARUSDT","DOTUSDT","DOGEUSDT","BNBUSDT","TRXUSDT","LTCUSDT","MATICUSDT"]

KRAKEN_SYMBOLS = {"BTCUSDT":"XXBTZUSD","ETHUSDT":"XETHZUSD","XRPUSDT":"XXRPZUSD","ATOMUSDT":"ATOMUSD","LINKUSDT":"LINKUSD","ADAUSDT":"ADAUSD","SOLUSDT":"SOLUSD","AVAXUSDT":"AVAXUSD","NEARUSDT":"NEARUSD","DOTUSDT":"DOTUSD","DOGEUSDT":"XDGUSD","BNBUSDT":"BNBUSD","TRXUSDT":"TRXUSD","LTCUSDT":"XLTCZUSD","MATICUSDT":"MATICUSD"}

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
        r = requests.get("https://api.kraken.com/0/public/OHLC", params={"pair": kraken_symbol, "interval": 60}, timeout=15)
        data = r.json()
        if data.get("error", []):
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
        val = ADXIndicator(high=df['high'], low=df['low'], close=df['close'], window=periode).adx().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 0
    except:
        return 0

def calculer_atr(df, periode=14):
    try:
        val = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=periode).average_true_range().iloc[-1]
        return round(float(val), 8) if not pd.isna(val) else 0
    except:
        return 0

def calculer_rsi(df, periode=14):
    try:
        val = RSIIndicator(close=df['close'], window=periode).rsi().iloc[-1]
        return round(float(val), 2) if not pd.isna(val) else 50
    except:
        return 50

def verifier_volume(df):
    volumes = df['volume'].tolist()
    if len(volumes) < 20:
        return True, 0
    moyenne_24h = sum(volumes[-24:]) / len(volumes[-24:])
    ratio = volumes[-1] / moyenne_24h if moyenne_24h > 0 else 0
    return ratio >= VOLUME_MINI, round(ratio * 100, 1)

def analyser_marche(symbole):
    df = get_klines(symbole, limite=100)
    if df is None or len(df) < 30:
        return "NEUTRE", {}
    adx = calculer_adx(df)
    atr = calculer_atr(df)
    rsi = calculer_rsi(df)
    if atr <= 0:
        return "NEUTRE", {}
    volume_ok, volume_ratio = verifier_volume(df)
    if not volume_ok:
        log.info(f"  {symbole} : Volume {volume_ratio}% < {VOLUME_MINI*100}% -> skip")
        return "NEUTRE", {}
    prix = df['close'].iloc[-1]
    atr_pct = (atr / prix) * 100
    if adx > ADX_MAX:
        log.info(f"  {symbole} : ADX {adx} > {ADX_MAX} -> skip")
        return "NEUTRE", {}
    details = {"adx": adx, "atr": atr, "rsi": rsi, "atr_pct": atr_pct, "volume_ratio": volume_ratio, "df": df}
    if rsi < RSI_ACHAT:
        log.info(f"  {symbole} : RSI {rsi} < {RSI_ACHAT} -> ACHAT")
        return "ACHAT", details
    elif rsi > RSI_VENTE:
        log.info(f"  {symbole} : RSI {rsi} > {RSI_VENTE} -> VENTE")
        return "VENTE", details
    else:
        return "NEUTRE", details

def choisir_meilleur_marche():
    signaux = {}
    for marche in MARCHES:
        direction, details = analyser_marche(marche)
        if direction != "NEUTRE":
            signaux[marche] = {"direction": direction, "details": details}
        time.sleep(0.5)
    if not signaux:
        return None, "NEUTRE", {}
    meilleur = max(signaux.items(), key=lambda x: (abs(x[1]["details"].get("rsi", 50) - 50), x[1]["details"].get("atr_pct", 0)))[0]
    return meilleur, signaux[meilleur]["direction"], signaux[meilleur]["details"]

def simuler_trade(symbole, direction, numero_trade, capital, details):
    prix_entree = get_prix_actuel(symbole)
    if prix_entree is None:
        return "ERREUR", 0
    atr = details.get("atr", 0)
    mise = capital * MISE_FIXE_PCT
    if direction == "ACHAT":
        stop_loss = round(prix_entree - (atr * ATR_MULTIPLIER), 8)
        objectif_partiel = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
        objectif_final = round(prix_entree + (atr * ATR_MULTIPLIER * RATIO_RR), 8)
    else:
        stop_loss = round(prix_entree + (atr * ATR_MULTIPLIER), 8)
        objectif_partiel = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_PARTIEL), 8)
        objectif_final = round(prix_entree - (atr * ATR_MULTIPLIER * RATIO_RR), 8)
    debut = time.time()
    stop_actuel = stop_loss
    meilleur_prix = prix_entree
    dernier_log = 0
    partiel_execute = False
    gain_partiel = 0
    niveau_actuel = 2.50
    while True:
        time.sleep(CHECK_INTERVAL)
        prix_actuel = get_prix_actuel(symbole)
        if prix_actuel is None:
            continue
        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * mise * LEVIER, 2)
        multiplicateur = get_multiplicateur_atr(pnl)
        distance_trailing = atr * multiplicateur
        stop_modifie = False
        if direction == "ACHAT":
            if prix_actuel > meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix - distance_trailing, 8)
            if nouveau_stop > stop_actuel:
                stop_actuel = nouveau_stop
                stop_modifie = True
        else:
            if prix_actuel < meilleur_prix:
                meilleur_prix = prix_actuel
            nouveau_stop = round(meilleur_prix + distance_trailing, 8)
            if nouveau_stop < stop_actuel:
                stop_actuel = nouveau_stop
                stop_modifie = True
        if multiplicateur != niveau_actuel and stop_modifie:
            if direction == "ACHAT":
                gain_protege = round((stop_actuel - prix_entree) / prix_entree * mise * LEVIER, 2)
            else:
                gain_protege = round((prix_entree - stop_actuel) / prix_entree * mise * LEVIER, 2)
            log.info(f"  [TRAILING] PnL {pnl}EUR -> ATRx{multiplicateur} | Stop : {stop_actuel} | Protege : ~{gain_protege}EUR")
            niveau_actuel = multiplicateur
        if direction == "ACHAT":
            atteint_partiel = not partiel_execute and prix_actuel >= objectif_partiel
            atteint_final = prix_actuel >= objectif_final
            atteint_stop = prix_actuel <= stop_actuel
        else:
            atteint_partiel = not partiel_execute and prix_actuel <= objectif_partiel
            atteint_final = prix_actuel <= objectif_final
            atteint_stop = prix_actuel >= stop_actuel
        duree = int((time.time() - debut) / 60)
        if time.time() - dernier_log >= 60:
            log.info(f"  {symbole}: {prix_actuel} | PnL: {pnl}EUR | Stop: {stop_actuel} | {duree}min")
            dernier_log = time.time()
        if atteint_partiel:
            gain_partiel = round(pnl * 0.5, 2)
            partiel_execute = True
            log.info(f"  SORTIE PARTIELLE 50% ! +{gain_partiel}EUR")
            continue
        if atteint_final:
            gain_final = round(pnl * 0.5, 2) if partiel_execute else pnl
            gain_total = round(gain_partiel + gain_final, 2)
            return "GAGNE", gain_total
        if atteint_stop:
            if partiel_execute:
                gain_total = round(gain_partiel + round(pnl * 0.5, 2), 2)
                return ("GAGNE" if gain_total > 0 else "PERDU"), gain_total
            else:
                return "PERDU", pnl
        if time.time() - debut >= TIMEOUT_TRADE:
            gain_total = round(gain_partiel + round(pnl * 0.5, 2), 2) if partiel_execute else pnl
            return ("GAGNE" if gain_total > 0 else "PERDU"), gain_total

def charger_etat():
    if os.path.exists(ETAT_FILE):
        with open(ETAT_FILE, "r") as f:
            return json.load(f)
    return {"capital": CAPITAL_INITIAL, "total_gagne": 0.0, "total_perdu": 0.0, "cumul_net": 0.0, "nb_trades": 0, "nb_wins": 0, "nb_losses": 0, "pertes_consecutives": 0, "pause_until": 0, "historique": []}

def sauvegarder_etat(etat):
    with open(ETAT_FILE, "w") as f:
        json.dump(etat, f, indent=2, ensure_ascii=False)

def afficher_tableau_de_bord(etat):
    win_rate = (etat["nb_wins"] / etat["nb_trades"] * 100) if etat["nb_trades"] > 0 else 0
    perf = ((etat["capital"] - CAPITAL_INITIAL) / CAPITAL_INITIAL * 100)
    log.info(f"  Capital: {round(etat['capital'],2)}EUR ({round(perf,2)}%) | Trades: {etat['nb_trades']} | Wins: {etat['nb_wins']} ({win_rate:.1f}%) | NET: {round(etat['cumul_net'],2)}EUR")

def verifier_kill_switch(etat, capital):
    if capital < CAPITAL_INITIAL * SEUIL_RUINE:
        log.critical(f"SEUIL DE RUINE ! Capital {capital}EUR")
        return "RUINE"
    pause_until = etat.get("pause_until", 0)
    if time.time() < pause_until:
        time.sleep(60)
        return "PAUSE"
    else:
        if pause_until > 0 and etat.get("pertes_consecutives", 0) >= MAX_PERTES_CONSECUTIVES:
            etat["pertes_consecutives"] = 0
            etat["pause_until"] = 0
            sauvegarder_etat(etat)
    if etat["pertes_consecutives"] >= MAX_PERTES_CONSECUTIVES:
        log.warning(f"KILL SWITCH - {MAX_PERTES_CONSECUTIVES} pertes consecutives")
        etat["pause_until"] = int(time.time()) + PAUSE_DUREE
        etat["pertes_consecutives"] = 0
        sauvegarder_etat(etat)
        return "PAUSE"
    return "OK"

def demarrer_bot():
    log.info("BOT MEAN REVERSION V7.2 - DEMARRAGE")
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
                time.sleep(PAUSE)
                continue
            etat["nb_trades"] += 1
            resultat, gain = simuler_trade(symbole, direction, etat["nb_trades"], etat["capital"], details)
            if resultat == "ERREUR":
                etat["nb_trades"] -= 1
                time.sleep(PAUSE)
                continue
            etat["capital"] = round(etat["capital"] + gain, 2)
            etat["cumul_net"] = round(etat["capital"] - CAPITAL_INITIAL, 2)
            if resultat == "GAGNE":
                etat["nb_wins"] += 1
                etat["total_gagne"] = round(etat["total_gagne"] + gain, 2)
                etat["pertes_consecutives"] = 0
            else:
                etat["nb_losses"] += 1
                etat["total_perdu"] = round(etat["total_perdu"] + abs(gain), 2)
                etat["pertes_consecutives"] += 1
            etat["historique"].append({"heure": datetime.now().strftime("%Y-%m-%d %H:%M"), "marche": symbole, "direction": direction, "resultat": resultat, "gain": round(gain, 2), "capital": etat["capital"]})
            sauvegarder_etat(etat)
            afficher_tableau_de_bord(etat)
            time.sleep(PAUSE)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Erreur : {e}")
            time.sleep(60)

if __name__ == "__main__":
    demarrer_bot()
