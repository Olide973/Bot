#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════╗
║  SCANNER DE FUNDING — OKX  (rapport envoyé sur Telegram)              ║
║  Étape 1 du projet "capture de funding" (delta-neutre).              ║
╚══════════════════════════════════════════════════════════════════════╝

À QUOI ÇA SERT
--------------
Sur les perpétuels, un "funding" est échangé toutes les 8h entre longs et
shorts. Quand le funding est POSITIF, ce sont les LONGS qui paient les SHORTS.
Capture : position delta-neutre (SHORT le perp pour ENCAISSER le funding +
LONG le spot pour annuler le risque de prix). On touche le funding toutes les
8h, quel que soit le sens du marché. Pas de pari directionnel.

Ce script NE TRADE RIEN. Il lit le funding réel OKX, calcule le rendement
réaliste APRÈS frais à ton échelle, et envoie un rapport clair sur TELEGRAM.
Aucune clé API OKX nécessaire : le funding est une donnée PUBLIQUE.

DÉPLOIEMENT
-----------
Déploie-le comme ton bot. Il faut juste que ces 2 variables soient présentes
dans Railway (tu les as déjà pour ton bot) :
    TELEGRAM_TOKEN     ton token de bot Telegram
    TELEGRAM_CHAT_ID   l'id de ton chat
Il envoie le rapport puis s'arrête. Relance-le quand tu veux re-scanner.
"""

import os
import asyncio
import aiohttp
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════════
#  RÉGLAGES
# ═══════════════════════════════════════════════════════════════════════
CAPITAL_EUR = 543.0            # ton capital total

MARCHES = [
    "BTC-USDT-SWAP",  "ETH-USDT-SWAP",  "SOL-USDT-SWAP",  "XRP-USDT-SWAP",
    "DOGE-USDT-SWAP", "ADA-USDT-SWAP",  "AVAX-USDT-SWAP", "LINK-USDT-SWAP",
    "NEAR-USDT-SWAP", "SUI-USDT-SWAP",  "LTC-USDT-SWAP",  "INJ-USDT-SWAP",
    "AAVE-USDT-SWAP",
]

N_PERIODES = 30               # 30 × 8h ≈ 10 jours d'historique de funding

FRAIS_PERP_TAKER = 0.0005     # 0.05 %  (taker, prudent)
FRAIS_SPOT_TAKER = 0.0010     # 0.10 %

BASE = "https://www.okx.com"

TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')


# ═══════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════════════
async def telegram(session, message):
    """Envoie un message sur Telegram (mêmes variables que ton bot)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_TOKEN / TELEGRAM_CHAT_ID absents — rapport affiché en logs seulement.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        async with session.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                print(f"⚠️ Telegram a répondu {r.status} : {await r.text()}")
    except Exception as e:
        print(f"⚠️ Erreur Telegram : {e}")


