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

FRAIS_TAKER    = 0.0005      # 0.05% par patte (taker)
GLISSEMENT_PCT = 0.0003      # 0.03% par patte : estimation du glissement (exécution pas au prix pile)
FUNDING_EST_8H = 0.0001      # 0.01%/8h : estimation du funding pour les trades tenus longtemps
                             #   (un SHORT encaisse ~ce taux/8h, un LONG le paie — funding supposé +)
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
def _duree_txt(sec):
    """Formate une durée en texte court : 45s, 12min, 2h05."""
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}min"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}"


async def boucle(session, etat):
    while True:
        prix_actuels = {}
        try:
            positions = etat.setdefault("positions", [])   # 1 position max par marché

            # 1) Prix de tous les marchés + mise à jour des fenêtres
            variations = {}
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

                ferme, pct_sortie, motif = False, 0.0, ""
                if i < 0:
                    if pct <= -STOP_PCT:
                        pct_sortie, motif, ferme = -STOP_PCT, "STOP", True
                else:
                    plancher = PALIERS_PCT[i - 1] if i >= 1 else 0.0
                    if pct <= plancher:
                        pct_sortie, motif, ferme = plancher, "PALIER", True

                if not ferme:
                    restantes.append(pos)
                    continue

                # ── Calcul complet et honnête du résultat ──
                m = pos["marche"]
                brut       = round(pct_sortie * NOTIONNEL, 4)                 # gain/perte "prix"
                frais_tot  = round(2 * NOTIONNEL * FRAIS_TAKER, 4)            # 2 pattes
                gliss_tot  = round(2 * NOTIONNEL * GLISSEMENT_PCT, 4)         # 2 pattes
                ouvert_ts  = pos.get("ouvert_ts", time.time())
                duree_sec  = max(0, time.time() - ouvert_ts)
                nb_8h      = int(duree_sec // 28800)                          # nb de règlements funding traversés
                # SHORT encaisse le funding (funding supposé +), LONG le paie
                funding    = round(nb_8h * NOTIONNEL * FUNDING_EST_8H * (1 if pos["sens"] == "short" else -1), 4)
                resultat   = round(brut - frais_tot - gliss_tot + funding, 4)  # NET de tout

                etat["capital"]   = round(etat.get("capital", CAPITAL_INITIAL) + resultat, 4)
                etat["total_net"] = round(etat.get("total_net", 0) + resultat, 4)
                etat["nb_trades"] = etat.get("nb_trades", 0) + 1
                if resultat > 0:
                    etat["nb_gagnants"] = etat.get("nb_gagnants", 0) + 1
                etat.setdefault("historique", []).append({
                    "marche": m, "sens": pos["sens"], "entree": entree, "sortie": p,
                    "ouvert_le": pos.get("ouvert_le"), "ferme_le": datetime.now(timezone.utc).isoformat(),
                    "duree_sec": round(duree_sec), "palier_max": i, "motif": motif,
                    "brut": brut, "frais": frais_tot, "glissement": gliss_tot, "funding": funding,
                    "resultat": resultat})

                ic = "✅" if resultat > 0 else "❌"
                sens_txt = "LONG" if pos["sens"] == "long" else "SHORT"
                if motif == "STOP":
                    detail = f"cassure, stoppé à -{STOP_PCT*100:.2f}%"
                elif i >= 1:
                    detail = f"a couru jusqu'au palier {i + 1}, verrouillé au palier {i} (+{pct_sortie*100:.1f}%)"
                else:
                    detail = "1er palier atteint puis retour au point mort"
                await telegram(session,
                    f"{ic} <b>{sens_txt} FERMÉ — {m.replace('USD','')}</b>\n"
                    f"{detail}\n"
                    f"Entrée {entree} → sortie {p} | durée {_duree_txt(duree_sec)}\n"
                    f"Brut {brut:+.2f}€ − frais {frais_tot:.2f}€ − glissement {gliss_tot:.2f}€"
                    f"{f' {funding:+.2f}€ funding' if nb_8h else ''}\n"
                    f"= <b>Net {resultat:+.2f}€</b> | Capital : {etat['capital']:.2f}€")

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
                    "ouvert_le": datetime.now(timezone.utc).isoformat(),
                    "ouvert_ts": time.time(), "palier": -1})
                marches_occupes.add(m)
                fleche = "📈 hausse" if sens == "long" else "📉 baisse"
                await telegram(session,
                    f"🚀 <b>IMPULSION — {m.replace('USD','')}</b>\n"
                    f"{fleche} de {abs(var)*100:.2f}% en {FENETRE_SEC//60} min → on SUIT en "
                    f"<b>{sens.upper()}</b> @ {p}\n"
                    f"Notionnel {NOTIONNEL:.0f}€ | stop -{STOP_PCT*100:.2f}% | "
                    f"1er palier +{PALIERS_PCT[0]*100:.1f}% puis échelle\n"
                    f"(frais + glissement ≈ {2*NOTIONNEL*(FRAIS_TAKER+GLISSEMENT_PCT):.2f}€ déduits à la fermeture)")

            # 4) Bilan quotidien à 22h UTC (19h Guyane) — se déclenche même si le
            #    bot a démarré un peu après l'heure (>= 22h), tant que pas déjà envoyé.
            now = datetime.now(timezone.utc)
            cle = now.strftime("%Y-%m-%d")
            if now.hour >= 22 and etat.get("dernier_rapport") != cle:
                etat["dernier_rapport"] = cle
                await rapport_quotidien(session, etat, cle, prix_actuels)

            sauvegarder_etat(etat)
        except Exception as e:
            log.error(f"Boucle : {e}")

        # Battement de cœur : P&L en direct de chaque position ouverte
        now = datetime.now(timezone.utc)
        net = round(etat.get("capital", CAPITAL_INITIAL) - CAPITAL_INITIAL, 3)
        po = etat.get("positions", [])
        bouts = []
        for pos in po:
            p = prix_actuels.get(pos["marche"])
            nom = pos["marche"].replace("USD", "")
            if p:
                pct = (p - pos["entree"]) / pos["entree"] if pos["sens"] == "long" else (pos["entree"] - p) / pos["entree"]
                bouts.append(f"{nom} {pct*NOTIONNEL:+.2f}€")
            else:
                bouts.append(nom)
        detail = ", ".join(bouts) if bouts else "aucune"
        log.info(f"  ❤️ [{now:%H:%M} UTC] Momentum actif — {len(po)} ouverte(s) [{detail}] | "
                 f"net {net:+.3f}€ | {etat.get('nb_trades',0)} clôturées")

        await asyncio.sleep(PAUSE_LOOP_SEC)


