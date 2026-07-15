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

# ── TOUT EN POURCENTAGE de mouvement de prix (demande de Damien).
#    Équivalent en € = pourcentage × notionnel. À 200€ : 0.5% = 1€, 1.0% = 2€.
STOP_PCT = 0.005             # stop du côté perdant : 0.50% (= 1€ à 200€)

# ── Échelle de PALIERS pour le côté GAGNANT : 1.5%, 2.5%, 3.5%, 4.5%… soit en €
#    (à 200€) : 3€, 5€, 7€, 9€… « ainsi de suite ». Le côté gagnant grimpe l'échelle ;
#    un plancher trailing verrouille le palier PRÉCÉDENT (au 1er palier : plancher au
#    point mort / breakeven). Ça laisse courir les gros mouvements tout en protégeant.
PALIER_1_PCT   = 0.015       # 1er palier : 1.50% (= 3€)
PALIER_PAS_PCT = 0.010       # pas entre paliers : 1.00% (= +2€)
NB_PALIERS     = 15          # nombre de paliers dans l'échelle (3€ → 31€)
PALIERS_PCT = [round(PALIER_1_PCT + i * PALIER_PAS_PCT, 6) for i in range(NB_PALIERS)]

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
    frais_1_cote = round(NOTIONNEL_PAR_COTE * FRAIS_TAKER, 4)

    while True:
        try:
            refs      = etat.setdefault("references", {})    # prix de référence par marché
            positions = etat.setdefault("positions", [])     # LISTE de positions INDÉPENDANTES

            # 1) Récupère le prix de tous les marchés
            prix_actuels = {}
            for m, inst in OKX_SYMBOLS.items():
                p = await prix(session, inst)
                await asyncio.sleep(0.1)
                if p is not None:
                    prix_actuels[m] = p
                    refs.setdefault(m, p)

            # 2) GESTION — chaque position vit sa vie, indépendamment de l'autre côté
            restantes = []
            for pos in positions:
                p = prix_actuels.get(pos["marche"])
                if p is None:
                    restantes.append(pos)
                    continue
                entree = pos["entree"]
                pct = (p - entree) / entree if pos["sens"] == "long" else (entree - p) / entree

                # Monte l'échelle de paliers (plus haut palier franchi)
                i = pos.get("palier", -1)
                while i + 1 < len(PALIERS_PCT) and pct >= PALIERS_PCT[i + 1]:
                    i += 1
                pos["palier"] = i

                ferme, resultat, motif = False, 0.0, ""
                if i < 0:
                    # Aucun palier franchi : stop initial à -STOP_PCT
                    if pct <= -STOP_PCT:
                        resultat = round(-STOP_PCT * NOTIONNEL_PAR_COTE - frais_1_cote, 4)
                        motif, ferme = "STOP", True
                else:
                    # Palier franchi : plancher trailing = palier PRÉCÉDENT (breakeven au 1er)
                    plancher = PALIERS_PCT[i - 1] if i >= 1 else 0.0
                    if pct <= plancher:
                        resultat = round(plancher * NOTIONNEL_PAR_COTE - frais_1_cote, 4)
                        motif, ferme = "PALIER", True

                if not ferme:
                    restantes.append(pos)
                    continue

                # Fermeture INDÉPENDANTE de cette position
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
                    detail = f"stoppé à -{STOP_PCT*100:.2f}%"
                elif i >= 1:
                    detail = f"palier {i + 1} franchi, verrouillé au palier {i}"
                else:
                    detail = "monté au 1er palier puis revenu au point mort"
                await telegram(session,
                    f"{ic} <b>{sens_txt} FERMÉ — {m.replace('USD','')}</b>\n"
                    f"Résultat : <b>{resultat:+.2f}€</b> ({detail})\n"
                    f"Capital : {etat['capital']:.2f}€")
                refs[m] = p   # référence remise à jour

            etat["positions"] = restantes
            positions = restantes

            # 3) SIGNAUX — ouvre une PAIRE indépendante (long + short) si le marché est libre.
            #    « Libre » = aucune position en cours sur ce marché (évite d'empiler sans fin).
            marches_occupes = {pos["marche"] for pos in positions}
            for m, p in prix_actuels.items():
                if m in marches_occupes:
                    continue
                variation = abs(p - refs[m]) / refs[m]
                if variation >= SEUIL_MOUVEMENT:
                    for sens in ("long", "short"):
                        positions.append({
                            "marche": m, "sens": sens, "entree": p,
                            "ouvert_le": datetime.now(timezone.utc).isoformat(), "palier": -1})
                    etat["capital"] = round(etat.get("capital", CAPITAL_INITIAL) - 2 * frais_1_cote, 4)
                    marches_occupes.add(m)
                    await telegram(session,
                        f"🔀 <b>DOUBLE POSITION — {m.replace('USD','')}</b>\n"
                        f"Mouvement {variation*100:.2f}% → LONG + SHORT ouverts @ {p} "
                        f"(<b>indépendants</b>)\n"
                        f"Notionnel {NOTIONNEL_PAR_COTE:.0f}€/côté | stop -{STOP_PCT*100:.2f}% "
                        f"| 1er palier +{PALIERS_PCT[0]*100:.1f}% puis échelle\n"
                        f"Frais d'ouverture (2 côtés) : -{2*frais_1_cote:.3f}€")

            # 4) Bilan quotidien complet à 22h UTC (19h Guyane)
            now = datetime.now(timezone.utc)
            cle = now.strftime("%Y-%m-%d")
            if now.hour == 22 and etat.get("dernier_rapport") != cle:
                etat["dernier_rapport"] = cle
                await rapport_quotidien(session, etat, cle)

            sauvegarder_etat(etat)
        except Exception as e:
            log.error(f"Boucle : {e}")

        # Battement de cœur : nb de positions ouvertes (chaque côté compte pour 1)
        now = datetime.now(timezone.utc)
        net = round(etat.get("capital", CAPITAL_INITIAL) - CAPITAL_INITIAL, 3)
        pos_ouvertes = etat.get("positions", [])
        marches = len({pos["marche"] for pos in pos_ouvertes})
        log.info(f"  ❤️ [{now:%H:%M} UTC] Deux-sens actif — {len(pos_ouvertes)} positions ouvertes "
                 f"sur {marches} marchés | net {net:+.3f}€ | {etat.get('nb_trades',0)} clôturées")

        await asyncio.sleep(PAUSE_LOOP_SEC)


