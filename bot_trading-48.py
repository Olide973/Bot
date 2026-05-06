"""
╔══════════════════════════════════════════════════════════════╗
║       BOT SCALPING V3 — VERSION OPTIMISEE COMPLETE          ║
║       ADX → Volume → MA → RSI (assoupli)                    ║
║       Mise 50EUR | +0.75EUR | -1.50EUR | 15 minutes         ║
║       12 marchés pour plus de signaux                        ║
╚══════════════════════════════════════════════════════════════╝
"""

import requests
import json
import time
import os
from datetime import datetime

# ══════════════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════════════

MISE         = 50.0
LEVIER       = 3
GAIN_CIBLE   = 0.75    # +0.75EUR
STOP_LOSS    = -25.0   # -25.0EUR
PAUSE        = 120     # 2 minutes entre trades
SCORE_MIN    = 10      # Score minimum 10/30
ADX_RANGE    = 20      # ADX < 20 = range = pas de trade
ADX_TREND    = 25      # ADX > 25 = tendance forte = bonus
VOLUME_MINI  = 0.50    # Volume > 50% de la moyenne 24h

# Filtre RSI/MA assoupli de moitié
RSI_MAX_ACHAT = 75     # ACHAT bloqué si RSI > 75 (au lieu de 65)
RSI_MIN_VENTE = 25     # VENTE bloquée si RSI < 25 (au lieu de 35)

MARCHES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
    "LINKUSDT", "XRPUSDT", "AVAXUSDT", "DOGEUSDT",
    "ADAUSDT", "DOTUSDT", "ATOMUSDT", "NEARUSDT"
]

KRAKEN_SYMBOLS = {
    "BTCUSDT":  "XXBTZUSD",
    "ETHUSDT":  "XETHZUSD",
    "SOLUSDT":  "SOLUSD",
    "XRPUSDT":  "XRPUSD",
    "AVAXUSDT": "AVAXUSD",
    "BNBUSDT":  "BNBUSD",
    "LINKUSDT": "LINKUSD",
    "DOGEUSDT": "XDGUSD",
    "ADAUSDT":  "ADAUSD",
    "DOTUSDT":  "DOTUSD",
    "ATOMUSDT": "ATOMUSD",
    "NEARUSDT": "NEARUSD"
}

print("=" * 55)
print("  BOT SCALPING V3 — VERSION OPTIMISEE COMPLETE")
print(f"  Mise      : {MISE}EUR | Levier : x{LEVIER}")
print(f"  Objectif  : +{GAIN_CIBLE}EUR | Stop : {STOP_LOSS}EUR")
print(f"  Bougies   : 15 minutes")
print(f"  Ordre     : ADX → Volume → MA → RSI")
print(f"  Marches   : {len(MARCHES)} cryptos")
print(f"  RSI filtre: ACHAT < {RSI_MAX_ACHAT} | VENTE > {RSI_MIN_VENTE}")
print("=" * 55)

# ══════════════════════════════════════════════════════════════
# RÉCUPÉRATION DES DONNÉES VIA KRAKEN
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
        key = list(result.keys())[0]
        return float(result[key]["c"][0])
    except Exception as e:
        print(f"  Erreur prix {symbole} : {e}")
        return None

def get_klines(symbole, limite=100):
    kraken_symbol = KRAKEN_SYMBOLS.get(symbole, symbole)
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": kraken_symbol, "interval": 15}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        errors = data.get("error", [])
        if errors:
            print(f"  Erreur klines {symbole} : {errors}")
            return None, None, None, None
        result = data.get("result", {})
        keys = [k for k in result.keys() if k != "last"]
        if not keys:
            return None, None, None, None
        candles = result[keys[0]]
        closes  = [float(k[4]) for k in candles]
        highs   = [float(k[2]) for k in candles]
        lows    = [float(k[3]) for k in candles]
        volumes = [float(k[6]) for k in candles]
        return closes[-limite:], highs[-limite:], lows[-limite:], volumes[-limite:]
    except Exception as e:
        print(f"  Erreur klines {symbole} : {e}")
        return None, None, None, None