# ═══════════════════════════════════════════════════════════════════════
#  RÉCUPÉRATION DU FUNDING
# ═══════════════════════════════════════════════════════════════════════
async def funding_history(session, inst_id):
    """Renvoie la liste des derniers taux de funding d'un marché, ou None."""
    url = f"{BASE}/api/v5/public/funding-rate-history"
    params = {"instId": inst_id, "limit": str(N_PERIODES)}
    try:
        async with session.get(url, params=params, timeout=15) as r:
            data = await r.json()
            if data.get("code") != "0" or not data.get("data"):
                return None
            return [float(x["fundingRate"]) for x in data["data"]]
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
#  ANALYSE + RAPPORT
# ═══════════════════════════════════════════════════════════════════════
async def analyser():
    frais_aller_retour = 2 * (FRAIS_PERP_TAKER + FRAIS_SPOT_TAKER)  # entrée+sortie, 2 pattes

    resultats = []
    async with aiohttp.ClientSession() as session:
        for m in MARCHES:
            taux = await funding_history(session, m)
            await asyncio.sleep(0.2)  # respecte la limite de requêtes OKX
            if not taux:
                print(f"  {m:18s} : pas de données")
                continue
            moy_8h   = sum(taux) / len(taux)
            par_jour = moy_8h * 3
            annuel   = par_jour * 365
            pct_pos  = sum(1 for t in taux if t > 0) / len(taux) * 100
            resultats.append((m, moy_8h, par_jour, annuel, pct_pos))

        if not resultats:
            await telegram(session, "❌ Scanner funding : aucune donnée récupérée (connexion ou format des marchés).")
            print("Aucune donnée.")
            return

        resultats.sort(key=lambda x: -x[1])

        # ── Construction du tableau (monospace pour aligner sur Telegram) ──
        lignes = []
        lignes.append(f"{'Marché':14s}{'/8h':>9s}{'/jour':>8s}{'/an':>8s}{'+%':>6s}")
        lignes.append("-" * 45)
        for m, moy_8h, par_jour, annuel, pct_pos in resultats:
            nom = m.replace("-USDT-SWAP", "")
            lignes.append(f"{nom:14s}{moy_8h*100:>8.4f}{par_jour*100:>7.3f}{annuel*100:>7.1f}{pct_pos:>5.0f}")
        tableau = "\n".join(lignes)

        # ── Verdict sur le meilleur ──
        m, moy_8h, par_jour, annuel, pct_pos = resultats[0]
        nom = m.replace("-USDT-SWAP", "")
        notionnel = CAPITAL_EUR / 2   # moitié spot, moitié marge perp (sans levier = prudent)
        brut_30j  = notionnel * par_jour * 30
        net_30j   = brut_30j - notionnel * frais_aller_retour
        jours_seuil = (frais_aller_retour / par_jour) if par_jour > 0 else 0

        verdict = []
        verdict.append(f"<b>🏆 MEILLEUR : {nom}</b>")
        verdict.append(f"Funding : {moy_8h*100:.4f}%/8h → <b>{annuel*100:.1f}%/an</b> (brut)")
        stab = "stable ✅" if pct_pos >= 80 else f"instable ⚠️ ({pct_pos:.0f}% +)"
        verdict.append(f"Régularité : {stab}")
        verdict.append(f"Frais A/R : {frais_aller_retour*100:.2f}% du notionnel")
        if par_jour > 0:
            verdict.append(f"Rentable si tu tiens ~{jours_seuil:.0f} jours (le funding couvre les frais).")
        verdict.append("")
        verdict.append(f"<b>À ton échelle ({CAPITAL_EUR:.0f}€, ~{notionnel:.0f}€ delta-neutre) :</b>")
        verdict.append(f"Brut 30j : {brut_30j:+.2f}€ | Net après frais : <b>{net_30j:+.2f}€</b>")
        if net_30j <= 0:
            verdict.append("⚠️ Nul/négatif à cette échelle et ce funding. Pas rentable maintenant —")
            verdict.append("à re-scanner quand le funding remonte, ou avec plus de capital.")
        else:
            verdict.append(f"→ ~{net_30j/CAPITAL_EUR*100:.2f}%/mois. Modeste mais RÉEL, sans pari directionnel.")
            if pct_pos < 80:
                verdict.append("⚠️ Funding instable : surveiller (s'il passe négatif, tu paierais).")

        message = (
            f"📊 <b>SCANNER FUNDING OKX</b>\n"
            f"{datetime.utcnow():%Y-%m-%d %H:%M} UTC — {N_PERIODES//3} derniers jours\n"
            f"<i>Funding + = shorts payés → short perp + long spot</i>\n\n"
            f"<pre>{tableau}</pre>\n"
            + "\n".join(verdict)
        )

        await telegram(session, message)
        # Aussi en logs
        print(message.replace("<b>", "").replace("</b>", "").replace("<i>", "")
                     .replace("</i>", "").replace("<pre>", "").replace("</pre>", ""))
        print("\n✅ Rapport envoyé sur Telegram.")


if __name__ == "__main__":
    asyncio.run(analyser())
