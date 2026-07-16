#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  BOT MOMENTUM — suivi des gros mouvements (SIMULATION)                     ║
║  On ne trade PAS le bruit. On suit une impulsion déjà lancée.             ║
╚══════════════════════════════════════════════════════════════════════════╝

L'IDÉE (différente du scalper et du deux-sens)
----------------------------------------------
Les micro-mouvements de 0.5% = du bruit, imprévisibles (prouvé). Ici on ignore
tout ça. On n'ouvre QUE quand un marché a fait un GROS mouvement récent
(défaut ≥ 1.5% en 10 min) — signe d'une vraie impulsion. On entre DANS LE SENS
de l'impulsion (ça monte fort → on achète ; ça chute fort → on vend), on coupe
vite si ça casse, et on laisse COURIR loin si ça continue (échelle de paliers +
plancher trailing). C'est du suivi de tendance : peu de trades, mais on cherche
à attraper les vrais mouvements.

⚠️ HONNÊTETÉ : le suivi de tendance affronte les « fausses cassures » (le prix
part fort puis se retourne) et les frais. Je penche pour que ça perde à cette
échelle/timeframe — mais c'est une vraie stratégie, pas encore testée ici, donc
la simu tranchera. Zéro argent réel.

DÉPLOIEMENT : mêmes variables (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL).
RESET_MOMENTUM=1 pour repartir de zéro.
"""

import os
import json
import time
import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
import pg8000

# ═══════════════════════════════════════════════════════════════════════════
#  RÉGLAGES
# ═══════════════════════════════════════════════════════════════════════════
CAPITAL_INITIAL = float(os.environ.get("CAPITAL_MOMENTUM", "416"))

NOTIONNEL = 300.0            # notionnel d'une position (une seule direction à la fois)

# Détection d'impulsion : gros mouvement sur une fenêtre courte
FENETRE_SEC     = 600        # fenêtre d'observation : 10 minutes
SEUIL_BREAKOUT  = 0.015      # 1.5% de mouvement sur la fenêtre = impulsion → on entre

# Sortie
STOP_PCT = 0.006            # stop : 0.60% (= 1.80€ à 300€) — on coupe vite si ça casse
# Échelle de paliers pour laisser COURIR le gagnant : 1%, 2%, 3%, 4%… (= 3€, 6€, 9€…)
# Plancher trailing = palier précédent (au 1er palier : point mort / breakeven).
PALIER_1_PCT   = 0.010
PALIER_PAS_PCT = 0.010
NB_PALIERS     = 20
PALIERS_PCT = [round(PALIER_1_PCT + i * PALIER_PAS_PCT, 6) for i in range(NB_PALIERS)]

FRAIS_TAKER    = 0.0005      # 0.05% par patte
PAUSE_LOOP_SEC = 60

CANDIDATS = [
    "ETH", "XRP", "SOL", "ADA", "LINK", "DOGE", "LTC", "TRX", "UNI", "HYPE",
    "AVAX", "NEAR", "AAVE", "SUI", "FIL", "BTC", "ALGO", "INJ", "BNB",
]

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RESET_MOMENTUM   = os.environ.get("RESET_MOMENTUM", "0").strip().lower() in ("1", "true", "oui", "yes")

OKX_PUBLIC = "https://www.okx.com"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("momentum")

OKX_SYMBOLS = {}
FENETRES = {}   # {marche: deque[(ts, prix)]} — en mémoire, se reconstruit au besoin


# ═══════════════════════════════════════════════════════════════════════════
#  PERSISTANCE POSTGRESQL (table séparée `momentum_etat`)
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
        cur.execute("""CREATE TABLE IF NOT EXISTS momentum_etat (
                         id INTEGER PRIMARY KEY DEFAULT 1, data TEXT NOT NULL,
                         maj_le TIMESTAMP DEFAULT NOW())""")
        conn.commit()
    finally:
        conn.close()

def charger_etat():
    conn = _connexion()
    try:
        cur = conn.cursor()
        cur.execute("SELECT data FROM momentum_etat WHERE id = 1")
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
            cur.execute("""INSERT INTO momentum_etat (id, data, maj_le) VALUES (1, %s, NOW())
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


