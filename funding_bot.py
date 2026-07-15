#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║  BOT DE FUNDING — capture delta-neutre (SIMULATION)                        ║
║  Phase 2 du projet. OKX X-Perp. Persistance PostgreSQL. Rapports Telegram. ║
╚══════════════════════════════════════════════════════════════════════════╝

PRINCIPE (pas de pari directionnel)
-----------------------------------
Sur un perp à funding POSITIF, les longs paient les shorts toutes les 8h.
On prend une position DELTA-NEUTRE :
    • SHORT le perp   → on ENCAISSE le funding
    • LONG le spot    → on annule le risque de prix
Quel que soit le sens du marché, le prix ne nous fait ni gagner ni perdre
(le spot compense le perp). On ne touche QUE le funding, moins les frais.
C'est un edge RÉEL et mesurable — modeste, mais positif.

CE BOT EST EN SIMULATION
------------------------
Il ne passe AUCUN ordre. Il choisit les meilleurs marchés à funding positif
et stable, "ouvre" des positions delta-neutres fictives, encaisse le funding
réel d'OKX à chaque règlement (00h/08h/16h UTC), paie les frais, et te fait
un rapport Telegram. But : PROUVER le concept et le maîtriser avant d'y mettre
du capital réel. Comme le prix est neutralisé, le P&L simulé = funding − frais
(hypothèse honnête du delta-neutre parfait ; la petite "base" spot/perp réelle
sera à modéliser plus tard).

DÉPLOIEMENT (comme ton scalper)
-------------------------------
Variables Railway nécessaires (tu les as déjà) :
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, DATABASE_URL
Optionnel : CAPITAL_FUNDING (défaut 416).  RESET_FUNDING=1 pour repartir de zéro.
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
CAPITAL_INITIAL = float(os.environ.get("CAPITAL_FUNDING", "416"))

MAX_POSITIONS   = 3        # nb de marchés tenus en parallèle (diversifie le risque funding)
# Capital delta-neutre = moitié spot + moitié marge perp (sans levier). Le notionnel
# total possible = CAPITAL/2, réparti sur MAX_POSITIONS.
NOTIONNEL_PAR_POS = CAPITAL_INITIAL / (2 * MAX_POSITIONS)

# Entrée : on n'ouvre que si le funding est assez élevé ET stable.
SEUIL_ENTREE_ANNUEL = 5.0   # % annualisé minimum pour ouvrir
SEUIL_STABILITE     = 80.0  # % de périodes positives (sur l'historique) minimum
# Sortie : on ferme si le funding passe sous ce seuil (devient trop faible/négatif).
SEUIL_SORTIE_ANNUEL = 0.0   # % annualisé : sous 0 = funding négatif → on paierait → on sort

# Frais OKX (taker, prudent). 2 pattes (perp + spot), payées à l'entrée ET à la sortie.
FRAIS_PERP = 0.0005         # 0.05 %
FRAIS_SPOT = 0.0010         # 0.10 %
FRAIS_UNE_PATTE = FRAIS_PERP + FRAIS_SPOT          # 0.15 % (une ouverture OU une fermeture)
FRAIS_ALLER_RETOUR = 2 * FRAIS_UNE_PATTE           # 0.30 % (ouverture + fermeture)

N_HIST_FUNDING = 30         # nb de périodes de funding lues pour juger (30 × 8h ≈ 10 j)
PAUSE_LOOP_SEC = 300        # scan toutes les 5 min

CANDIDATS = [
    "ETH", "XRP", "SOL", "ADA", "LINK", "DOGE", "LTC", "TRX", "UNI", "HYPE",
    "AVAX", "NEAR", "AAVE", "SUI", "FIL", "BTC", "ALGO", "INJ", "BNB",
]

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
RESET_FUNDING    = os.environ.get("RESET_FUNDING", "0").strip().lower() in ("1", "true", "oui", "yes")