# ═══════════════════════════════════════════════════════════════════════════
#  DÉMARRAGE
# ═══════════════════════════════════════════════════════════════════════════
async def rapport_quotidien(session, etat, cle):
    """Bilan complet du jour : résultat, trades, palier vs stop, détail, ouverts."""
    hist = etat.get("historique", [])
    jour = [h for h in hist if str(h.get("ferme_le", ""))[:10] == cle]

    net_jour = round(sum(h.get("resultat", 0) for h in jour), 2)
    gagnants = sum(1 for h in jour if h.get("resultat", 0) > 0)
    perdants = len(jour) - gagnants

    # Le chiffre clé : combien de positions ont grimpé un palier vs juste stoppé
    nb_palier = sum(1 for h in jour if h.get("motif") == "PALIER")
    nb_stop   = sum(1 for h in jour if h.get("motif") == "STOP")

    # Détail position par position (compact) — 🎯 = palier franchi, 🛑 = stoppé
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

    pos_ouvertes = etat.get("positions", [])
    ouverts = f"{len(pos_ouvertes)} positions sur {len({p['marche'] for p in pos_ouvertes})} marchés" \
              if pos_ouvertes else "aucune"

    # Lecture en une phrase, factuelle
    if len(jour) == 0:
        lecture = "Aucune position terminée aujourd'hui — trop tôt pour juger."
    elif nb_palier == 0:
        lecture = f"Aucune position n'a franchi de palier. Toutes stoppées ({nb_stop}×)."
    elif nb_palier < nb_stop:
        lecture = f"Le stop tombe bien plus souvent qu'un palier ({nb_stop} contre {nb_palier})."
    else:
        lecture = f"Palier franchi {nb_palier}× / stoppé {nb_stop}×."

    await telegram(session,
        f"📊 <b>BILAN DEUX SENS — {cle}</b>\n"
        f"\n💰 <b>RÉSULTAT DU JOUR</b>\n"
        f"Net : <b>{net_jour:+.2f}€</b>\n"
        f"Capital : {etat.get('capital',CAPITAL_INITIAL):.2f}€ (départ {CAPITAL_INITIAL:.0f}€)\n"
        f"\n📈 <b>POSITIONS (chaque côté compté séparément)</b>\n"
        f"Clôturées aujourd'hui : {len(jour)}\n"
        f"Gagnantes : {gagnants} | Perdantes : {perdants}\n"
        f"\n🎯 <b>LE CHIFFRE CLÉ</b>\n"
        f"Ont franchi un palier : {nb_palier} fois\n"
        f"Juste stoppées (-{STOP_PCT*100:.2f}%) : {nb_stop} fois\n"
        f"→ {lecture}\n"
        f"\n📋 <b>DÉTAIL</b> (L=long, S=short)\n<pre>{detail}</pre>\n"
        f"⏳ Encore ouvertes : {ouverts}")


async def main():
    log.info("=" * 60)
    log.info("  BOT DEUX SENS — capture des deux directions (SIMULATION)")
    log.info("=" * 60)
    init_db()
    etat = {} if RESET_DEUXSENS else (charger_etat() or {})
    etat.setdefault("capital", CAPITAL_INITIAL)
    etat.setdefault("positions", [])
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
            f"À chaque signal (mouvement ≥ {SEUIL_MOUVEMENT*100:.1f}%) : LONG + SHORT "
            f"<b>indépendants</b> (chacun sa vie)\n"
            f"Notionnel {NOTIONNEL_PAR_COTE:.0f}€/côté\n"
            f"Stop : -{STOP_PCT*100:.2f}% (= -{STOP_PCT*NOTIONNEL_PAR_COTE:.1f}€)\n"
            f"Paliers (échelle) : "
            f"{', '.join(f'{x*100:.1f}%' for x in PALIERS_PCT[:5])}…\n"
            f"  soit en € : {', '.join(f'{x*NOTIONNEL_PAR_COTE:.0f}€' for x in PALIERS_PCT[:5])}…\n"
            f"Le gagnant grimpe l'échelle, un plancher verrouille le palier précédent.\n"
            f"Marchés suivis : {len(OKX_SYMBOLS)}")
        await boucle(session, etat)


if __name__ == "__main__":
    asyncio.run(main())