def variation_fenetre(m, p_actuel):
    """Met à jour la fenêtre glissante et renvoie (variation, fenetre_pleine)."""
    now = time.time()
    fen = FENETRES.setdefault(m, deque())
    fen.append((now, p_actuel))
    # Purge ce qui est plus vieux que la fenêtre
    while fen and now - fen[0][0] > FENETRE_SEC:
        fen.popleft()
    p_vieux = fen[0][1]
    age = now - fen[0][0]
    var = (p_actuel - p_vieux) / p_vieux if p_vieux else 0.0
    pleine = age >= FENETRE_SEC * 0.7   # fenêtre assez remplie pour être fiable
    return var, pleine


# ═══════════════════════════════════════════════════════════════════════════
#  BOUCLE
# ═══════════════════════════════════════════════════════════════════════════
async def boucle(session, etat):
    frais = round(NOTIONNEL * FRAIS_TAKER, 4)

    while True:
        try:
            positions = etat.setdefault("positions", [])   # 1 position max par marché

            # 1) Prix de tous les marchés + mise à jour des fenêtres
            prix_actuels, variations = {}, {}
            for m, inst in OKX_SYMBOLS.items():
                p = await prix(session, inst)
                await asyncio.sleep(0.1)
                if p is not None:
                    prix_actuels[m] = p
                    variations[m] = variation_fenetre(m, p)

            # 2) GESTION des positions ouvertes (stop + échelle + plancher trailing)
            restantes = []
            for pos in positions:
                p = prix_actuels.get(pos["marche"])
                if p is None:
                    restantes.append(pos)
                    continue
                entree = pos["entree"]
                pct = (p - entree) / entree if pos["sens"] == "long" else (entree - p) / entree
                i = pos.get("palier", -1)
                while i + 1 < len(PALIERS_PCT) and pct >= PALIERS_PCT[i + 1]:
                    i += 1
                pos["palier"] = i

                ferme, resultat, motif = False, 0.0, ""
                if i < 0:
                    if pct <= -STOP_PCT:
                        resultat = round(-STOP_PCT * NOTIONNEL - frais, 4)
                        motif, ferme = "STOP", True
                else:
                    plancher = PALIERS_PCT[i - 1] if i >= 1 else 0.0
                    if pct <= plancher:
                        resultat = round(plancher * NOTIONNEL - frais, 4)
                        motif, ferme = "PALIER", True

                if not ferme:
                    restantes.append(pos)
                    continue

                m = pos["marche"]
                etat["capital"]   = round(etat.get("capital", CAPITAL_INITIAL) + resultat, 4)
                etat["total_net"] = round(etat.get("total_net", 0) + resultat, 4)
                etat["nb_trades"] = etat.get("nb_trades", 0) + 1
                if resultat > 0:
                    etat["nb_gagnants"] = etat.get("nb_gagnants", 0) + 1
                etat.setdefault("historique", []).append({
                    "marche": m, "sens": pos["sens"], "resultat": resultat, "motif": motif,
                    "palier": i, "ferme_le": datetime.now(timezone.utc).isoformat()})
                ic = "✅" if resultat > 0 else "❌"
                sens_txt = "LONG" if pos["sens"] == "long" else "SHORT"
                if motif == "STOP":
                    detail = f"cassure, stoppé à -{STOP_PCT*100:.2f}%"
                elif i >= 1:
                    detail = f"a couru jusqu'au palier {i + 1}, verrouillé au palier {i}"
                else:
                    detail = "1er palier atteint puis retour au point mort"
                await telegram(session,
                    f"{ic} <b>{sens_txt} FERMÉ — {m.replace('USD','')}</b>\n"
                    f"Résultat : <b>{resultat:+.2f}€</b> ({detail})\n"
                    f"Capital : {etat['capital']:.2f}€")

            etat["positions"] = restantes
            positions = restantes

            # 3) DÉTECTION d'impulsion → on entre DANS LE SENS (momentum)
            marches_occupes = {pos["marche"] for pos in positions}
            for m, p in prix_actuels.items():
                if m in marches_occupes:
                    continue
                var, pleine = variations.get(m, (0.0, False))
                if not pleine or abs(var) < SEUIL_BREAKOUT:
                    continue
                sens = "long" if var > 0 else "short"   # on SUIT l'impulsion
                positions.append({
                    "marche": m, "sens": sens, "entree": p,
                    "ouvert_le": datetime.now(timezone.utc).isoformat(), "palier": -1})
                etat["capital"] = round(etat.get("capital", CAPITAL_INITIAL) - frais, 4)
                marches_occupes.add(m)
                fleche = "📈 hausse" if sens == "long" else "📉 baisse"
                await telegram(session,
                    f"🚀 <b>IMPULSION — {m.replace('USD','')}</b>\n"
                    f"{fleche} de {abs(var)*100:.2f}% en {FENETRE_SEC//60} min → on SUIT en "
                    f"<b>{sens.upper()}</b> @ {p}\n"
                    f"Notionnel {NOTIONNEL:.0f}€ | stop -{STOP_PCT*100:.2f}% | "
                    f"1er palier +{PALIERS_PCT[0]*100:.1f}% puis échelle\n"
                    f"Frais d'entrée : -{frais:.3f}€")

            # 4) Bilan quotidien à 22h UTC (19h Guyane)
            now = datetime.now(timezone.utc)
            cle = now.strftime("%Y-%m-%d")
            if now.hour == 22 and etat.get("dernier_rapport") != cle:
                etat["dernier_rapport"] = cle
                await rapport_quotidien(session, etat, cle)

            sauvegarder_etat(etat)
        except Exception as e:
            log.error(f"Boucle : {e}")

        # Battement de cœur
        now = datetime.now(timezone.utc)
        net = round(etat.get("capital", CAPITAL_INITIAL) - CAPITAL_INITIAL, 3)
        po = etat.get("positions", [])
        detail = ", ".join(f"{x['marche'].replace('USD','')}" for x in po) or "aucune"
        log.info(f"  ❤️ [{now:%H:%M} UTC] Momentum actif — {len(po)} position(s) ({detail}) | "
                 f"net {net:+.3f}€ | {etat.get('nb_trades',0)} clôturées")

        await asyncio.sleep(PAUSE_LOOP_SEC)