# ══════════════════════════════════════════════════════════════
# FILTRE 1 — ADX
# ══════════════════════════════════════════════════════════════

def calculer_adx(highs, lows, closes, periode=14):
    if len(closes) < periode * 2:
        return 0
    tr_list, plus_dm, minus_dm = [], [], []
    for i in range(1, len(closes)):
        high_diff = highs[i] - highs[i-1]
        low_diff  = lows[i-1] - lows[i]
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
        plus_dm.append(high_diff if high_diff > low_diff and high_diff > 0 else 0)
        minus_dm.append(low_diff if low_diff > high_diff and low_diff > 0 else 0)

    def smooth(data, p):
        result = [sum(data[:p])]
        for i in range(p, len(data)):
            result.append(result[-1] - result[-1]/p + data[i])
        return result

    atr  = smooth(tr_list, periode)
    pdi  = smooth(plus_dm, periode)
    mdi  = smooth(minus_dm, periode)

    dx_list = []
    for i in range(len(atr)):
        if atr[i] == 0:
            continue
        pdi_val = 100 * pdi[i] / atr[i]
        mdi_val = 100 * mdi[i] / atr[i]
        if pdi_val + mdi_val == 0:
            continue
        dx = 100 * abs(pdi_val - mdi_val) / (pdi_val + mdi_val)
        dx_list.append(dx)

    if not dx_list:
        return 0
    return round(sum(dx_list[-periode:]) / periode, 2)

# ══════════════════════════════════════════════════════════════
# FILTRE 2 — VOLUME
# ══════════════════════════════════════════════════════════════

def verifier_volume(volumes):
    if len(volumes) < 10:
        return True, 0
    moyenne_24h   = sum(volumes[-96:]) / len(volumes[-96:])
    volume_recent = sum(volumes[-4:]) / 4
    ratio = volume_recent / moyenne_24h if moyenne_24h > 0 else 0
    suffisant = ratio >= VOLUME_MINI
    return suffisant, round(ratio * 100, 1)

# ══════════════════════════════════════════════════════════════
# FILTRE 3 — MOYENNE MOBILE (direction)
# ══════════════════════════════════════════════════════════════

def calculer_ma(closes, periode):
    if len(closes) < periode:
        return None
    return sum(closes[-periode:]) / periode

def scorer_ma(closes):
    ma_courte = calculer_ma(closes, 10)
    ma_longue = calculer_ma(closes, 30)
    if ma_courte is None or ma_longue is None:
        return 0, "NEUTRE"
    ecart = abs(ma_courte - ma_longue) / ma_longue * 100
    direction = "ACHAT" if ma_courte > ma_longue else "VENTE"
    if ecart > 2:     return 10, direction
    elif ecart > 1:   return 7,  direction
    elif ecart > 0.5: return 4,  direction
    else:             return 1,  direction

# ══════════════════════════════════════════════════════════════
# FILTRE 4 — RSI (timing confirmé par MA - assoupli)
# ══════════════════════════════════════════════════════════════

def calculer_rsi(closes, periode=14):
    if len(closes) < periode + 1:
        return 50
    gains, pertes = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        pertes.append(abs(min(diff, 0)))
    moy_gain  = sum(gains[-periode:]) / periode
    moy_perte = sum(pertes[-periode:]) / periode
    if moy_perte == 0:
        return 100
    return round(100 - (100 / (1 + moy_gain / moy_perte)), 2)

def scorer_rsi(rsi):
    if rsi < 25:   return 10, "ACHAT"
    elif rsi < 30: return 8,  "ACHAT"
    elif rsi < 40: return 5,  "ACHAT"
    elif rsi > 75: return 10, "VENTE"
    elif rsi > 70: return 8,  "VENTE"
    elif rsi > 60: return 5,  "VENTE"
    else:          return 2,  "NEUTRE"

