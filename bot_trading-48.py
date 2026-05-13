# =========================
# FICHIER : bot.py
# =========================

import os
import time
import logging
import requests
import pandas as pd

from datetime import datetime

from ta.trend import ADXIndicator
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator

from database import (
    init_database,
    charger_etat,
    sauvegarder_etat,
    enregistrer_trade
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

log = logging.getLogger(__name__)

CAPITAL_INITIAL = 215.0
LEVIER = 10

RSI_ACHAT = 30
RSI_VENTE = 70

ADX_MAX = 40

CHECK_INTERVAL = 10
PAUSE = 120

ATR_MULTIPLIER = 2.5
RATIO_RR = 2.0

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

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


def telegram(message):

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            },
            timeout=10
        )

    except Exception as e:
        log.error(f"Telegram erreur : {e}")


def get_klines(symbole, limite=100):

    try:

        pair = KRAKEN_SYMBOLS.get(symbole, symbole)

        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={
                "pair": pair,
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

    except Exception as e:

        log.error(f"Klines erreur : {e}")

        return None


def get_prix_actuel(symbole):

    try:

        pair = KRAKEN_SYMBOLS.get(symbole, symbole)

        r = requests.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": pair},
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


def analyser_marche(symbole):

    df = get_klines(symbole)

    if df is None or len(df) < 30:
        return "NEUTRE", {}

    try:

        rsi = RSIIndicator(
            close=df['close'],
            window=14
        ).rsi().iloc[-1]

        adx = ADXIndicator(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=14
        ).adx().iloc[-1]

        atr = AverageTrueRange(
            high=df['high'],
            low=df['low'],
            close=df['close'],
            window=14
        ).average_true_range().iloc[-1]

        if adx > ADX_MAX:
            return "NEUTRE", {}

        details = {
            "rsi": round(float(rsi), 2),
            "adx": round(float(adx), 2),
            "atr": round(float(atr), 8)
        }

        if rsi < RSI_ACHAT:
            return "ACHAT", details

        if rsi > RSI_VENTE:
            return "VENTE", details

        return "NEUTRE", {}

    except Exception as e:

        log.error(f"Analyse erreur : {e}")

        return "NEUTRE", {}


def choisir_meilleur_marche():

    signaux = {}

    for symbole in MARCHES:

        direction, details = analyser_marche(symbole)

        if direction != "NEUTRE":

            signaux[symbole] = {
                "direction": direction,
                "details": details
            }

        time.sleep(1)

    if not signaux:
        return None, "NEUTRE", {}

    meilleur = max(
        signaux.items(),
        key=lambda x: abs(x[1]["details"]["rsi"] - 50)
    )

    symbole = meilleur[0]

    return (
        symbole,
        meilleur[1]["direction"],
        meilleur[1]["details"]
    )


def simuler_trade(symbole, direction, capital, details):

    prix_entree = get_prix_actuel(symbole)

    if prix_entree is None:
        return "ERREUR", 0, {}

    atr = details["atr"]

    mise = round(capital * 0.20, 2)

    if direction == "ACHAT":

        stop_loss = prix_entree - atr * ATR_MULTIPLIER

        objectif = prix_entree + atr * ATR_MULTIPLIER * RATIO_RR

    else:

        stop_loss = prix_entree + atr * ATR_MULTIPLIER

        objectif = prix_entree - atr * ATR_MULTIPLIER * RATIO_RR

    debut = time.time()

    while True:

        time.sleep(CHECK_INTERVAL)

        prix = get_prix_actuel(symbole)

        if prix is None:
            continue

        if direction == "ACHAT":

            pnl = (
                (prix - prix_entree)
                / prix_entree
            ) * mise * LEVIER

            if prix <= stop_loss:
                return "PERDU", round(pnl, 2), {
                    "prix_entree": prix_entree,
                    "prix_sortie": prix,
                    "stop_loss": stop_loss,
                    "objectif": objectif
                }

            if prix >= objectif:
                return "GAGNE", round(pnl, 2), {
                    "prix_entree": prix_entree,
                    "prix_sortie": prix,
                    "stop_loss": stop_loss,
                    "objectif": objectif
                }

        else:

            pnl = (
                (prix_entree - prix)
                / prix_entree
            ) * mise * LEVIER

            if prix >= stop_loss:
                return "PERDU", round(pnl, 2), {
                    "prix_entree": prix_entree,
                    "prix_sortie": prix,
                    "stop_loss": stop_loss,
                    "objectif": objectif
                }

            if prix <= objectif:
                return "GAGNE", round(pnl, 2), {
                    "prix_entree": prix_entree,
                    "prix_sortie": prix,
                    "stop_loss": stop_loss,
                    "objectif": objectif
                }

        if time.time() - debut > 3600:
            return "PERDU", round(pnl, 2), {
                "prix_entree": prix_entree,
                "prix_sortie": prix,
                "stop_loss": stop_loss,
                "objectif": objectif
            }


def main():

    log.info("DÉMARRAGE BOT")

    init_database()

    etat = charger_etat()

    telegram("🚀 BOT démarré")

    while True:

        try:

            symbole, direction, details = choisir_meilleur_marche()

            if direction == "NEUTRE":

                log.info("Aucun signal")

                time.sleep(PAUSE)

                continue

            log.info(f"SIGNAL : {symbole} {direction}")

            resultat, gain, infos = simuler_trade(
                symbole,
                direction,
                etat["capital"],
                details
            )

            etat["nb_trades"] += 1

            etat["capital"] = round(
                etat["capital"] + gain,
                2
            )

            etat["cumul_net"] = round(
                etat["capital"] - CAPITAL_INITIAL,
                2
            )

            if resultat == "GAGNE":

                etat["nb_wins"] += 1

                etat["total_gagne"] += gain

            else:

                etat["nb_losses"] += 1

                etat["total_perdu"] += abs(gain)

            enregistrer_trade({
                "marche": symbole,
                "direction": direction,
                "resultat": resultat,
                "prix_entree": infos["prix_entree"],
                "prix_sortie": infos["prix_sortie"],
                "stop_loss": infos["stop_loss"],
                "objectif": infos["objectif"],
                "mise": round(etat["capital"] * 0.20, 2),
                "gain": gain,
                "capital_apres": etat["capital"],
                "duree_minutes": 0,
                "score": None,
                "adx": details["adx"],
                "atr": details["atr"],
                "rsi": details["rsi"]
            })

            sauvegarder_etat(etat)

            log.info(
                f"Résultat : {resultat} | "
                f"Gain : {gain}€ | "
                f"Capital : {etat['capital']}€"
            )

            telegram(
                f"{symbole} {direction}\n"
                f"{resultat}\n"
                f"{gain}€\n"
                f"Capital : {etat['capital']}€"
            )

            time.sleep(PAUSE)

        except Exception as e:

            log.error(f"Erreur globale : {e}")

            time.sleep(60)


if __name__ == "__main__":
    main()