async def rapport_quotidien(session, etat, cle, prix_actuels=None):
    prix_actuels = prix_actuels or {}
    hist = etat.get("historique", [])
    jour = [h for h in hist if str(h.get("ferme_le", ""))[:10] == cle]

    net_jour  = round(sum(h.get("resultat", 0) for h in jour), 2)
    gagnants  = sum(1 for h in jour if h.get("resultat", 0) > 0)
    perdants  = len(jour) - gagnants
    nb_palier = sum(1 for h in jour if h.get("motif") == "PALIER")
    nb_stop   = sum(1 for h in jour if h.get("motif") == "STOP")
    gain_moy  = round(sum(h["resultat"] for h in jour if h["resultat"] > 0) / gagnants, 2) if gagnants else 0
    perte_moy = round(sum(h["resultat"] for h in jour if h["resultat"] <= 0) / perdants, 2) if perdants else 0

    # Totaux de coûts du jour (transparence)
    tot_brut  = round(sum(h.get("brut", 0) for h in jour), 2)
    tot_frais = round(sum(h.get("frais", 0) for h in jour), 2)
    tot_gliss = round(sum(h.get("glissement", 0) for h in jour), 2)
    tot_fund  = round(sum(h.get("funding", 0) for h in jour), 2)
    duree_moy = round(sum(h.get("duree_sec", 0) for h in jour) / len(jour)) if jour else 0

    # Détail trade par trade : sens, marché, entrée→sortie, durée, palier max, net
    lignes = []
    for h in jour[:30]:
        m = h.get("marche", "?").replace("USD", "")
        sens = "L" if h.get("sens") == "long" else "S"
        ic = "✅" if h.get("resultat", 0) > 0 else "❌"
        pmax = h.get("palier_max", -1)
        pmax_txt = f"P{pmax+1}" if pmax >= 0 else "—"
        lignes.append(
            f"{ic}{sens} {m:5s} {h.get('resultat',0):+6.2f}€ "
            f"{_duree_txt(h.get('duree_sec',0)):>5s} max:{pmax_txt}")
    if len(jour) > 30:
        lignes.append(f"… et {len(jour)-30} autres")
    detail = "\n".join(lignes) if lignes else "(aucune position clôturée aujourd'hui)"

    # État EN DIRECT des positions encore ouvertes
    po = etat.get("positions", [])
    lignes_o = []
    for pos in po:
        m = pos["marche"]
        p = prix_actuels.get(m)
        sens = "L" if pos["sens"] == "long" else "S"
        dur = _duree_txt(time.time() - pos.get("ouvert_ts", time.time()))
        if p:
            pct = (p - pos["entree"]) / pos["entree"] if pos["sens"] == "long" else (pos["entree"] - p) / pos["entree"]
            pnl = round(pct * NOTIONNEL, 2)
            pmax = pos.get("palier", -1)
            pmax_txt = f"P{pmax+1}" if pmax >= 0 else "—"
            lignes_o.append(f"{sens} {m.replace('USD',''):5s} {pnl:+6.2f}€ (en cours) {dur:>5s} max:{pmax_txt}")
        else:
            lignes_o.append(f"{sens} {m.replace('USD',''):5s} (prix indispo) {dur:>5s}")
    ouverts = "\n".join(lignes_o) if lignes_o else "aucune"

    if len(jour) == 0:
        lecture = "Aucune impulsion clôturée aujourd'hui — trop tôt pour juger."
    elif nb_palier == 0:
        lecture = f"Aucune impulsion n'a couru : toutes retournées (stop {nb_stop}×). Fausses cassures."
    elif net_jour > 0:
        lecture = f"Les gagnants ({gain_moy:+.2f}€ moy) couvrent les perdants ({perte_moy:+.2f}€ moy). À confirmer sur plusieurs jours."
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
        f"Durée moyenne d'un trade : {_duree_txt(duree_moy)}\n"
        f"\n🎯 <b>LE CHIFFRE CLÉ</b>\n"
        f"Ont couru (palier) : {nb_palier} | Fausses cassures (stop) : {nb_stop}\n"
        f"→ {lecture}\n"
        f"\n🧾 <b>DÉCOMPTE DES COÛTS (jour)</b>\n"
        f"Brut : {tot_brut:+.2f}€\n"
        f"− Frais : {tot_frais:.2f}€ | − Glissement : {tot_gliss:.2f}€"
        f"{f' | Funding : {tot_fund:+.2f}€' if tot_fund else ''}\n"
        f"= Net : {net_jour:+.2f}€\n"
        f"\n📋 <b>DÉTAIL CLÔTURÉS</b> (L/S · net · durée · palier max)\n<pre>{detail}</pre>\n"
        f"⏳ <b>ENCORE OUVERTES</b> (P&L en direct)\n<pre>{ouverts}</pre>")


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

        # Aperçu de l'état au démarrage (pour voir où on en est sans attendre 19h)
        hist = etat.get("historique", [])
        if hist:
            gagn  = sum(1 for h in hist if h.get("resultat", 0) > 0)
            npal  = sum(1 for h in hist if h.get("motif") == "PALIER")
            nstop = sum(1 for h in hist if h.get("motif") == "STOP")
            net_tot = round(etat.get("capital", CAPITAL_INITIAL) - CAPITAL_INITIAL, 2)
            await telegram(session,
                f"📸 <b>ÉTAT ACTUEL (depuis le début)</b>\n"
                f"Positions clôturées : {len(hist)} | Gagnantes : {gagn}\n"
                f"Ont couru (palier) : {npal} | Fausses cassures (stop) : {nstop}\n"
                f"<b>Net total : {net_tot:+.2f}€</b> | Capital : {etat.get('capital',CAPITAL_INITIAL):.2f}€")

        await boucle(session, etat)


if __name__ == "__main__":
    asyncio.run(main())