# ══════════════════════════════════════════════════════════════
# ANALYSE COMPLÈTE — ADX → Volume → MA → RSI
# ══════════════════════════════════════════════════════════════

def analyser_marche(symbole):
    closes, highs, lows, volumes = get_klines(symbole)
    if closes is None:
        print(f"  {symbole} : Erreur données")
        return 0, "NEUTRE", {}

    # ── FILTRE 1 : ADX ──
    adx = calculer_adx(highs, lows, closes)
    if adx < ADX_RANGE:
        print(f"  {symbole} : ADX {adx} < {ADX_RANGE} → RANGE → pas de trade")
        return 0, "NEUTRE", {"adx": adx}

    # ── FILTRE 2 : VOLUME ──
    volume_ok, volume_ratio = verifier_volume(volumes)
    if not volume_ok:
        print(f"  {symbole} : Volume {volume_ratio}% < 50% → pas de trade")
        return 0, "NEUTRE", {"adx": adx, "volume_ratio": volume_ratio}

    # ── FILTRE 3 : MA (direction) ──
    score_ma, direction_ma = scorer_ma(closes)

    # ── FILTRE 4 : RSI (timing confirmé par MA - assoupli) ──
    rsi = calculer_rsi(closes)
    score_rsi, direction_rsi = scorer_rsi(rsi)

    # Volatilité
    if len(highs) >= 14:
        amplitudes = [(highs[i] - lows[i]) / closes[i] * 100 for i in range(-14, 0)]
        volatilite = round(sum(amplitudes) / len(amplitudes), 2)
    else:
        volatilite = 0

    if volatilite > 3:     score_vol = 10
    elif volatilite > 2:   score_vol = 8
    elif volatilite > 1:   score_vol = 5
    elif volatilite > 0.5: score_vol = 3
    else:                  score_vol = 1

    # ── DIRECTION FINALE ──
    if direction_ma != "NEUTRE":
        direction_finale = direction_ma

        # Filtre RSI/MA assoupli
        if direction_ma == "ACHAT" and rsi > RSI_MAX_ACHAT:
            print(f"  {symbole} : RSI {rsi} > {RSI_MAX_ACHAT} pour ACHAT → ignore")
            return 0, "NEUTRE", {"rsi": rsi, "adx": adx, "score_total": 0, "volatilite": volatilite, "direction": "NEUTRE"}
        elif direction_ma == "VENTE" and rsi < RSI_MIN_VENTE:
            print(f"  {symbole} : RSI {rsi} < {RSI_MIN_VENTE} pour VENTE → ignore")
            return 0, "NEUTRE", {"rsi": rsi, "adx": adx, "score_total": 0, "volatilite": volatilite, "direction": "NEUTRE"}

        if direction_rsi == direction_ma:
            score_total = score_ma + score_rsi + score_vol
        else:
            score_total = score_ma + score_vol

    elif direction_rsi != "NEUTRE":
        direction_finale = direction_rsi
        score_total = score_rsi + score_vol
    else:
        direction_finale = "NEUTRE"
        score_total = 0

    if adx > ADX_TREND:
        score_total = min(score_total + 3, 30)

    score_total = min(score_total, 30)

    print(f"  {symbole} : ADX {adx} | Vol {volume_ratio}% | "
          f"RSI {rsi} ({direction_rsi}) | MA ({direction_ma}) | "
          f"Volatilite {volatilite}% | Score {score_total}/30 | {direction_finale}")

    return score_total, direction_finale, {
        "adx": adx,
        "volume_ratio": volume_ratio,
        "rsi": rsi,
        "volatilite": volatilite,
        "score_total": score_total,
        "direction": direction_finale
    }