async def rapport_quotidien(session, etat, cle):
    hist = etat.get("historique", [])
    jour = [h for h in hist if str(h.get("ferme_le", ""))[:10] == cle]

    net_jour = round(sum(h.get("resultat", 0) for h in jour), 2)
    gagnants = sum(1 for h in jour if h.get("resultat", 0) > 0)
    perdants = len(jour) - gagnants
    nb_palier = sum(1 for h in jour if h.get("motif") == "PALIER")
    nb_stop   = sum(1 for h in jour if h.get("motif") == "STOP")
    gain_moy = round(sum(h["resultat"] for h in jour if h["resultat"] > 0) / gagnants, 2) if gagnants else 0
    perte_moy = round(sum(h["resultat"] for h in jour if h["resultat"] <= 0) / perdants, 2) if perdants else 0

    lignes = []
    for h in jour[:40]:
        m = h.get("marche", "?").replace("USD", "")
        sens = "L" if h.get("sens") == "long" else "S"
        ic = "✅" if h.get("resultat", 0) > 0 else "❌"
        sym = "🎯" if h.get("motif") == "PALIER" else "🛑"
        lignes.append(f"{ic} {sens} {m:6s} {h.get('resultat',0):+.2f}€ {sym}")
    if len(jour) > 40:
        lignes.append(f"… et {len(jour)-40} autres")
    detail = "\n".join(lignes) if lignes else "(aucune position clôturée aujourd'hui)"

    po = etat.get("positions", [])
    ouverts = f"{len(po)} ({', '.join(x['marche'].replace('USD','') for x in po)})" if po else "aucune"

    if len(jour) == 0:
        lecture = "Aucune impulsion clôturée aujourd'hui — trop tôt pour juger."
    elif nb_palier == 0:
        lecture = f"Aucune impulsion n'a couru : toutes se sont retournées (stop {nb_stop}×). Fausses cassures."
    elif gagnants and perdants and gain_moy > abs(perte_moy) * (perdants / max(gagnants, 1)):
        lecture = f"Les gagnants ({gain_moy:+.2f}€ moy) couvrent les perdants ({perte_moy:+.2f}€ moy). À confirmer."
    else:
        lecture = f"Trop de fausses cassures : {nb_stop} stops pour {nb_palier} qui ont couru."

    await telegram(session,
        f"📊 <b>BILAN MOMENTUM — {cle}</b>\n"
        f"\n💰 <b>RÉSULTAT DU JOUR</b>\n"
        f"Net : <b>{net_jour:+.2f}€</b>\n"
        f"Capital : {etat.get('capital',CAPITAL_INITIAL):.2f}€ (départ {CAPITAL_INITIAL:.0f}€)\n"
        f"\n📈 <b>IMPULSIONS SUIVIES</b>\n"
        f"Clôturées : {len(jour)} | Gagnantes : {gagnants} | Perdantes : {perdants}\n"
        f"Gain moyen : {gain_moy:+.2f}€ | Perte moyenne : {perte_moy:+.2f}€\n"
        f"\n🎯 <b>LE CHIFFRE CLÉ</b>\n"
        f"Ont couru (palier) : {nb_palier} | Fausses cassures (stop) : {nb_stop}\n"
        f"→ {lecture}\n"
        f"\n📋 <b>DÉTAIL</b> (L=long, S=short)\n<pre>{detail}</pre>\n"
        f"⏳ Encore ouvertes : {ouverts}")


