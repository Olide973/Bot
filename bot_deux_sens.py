#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  BOT DEUX SENS — capture des deux directions (SIMULATION)                  ║
║  Test de l'idée de Damien : à chaque signal, ouvrir LONG **et** SHORT.     ║
╚══════════════════════════════════════════════════════════════════════════╝

L'IDÉE
------
On ne sait pas deviner le sens → on ouvre les deux en même temps sur le même
marché. Le côté perdant est coupé à un petit stop (défaut -1€), le côté gagnant
vise un palier (défaut +3€). Fichier SÉPARÉ pour ne pas toucher au bon bot
adaptatif. Activable via MODE_DEUX_SENS=1 (comme le funding).

CE BOT EST EN SIMULATION — il ne passe AUCUN ordre. Il détecte les vrais
mouvements de prix OKX, "ouvre" deux positions fictives, les gère avec ton stop
et ton palier, et te fait un rapport Telegram. But : te MONTRER, chiffres réels
à l'appui, ce que ça donne.

⚠️ Honnêteté (les maths, déjà expliquées) : les deux côtés s'annulent au départ
(tu paies double frais pour ne pas bouger). Quand un côté est stoppé, il te reste
UNE position sans edge (pile ou face) + une perte de départ. Statistiquement, ça
perd plus qu'un seul trade. Ce bot sert à le VÉRIFIER sans risque, pas à gagner.

DÉPLOIEMENT : mêmes variables que tes autres bots (TELEGRAM_TOKEN,
TELEGRAM_CHAT_ID, DATABASE_URL). RESET_DEUXSENS=1 pour repartir de zéro.
"""

import os
import json
import time
import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
import pg8000

# ═══════════════════════════════════════════════════════════════════════════
#  RÉGLAGES
# ═══════════════════════════════════════════════════════════════════════════
CAPITAL_INITIAL = float(os.environ.get("CAPITAL_DEUXSENS", "416"))

NOTIONNEL_PAR_COTE = 200.0   # notionnel de CHAQUE côté (long et short). 2 côtés = 400€ d'expo.
STOP_EUR   = 1.0             # stop du côté perdant (demande de Damien : 1€)
PALIER_EUR = 3.0             # objectif du côté gagnant (demande de Damien : 3€)
# → avec 200€ de notionnel : stop = 0.50% de mouvement, palier = 1.50%.

SEUIL_MOUVEMENT = 0.005      # 0.5% : mouvement depuis la référence qui déclenche un signal
FRAIS_TAKER     = 0.0005     # 0.05% par patte (comme ton scalper)

PAUSE_LOOP_SEC = 60          # vérifie prix + stops toutes les 60s

CANDIDATS = [
    "ETH", "XRP", "SOL", "ADA", "LINK", "DOGE", "LTC", "TRX", "UNI", "HYPE",
    "AVAX", "NEAR", "AAVE", "SUI", "FIL", "BTC", "ALGO", "INJ", "BNB",
]

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RESET_DEUXSENS   = os.environ.get("RESET_DEUXSENS", "0").strip().lower() in ("1", "true", "oui", "yes")

OKX_PUBLIC = "https://www.okx.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("deuxsens")

OKX_SYMBOLS = {}   # { "AAVEUSD": "AAVE-USD_UM_XPERP-..." }


# ═══════════════════════════════════════════════════════════════════════════
#  PERSISTANCE POSTGRESQL (table séparée `deuxsens_etat` → aucun conflit)
# ═══════════════════════════════════════════════════════════════════════════
def _connexion():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL absente")
    p = urlparse(url)
    derniere = None
    for _ in range(4):
        try:
            return pg8000.connect(user=p.username, password=p.password, host=p.hostname,
                                  port=p.port or 5432, database=(p.path or "/postgres").lstrip("/"))
        except Exception as e:
            derniere = e
            time.sleep(2)
    raise RuntimeError(f"Connexion PostgreSQL impossible : {derniere}")

def init_db():
    conn = _connexion()
    try:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS deuxsens_etat (
                         id INTEGER PRIMARY KEY DEFAULT 1, data TEXT NOT NULL,
                         maj_le TIMESTAMP DEFAULT NOW())""")
        conn.commit()
    finally:
        conn.close()

def charger_etat():
    conn = _connexion()
    try:
        cur = conn.cursor()
        cur.execute("SELECT data FROM deuxsens_etat WHERE id = 1")
        row = cur.fetchone()
        if not row:
            return {}
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]
    finally:
        conn.close()