def choisir_meilleur_marche():
    print(f"\n  [{datetime.now().strftime('%H:%M:%S')}] Analyse des marches...")
    resultats = {}

    for marche in MARCHES:
        score, direction, details = analyser_marche(marche)
        resultats[marche] = {"score": score, "direction": direction, "details": details}
        time.sleep(1)

    valides = {k: v for k, v in resultats.items()
               if v["direction"] != "NEUTRE" and v["score"] >= SCORE_MIN}

    if not valides:
        print("  => Aucun signal valide. On attend...")
        return None, "NEUTRE", {}

    meilleur = max(valides, key=lambda x: (
        valides[x]["score"],
        valides[x]["details"].get("volatilite", 0)
    ))

    direction = valides[meilleur]["direction"]
    score     = valides[meilleur]["score"]
    vol       = valides[meilleur]["details"].get("volatilite", 0)
    adx       = valides[meilleur]["details"].get("adx", 0)

    print(f"\n  => CHOIX : {meilleur} ({direction})")
    print(f"     Score {score}/30 | ADX {adx} | Vol {vol}%")
    return meilleur, direction, valides[meilleur]["details"]

# ══════════════════════════════════════════════════════════════
# SIMULATION DU TRADE
# ══════════════════════════════════════════════════════════════

def simuler_trade(symbole, direction, numero_trade):
    prix_entree = get_prix_actuel(symbole)
    if prix_entree is None:
        return "ERREUR", 0

    pct_gain = GAIN_CIBLE / (MISE * LEVIER)
    pct_stop = abs(STOP_LOSS) / (MISE * LEVIER)

    if direction == "ACHAT":
        prix_objectif  = round(prix_entree * (1 + pct_gain), 6)
        prix_stop_loss = round(prix_entree * (1 - pct_stop), 6)
    else:
        prix_objectif  = round(prix_entree * (1 - pct_gain), 6)
        prix_stop_loss = round(prix_entree * (1 + pct_stop), 6)

    print(f"\n  {'='*50}")
    print(f"  TRADE #{numero_trade} — {datetime.now().strftime('%H:%M:%S')}")
    print(f"  {'='*50}")
    print(f"  Symbole    : {symbole} ({direction})")
    print(f"  Prix entree: {prix_entree}")
    print(f"  Objectif   : {prix_objectif} -> +{GAIN_CIBLE}EUR")
    print(f"  Stop-Loss  : {prix_stop_loss} -> {STOP_LOSS}EUR")
    print(f"  Mouvement  : {round(pct_gain*100, 3)}%\n")

    debut = time.time()

    while True:
        time.sleep(30)

        prix_actuel = get_prix_actuel(symbole)
        if prix_actuel is None:
            continue

        if direction == "ACHAT":
            pnl = round((prix_actuel - prix_entree) / prix_entree * MISE * LEVIER, 2)
        else:
            pnl = round((prix_entree - prix_actuel) / prix_entree * MISE * LEVIER, 2)

        heure = datetime.now().strftime("%H:%M:%S")
        duree = int((time.time() - debut) / 60)
        print(f"  [{heure}] {symbole}: {prix_actuel} | "
              f"PnL: {'+' if pnl >= 0 else ''}{pnl}EUR | {duree}min")

        if pnl >= GAIN_CIBLE:
            print(f"\n  OBJECTIF ATTEINT ! +{pnl}EUR")
            return "GAGNE", pnl

        if pnl <= STOP_LOSS:
            print(f"\n  STOP-LOSS ATTEINT ! {pnl}EUR")
            return "PERDU", pnl

        if time.time() - debut > 86400:
            print(f"\n  TIMEOUT 24H — Fermeture : {'+' if pnl >= 0 else ''}{pnl}EUR")
            return ("GAGNE" if pnl > 0 else "PERDU"), pnl

# ══════════════════════════════════════════════════════════════
# GESTION DE L'ÉTAT
# ══════════════════════════════════════════════════════════════