OKX_PUBLIC = "https://www.okx.com"
SETTLEMENTS_UTC = (0, 8, 16)   # heures UTC de règlement du funding

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("funding")

OKX_SYMBOLS = {}   # { "AAVEUSD": "AAVE-USD_UM_XPERP-..." } instId public


# ═══════════════════════════════════════════════════════════════════════════
#  PERSISTANCE POSTGRESQL  (mêmes DATABASE_URL / pg8000 que ton scalper,
#  mais table séparée `funding_etat` → aucun conflit avec le scalper)
# ═══════════════════════════════════════════════════════════════════════════
def _connexion():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL absente")
    p = urlparse(url)
    derniere = None
    for essai in range(4):
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
        cur.execute("""CREATE TABLE IF NOT EXISTS funding_etat (
                         id INTEGER PRIMARY KEY DEFAULT 1, data TEXT NOT NULL,
                         maj_le TIMESTAMP DEFAULT NOW())""")
        conn.commit()
    finally:
        conn.close()

def charger_etat():
    conn = _connexion()
    try:
        cur = conn.cursor()
        cur.execute("SELECT data FROM funding_etat WHERE id = 1")
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
            cur.execute("""INSERT INTO funding_etat (id, data, maj_le) VALUES (1, %s, NOW())
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
#  OKX — instruments + funding
# ═══════════════════════════════════════════════════════════════════════════
async def resoudre_instruments(session):
    """Mappe chaque crypto à son instId X-Perp public (instType=FUTURES, ruleType=xperp)."""
    global OKX_SYMBOLS
    try:
        async with session.get(f"{OKX_PUBLIC}/api/v5/public/instruments",
                               params={"instType": "FUTURES"},
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
        if data.get("code") != "0":
            log.error(f"instruments OKX : {data}")
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

async def funding_marche(session, inst_id):
    """Renvoie (taux_courant, taux_moyen, pct_positif, annualise_moyen) ou None.
    taux_courant = dernier funding réglé (pour créditer). moyen/pct = pour décider."""
    try:
        url = f"{OKX_PUBLIC}/api/v5/public/funding-rate-history"
        async with session.get(url, params={"instId": inst_id, "limit": str(N_HIST_FUNDING)},
                               timeout=aiohttp.ClientTimeout(total=12)) as r:
            data = await r.json()
        if data.get("code") != "0" or not data.get("data"):
            return None
        taux = [float(x["fundingRate"]) for x in data["data"]]   # OKX renvoie du plus récent au plus ancien
        courant = taux[0]
        moyen   = sum(taux) / len(taux)
        pct_pos = sum(1 for t in taux if t > 0) / len(taux) * 100
        annuel  = moyen * 3 * 365 * 100
        return courant, moyen, pct_pos, annuel
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  LOGIQUE DE RÈGLEMENT DU FUNDING
# ═══════════════════════════════════════════════════════════════════════════
def dernier_settlement_ts(maintenant=None):
    """Timestamp (UTC) du dernier règlement de funding (00h/08h/16h) <= maintenant."""
    now = maintenant or datetime.now(timezone.utc)
    h = max(s for s in SETTLEMENTS_UTC if s <= now.hour) if now.hour >= SETTLEMENTS_UTC[0] else SETTLEMENTS_UTC[-1]
    jour = now
    if now.hour < SETTLEMENTS_UTC[0]:
        # avant 00h impossible (00 est le min) — garde-fou
        h = SETTLEMENTS_UTC[0]
    borne = now.replace(hour=h, minute=0, second=0, microsecond=0)
    return borne.timestamp()


# ═══════════════════════════════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════
async def boucle(session, etat):
    while True:
        try:
            # 1) Lire le funding de tous les marchés
            infos = {}
            for m, inst in OKX_SYMBOLS.items():
                f = await funding_marche(session, inst)
                await asyncio.sleep(0.15)
                if f:
                    infos[m] = {"courant": f[0], "moyen": f[1], "pct_pos": f[2], "annuel": f[3]}

            positions = etat.setdefault("positions", {})

            # 2) RÈGLEMENT : si on a franchi une échéance depuis le dernier crédit,
            #    on crédite chaque position ouverte (funding réel × notionnel).
            settl = dernier_settlement_ts()
            if settl > etat.get("dernier_settlement_ts", 0) and positions:
                total_credit = 0.0
                details = []
                for m, pos in positions.items():
                    taux = infos.get(m, {}).get("courant", 0.0)
                    credit = taux * pos["notionnel"]          # short perp : + si funding +, - si -
                    pos["funding_encaisse"] = round(pos.get("funding_encaisse", 0) + credit, 4)
                    etat["capital"] = round(etat["capital"] + credit, 4)
                    total_credit += credit
                    details.append(f"{m.replace('USD','')} {taux*100:+.4f}% → {credit:+.3f}€")
                etat["dernier_settlement_ts"] = settl
                etat["total_funding"] = round(etat.get("total_funding", 0) + total_credit, 4)
                await telegram(session,
                    f"💰 <b>FUNDING ENCAISSÉ</b>\n"
                    f"<pre>{chr(10).join(details)}</pre>\n"
                    f"Total ce règlement : <b>{total_credit:+.3f}€</b>\n"
                    f"Capital : {etat['capital']:.2f}€")
            elif settl > etat.get("dernier_settlement_ts", 0):
                etat["dernier_settlement_ts"] = settl  # pas de position : on avance juste le repère

            # 3) SORTIES : fermer les positions dont le funding est tombé trop bas / négatif
            for m in list(positions.keys()):
                annuel = infos.get(m, {}).get("annuel")
                if annuel is None:
                    continue
                if annuel < SEUIL_SORTIE_ANNUEL:
                    pos = positions.pop(m)
                    frais_sortie = round(pos["notionnel"] * FRAIS_UNE_PATTE, 4)
                    etat["capital"] = round(etat["capital"] - frais_sortie, 4)
                    etat["total_frais"] = round(etat.get("total_frais", 0) + frais_sortie, 4)
                    net = round(pos.get("funding_encaisse", 0) - pos.get("frais_entree", 0) - frais_sortie, 4)
                    etat.setdefault("historique", []).append({
                        "marche": m, "ouvert_le": pos.get("ouvert_le"),
                        "ferme_le": datetime.now(timezone.utc).isoformat(),
                        "funding": pos.get("funding_encaisse", 0), "net": net})
                    await telegram(session,
                        f"🔴 <b>POSITION FERMÉE — {m.replace('USD','')}</b>\n"
                        f"Funding devenu trop faible ({annuel:.1f}%/an).\n"
                        f"Funding encaissé : {pos.get('funding_encaisse',0):+.3f}€ | "
                        f"Frais totaux : -{pos.get('frais_entree',0)+frais_sortie:.3f}€\n"
                        f"Résultat net position : <b>{net:+.3f}€</b>")

            # 4) ENTRÉES : remplir les slots libres avec les meilleurs marchés éligibles
            slots_libres = MAX_POSITIONS - len(positions)
            if slots_libres > 0:
                candidats = [
                    (m, d) for m, d in infos.items()
                    if m not in positions
                    and d["annuel"] >= SEUIL_ENTREE_ANNUEL
                    and d["pct_pos"] >= SEUIL_STABILITE
                ]
                candidats.sort(key=lambda x: -x[1]["annuel"])
                for m, d in candidats[:slots_libres]:
                    frais_entree = round(NOTIONNEL_PAR_POS * FRAIS_UNE_PATTE, 4)
                    etat["capital"] = round(etat["capital"] - frais_entree, 4)
                    etat["total_frais"] = round(etat.get("total_frais", 0) + frais_entree, 4)
                    positions[m] = {
                        "notionnel": round(NOTIONNEL_PAR_POS, 2),
                        "ouvert_le": datetime.now(timezone.utc).isoformat(),
                        "funding_encaisse": 0.0, "frais_entree": frais_entree}
                    await telegram(session,
                        f"🟢 <b>POSITION OUVERTE — {m.replace('USD','')}</b>\n"
                        f"Delta-neutre : short perp + long spot, notionnel {NOTIONNEL_PAR_POS:.0f}€\n"
                        f"Funding actuel : {d['annuel']:.1f}%/an (positif {d['pct_pos']:.0f}% du temps)\n"
                        f"Frais d'entrée : -{frais_entree:.3f}€")

            # 5) Rapport quotidien (22h UTC = 19h Guyane)
            now = datetime.now(timezone.utc)
            cle_jour = now.strftime("%Y-%m-%d")
            if now.hour == 22 and etat.get("dernier_rapport") != cle_jour:
                etat["dernier_rapport"] = cle_jour
                await rapport_quotidien(session, etat)

            sauvegarder_etat(etat)
        except Exception as e:
            log.error(f"Boucle : {e}")

        await asyncio.sleep(PAUSE_LOOP_SEC)


async def rapport_quotidien(session, etat):
    positions = etat.get("positions", {})
    lignes = []
    for m, pos in positions.items():
        lignes.append(f"{m.replace('USD',''):8s} {pos['notionnel']:>5.0f}€  "
                      f"funding {pos.get('funding_encaisse',0):+.3f}€")
    tableau = "\n".join(lignes) if lignes else "(aucune position ouverte)"
    net_total = round(etat.get("capital", CAPITAL_INITIAL) - CAPITAL_INITIAL, 3)
    await telegram(session,
        f"📊 <b>BILAN FUNDING — {datetime.now(timezone.utc):%Y-%m-%d}</b>\n"
        f"<pre>{tableau}</pre>\n"
        f"Funding total encaissé : {etat.get('total_funding',0):+.3f}€\n"
        f"Frais totaux payés     : -{etat.get('total_frais',0):.3f}€\n"
        f"<b>Résultat net global   : {net_total:+.3f}€</b>\n"
        f"Capital : {etat.get('capital',CAPITAL_INITIAL):.2f}€ "
        f"(départ {CAPITAL_INITIAL:.0f}€)")


# ═══════════════════════════════════════════════════════════════════════════
#  DÉMARRAGE
# ═══════════════════════════════════════════════════════════════════════════
async def main():
    log.info("=" * 60)
    log.info("  BOT FUNDING — capture delta-neutre (SIMULATION)")
    log.info("=" * 60)
    init_db()
    etat = {} if RESET_FUNDING else (charger_etat() or {})
    etat.setdefault("capital", CAPITAL_INITIAL)
    etat.setdefault("positions", {})
    etat.setdefault("total_funding", 0.0)
    etat.setdefault("total_frais", 0.0)
    etat.setdefault("dernier_settlement_ts", dernier_settlement_ts())  # ne crédite pas rétroactivement

    async with aiohttp.ClientSession() as session:
        await resoudre_instruments(session)
        await telegram(session,
            (f"🔄 <b>RESET FUNDING</b> — repart de zéro.\n\n" if RESET_FUNDING else "")
            + f"🏦 <b>BOT FUNDING DÉMARRÉ</b> (simulation)\n"
            f"Capital : {etat['capital']:.2f}€\n"
            f"Positions max : {MAX_POSITIONS} | notionnel/pos : {NOTIONNEL_PAR_POS:.0f}€\n"
            f"Entrée si funding ≥ {SEUIL_ENTREE_ANNUEL:.0f}%/an et stable ≥ {SEUIL_STABILITE:.0f}%\n"
            f"Frais aller-retour : {FRAIS_ALLER_RETOUR*100:.2f}% du notionnel\n"
            f"Principe : short perp + long spot → encaisse le funding, sans pari.\n"
            f"Marchés suivis : {len(OKX_SYMBOLS)}")
        await boucle(session, etat)


if __name__ == "__main__":
    asyncio.run(main())