def sauvegarder_etat(etat):
    try:
        conn = _connexion()
        try:
            cur = conn.cursor()
            cur.execute("""INSERT INTO deuxsens_etat (id, data, maj_le) VALUES (1, %s, NOW())
                           ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, maj_le = NOW()""",
                        (json.dumps(etat),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"[DB] échec sauvegarde : {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════
async def telegram(session, message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(message)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with session.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                                           "parse_mode": "HTML"},
                                timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                log.warning(f"Telegram {r.status} : {await r.text()}")
    except Exception as e:
        log.error(f"Erreur Telegram : {e}")


# ═══════════════════════════════════════════════════════════════════════════
#  OKX — instruments + prix
# ═══════════════════════════════════════════════════════════════════════════
async def resoudre_instruments(session):
    global OKX_SYMBOLS
    try:
        async with session.get(f"{OKX_PUBLIC}/api/v5/public/instruments",
                               params={"instType": "FUTURES"},
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
        if data.get("code") != "0":
            return
        xperps = {}
        for inst in data.get("data", []):
            if inst.get("ruleType") != "xperp":
                continue
            base = inst.get("instId", "").split("-")[0]
            if base:
                xperps[base] = inst["instId"]
        for base in CANDIDATS:
            if base in xperps:
                OKX_SYMBOLS[f"{base}USD"] = xperps[base]
        log.info(f"Instruments résolus : {len(OKX_SYMBOLS)}")
    except Exception as e:
        log.error(f"Résolution instruments : {e}")

async def prix(session, inst_id):
    try:
        async with session.get(f"{OKX_PUBLIC}/api/v5/market/ticker",
                               params={"instId": inst_id},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
        if data.get("code") == "0" and data.get("data"):
            last = data["data"][0].get("last", "")
            return float(last) if last else None
    except Exception:
        return None
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  BOUCLE
# ═══════════════════════════════════════════════════════════════════════════
async def boucle(session, etat):
    stop_pct   = STOP_EUR / NOTIONNEL_PAR_COTE      # ex 1/200 = 0.5%
    palier_pct = PALIER_EUR / NOTIONNEL_PAR_COTE    # ex 3/200 = 1.5%
    frais_1_cote = round(NOTIONNEL_PAR_COTE * FRAIS_TAKER, 4)

    while True:
        try:
            refs   = etat.setdefault("references", {})       # prix de référence par marché
            trades = etat.setdefault("trades", {})           # trades doubles ouverts par marché

            for m, inst in OKX_SYMBOLS.items():
                p = await prix(session, inst)
                await asyncio.sleep(0.1)
                if p is None:
                    continue

                # Initialise la référence au premier passage
                if m not in refs:
                    refs[m] = p
                    continue

                # ── Marché avec un trade double ouvert : on gère les 2 côtés ──
                if m in trades:
                    t = trades[m]
                    entree = t["entree"]
                    # P&L de chaque côté (en €), delta-neutre au départ
                    pnl_long  = (p - entree) / entree * NOTIONNEL_PAR_COTE
                    pnl_short = (entree - p) / entree * NOTIONNEL_PAR_COTE

                    for cote, pnl in (("long", pnl_long), ("short", pnl_short)):
                        if t[cote]["ouvert"]:
                            if pnl <= -STOP_EUR:
                                t[cote] = {"ouvert": False, "resultat": round(-STOP_EUR - frais_1_cote, 4), "motif": "STOP"}
                            elif pnl >= PALIER_EUR:
                                t[cote] = {"ouvert": False, "resultat": round(PALIER_EUR - frais_1_cote, 4), "motif": "PALIER"}

                    # Les 2 côtés fermés → on clôture le trade double
                    if not t["long"]["ouvert"] and not t["short"]["ouvert"]:
                        net = round(t["long"]["resultat"] + t["short"]["resultat"], 4)
                        etat["capital"] = round(etat.get("capital", CAPITAL_INITIAL) + net, 4)
                        etat["total_net"] = round(etat.get("total_net", 0) + net, 4)
                        etat["nb_trades"] = etat.get("nb_trades", 0) + 1
                        if net > 0:
                            etat["nb_gagnants"] = etat.get("nb_gagnants", 0) + 1
                        etat.setdefault("historique", []).append(
                            {"marche": m, "net": net, "long": t["long"], "short": t["short"],
                             "ferme_le": datetime.now(timezone.utc).isoformat()})
                        icone = "✅" if net > 0 else "❌"
                        await telegram(session,
                            f"{icone} <b>TRADE DOUBLE FERMÉ — {m.replace('USD','')}</b>\n"
                            f"Long : {t['long']['resultat']:+.2f}€ ({t['long']['motif']})\n"
                            f"Short : {t['short']['resultat']:+.2f}€ ({t['short']['motif']})\n"
                            f"<b>Net : {net:+.2f}€</b> | Capital : {etat['capital']:.2f}€")
                        trades.pop(m)
                        refs[m] = p   # nouvelle référence pour repartir
                    continue

                # ── Pas de trade ouvert : détection d'un signal (mouvement ≥ seuil) ──
                variation = abs(p - refs[m]) / refs[m]
                if variation >= SEUIL_MOUVEMENT:
                    trades[m] = {
                        "entree": p,
                        "ouvert_le": datetime.now(timezone.utc).isoformat(),
                        "long":  {"ouvert": True, "resultat": 0.0, "motif": ""},
                        "short": {"ouvert": True, "resultat": 0.0, "motif": ""},
                    }
                    etat["capital"] = round(etat.get("capital", CAPITAL_INITIAL) - 2 * frais_1_cote, 4)
                    await telegram(session,
                        f"🔀 <b>DOUBLE POSITION — {m.replace('USD','')}</b>\n"
                        f"Mouvement {variation*100:.2f}% détecté → LONG + SHORT ouverts @ {p}\n"
                        f"Notionnel {NOTIONNEL_PAR_COTE:.0f}€/côté | stop -{STOP_EUR:.0f}€ | palier +{PALIER_EUR:.0f}€\n"
                        f"Frais d'ouverture (2 côtés) : -{2*frais_1_cote:.3f}€")

            # ── Bilan quotidien à 22h UTC (19h Guyane) ──
            now = datetime.now(timezone.utc)
            cle = now.strftime("%Y-%m-%d")
            if now.hour == 22 and etat.get("dernier_rapport") != cle:
                etat["dernier_rapport"] = cle
                nb = etat.get("nb_trades", 0)
                wr = (etat.get("nb_gagnants", 0) / nb * 100) if nb else 0
                await telegram(session,
                    f"📊 <b>BILAN DEUX SENS — {cle}</b>\n"
                    f"Trades doubles : {nb} | Gagnants : {wr:.0f}%\n"
                    f"<b>Résultat net global : {etat.get('total_net',0):+.2f}€</b>\n"
                    f"Capital : {etat.get('capital',CAPITAL_INITIAL):.2f}€ (départ {CAPITAL_INITIAL:.0f}€)")

            sauvegarder_etat(etat)
        except Exception as e:
            log.error(f"Boucle : {e}")

        # Battement de cœur (comme le bot funding)
        now = datetime.now(timezone.utc)
        net = round(etat.get("capital", CAPITAL_INITIAL) - CAPITAL_INITIAL, 3)
        ouverts = ", ".join(k.replace("USD", "") for k in etat.get("trades", {})) or "aucun"
        log.info(f"  ❤️ [{now:%H:%M} UTC] Deux-sens actif — trades ouverts: {ouverts} | "
                 f"net {net:+.3f}€ | {etat.get('nb_trades',0)} trades clôturés")

        await asyncio.sleep(PAUSE_LOOP_SEC)


# ═══════════════════════════════════════════════════════════════════════════
#  DÉMARRAGE
# ═══════════════════════════════════════════════════════════════════════════
async def main():
    log.info("=" * 60)
    log.info("  BOT DEUX SENS — capture des deux directions (SIMULATION)")
    log.info("=" * 60)
    init_db()
    etat = {} if RESET_DEUXSENS else (charger_etat() or {})
    etat.setdefault("capital", CAPITAL_INITIAL)
    etat.setdefault("trades", {})
    etat.setdefault("references", {})
    etat.setdefault("total_net", 0.0)
    etat.setdefault("nb_trades", 0)
    etat.setdefault("nb_gagnants", 0)

    async with aiohttp.ClientSession() as session:
        await resoudre_instruments(session)
        await telegram(session,
            (f"🔄 <b>RESET DEUX SENS</b> — repart de zéro.\n\n" if RESET_DEUXSENS else "")
            + f"🔀 <b>BOT DEUX SENS DÉMARRÉ</b> (simulation)\n"
            f"Capital : {etat['capital']:.2f}€\n"
            f"À chaque signal (mouvement ≥ {SEUIL_MOUVEMENT*100:.1f}%) : LONG + SHORT\n"
            f"Notionnel {NOTIONNEL_PAR_COTE:.0f}€/côté | stop -{STOP_EUR:.0f}€ | palier +{PALIER_EUR:.0f}€\n"
            f"(stop = {STOP_EUR/NOTIONNEL_PAR_COTE*100:.2f}% | palier = {PALIER_EUR/NOTIONNEL_PAR_COTE*100:.2f}% de mouvement)\n"
            f"Marchés suivis : {len(OKX_SYMBOLS)}")
        await boucle(session, etat)


if __name__ == "__main__":
    asyncio.run(main())