def charger_etat():
    if os.path.exists("etat_bot.json"):
        with open("etat_bot.json", "r") as f:
            return json.load(f)
    return {
        "total_gagne": 0.0, "total_perdu": 0.0,
        "cumul_net": 0.0, "nb_trades": 0,
        "nb_wins": 0, "nb_losses": 0,
        "nb_skips": 0, "historique": []
    }

def sauvegarder_etat(etat):
    with open("etat_bot.json", "w") as f:
        json.dump(etat, f, indent=2, ensure_ascii=False)

def afficher_tableau_de_bord(etat):
    win_rate = (etat["nb_wins"] / etat["nb_trades"] * 100) if etat["nb_trades"] > 0 else 0
    print(f"\n  {'='*55}")
    print(f"  TABLEAU DE BORD")
    print(f"  {'='*55}")
    print(f"  Trades total  : {etat['nb_trades']}")
    print(f"  Victoires     : {etat['nb_wins']} ({win_rate:.1f}%)")
    print(f"  Defaites      : {etat['nb_losses']}")
    print(f"  Signaux sautes: {etat['nb_skips']}")
    print(f"  Total gagne   : +{round(etat['total_gagne'], 2)}EUR")
    print(f"  Total perdu   : -{round(etat['total_perdu'], 2)}EUR")
    print(f"  BENEFICE NET  : {'+' if etat['cumul_net'] >= 0 else ''}{round(etat['cumul_net'], 2)}EUR")
    if etat["historique"]:
        print(f"\n  Derniers trades :")
        for h in etat["historique"][-5:]:
            icone = "OK" if h["resultat"] == "GAGNE" else "XX"
            print(f"    [{icone}] {h['heure']} | {h['marche']} | "
                  f"{h['direction']} | {h['resultat']} | "
                  f"{'+' if h['gain'] >= 0 else ''}{h['gain']}EUR | "
                  f"Cumul: {'+' if h['cumul'] >= 0 else ''}{h['cumul']}EUR")
    print(f"  {'='*55}")

# ══════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════════════

def demarrer_bot():
    print(f"\n  DEMARRAGE — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    etat = charger_etat()
    afficher_tableau_de_bord(etat)

    while True:
        try:
            symbole, direction, details = choisir_meilleur_marche()

            if direction == "NEUTRE" or symbole is None:
                etat["nb_skips"] += 1
                sauvegarder_etat(etat)
                print(f"  Nouvelle analyse dans 2 minutes...")
                time.sleep(PAUSE)
                continue

            etat["nb_trades"] += 1
            resultat, gain = simuler_trade(symbole, direction, etat["nb_trades"])

            if resultat == "ERREUR":
                etat["nb_trades"] -= 1
                print("  Erreur. Nouvelle tentative dans 2 minutes...")
                time.sleep(PAUSE)
                continue

            if resultat == "GAGNE":
                etat["nb_wins"]     += 1
                etat["total_gagne"]  = round(etat["total_gagne"] + gain, 2)
            else:
                etat["nb_losses"]   += 1
                etat["total_perdu"]  = round(etat["total_perdu"] + abs(gain), 2)

            etat["cumul_net"] = round(etat["total_gagne"] - etat["total_perdu"], 2)
            etat["historique"].append({
                "heure":     datetime.now().strftime("%Y-%m-%d %H:%M"),
                "marche":    symbole,
                "direction": direction,
                "resultat":  resultat,
                "gain":      round(gain, 2),
                "cumul":     etat["cumul_net"]
            })
            sauvegarder_etat(etat)
            afficher_tableau_de_bord(etat)

            print(f"\n  Pause de 2 minutes avant le prochain trade...")
            time.sleep(PAUSE)

        except KeyboardInterrupt:
            print("\n  Bot arrete.")
            break
        except Exception as e:
            print(f"\n  Erreur : {e}")
            time.sleep(60)

if __name__ == "__main__":
    demarrer_bot()