# ═══════════════════════════════════════════════════════════════════════════
#  DÉMARRAGE
# ═══════════════════════════════════════════════════════════════════════════
async def main():
    log.info("=" * 60)
    log.info("  BOT MOMENTUM — suivi des gros mouvements (SIMULATION)")
    log.info("=" * 60)
    init_db()
    etat = {} if RESET_MOMENTUM else (charger_etat() or {})
    etat.setdefault("capital", CAPITAL_INITIAL)
    etat.setdefault("positions", [])
    etat.setdefault("total_net", 0.0)
    etat.setdefault("nb_trades", 0)
    etat.setdefault("nb_gagnants", 0)

    async with aiohttp.ClientSession() as session:
        await resoudre_instruments(session)
        await telegram(session,
            (f"🔄 <b>RESET MOMENTUM</b> — repart de zéro.\n\n" if RESET_MOMENTUM else "")
            + f"🚀 <b>BOT MOMENTUM DÉMARRÉ</b> (simulation)\n"
            f"Capital : {etat['capital']:.2f}€\n"
            f"On IGNORE le bruit. On entre seulement sur une impulsion "
            f"≥ {SEUIL_BREAKOUT*100:.1f}% en {FENETRE_SEC//60} min, DANS son sens.\n"
            f"Notionnel {NOTIONNEL:.0f}€ | stop -{STOP_PCT*100:.2f}% (= -{STOP_PCT*NOTIONNEL:.1f}€)\n"
            f"Paliers : {', '.join(f'{x*100:.0f}%' for x in PALIERS_PCT[:5])}… "
            f"(= {', '.join(f'{x*NOTIONNEL:.0f}€' for x in PALIERS_PCT[:5])}…)\n"
            f"On coupe vite, on laisse courir loin.\n"
            f"⏳ Chauffe ~{FENETRE_SEC//60} min avant les 1ers signaux (remplissage fenêtre).\n"
            f"Marchés suivis : {len(OKX_SYMBOLS)}")
        await boucle(session, etat)


if __name__ == "__main__":
    asyncio.run(main())
