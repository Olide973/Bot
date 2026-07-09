# ═══════════════════════════════════════════════════════════════
#  database.py — persistance PostgreSQL (Railway) pour bot_trading-48.py
#  Reconstruit le 09/07/2026 à partir de la structure d'origine
#  (mai 2026) : pg8000, DATABASE_URL lue DANS get_connection()
#  (correctif de mai — jamais au niveau module), tables etat_bot
#  et trades. Interface identique à l'original :
#    init_database(), charger_etat(), sauvegarder_etat(etat),
#    enregistrer_trade(trade)
# ═══════════════════════════════════════════════════════════════
import os
import json
import logging
from urllib.parse import urlparse

import pg8000

log = logging.getLogger(__name__)


def get_connection():
    """Ouvre une connexion PostgreSQL à partir de DATABASE_URL.
    La variable est lue ICI (pas au niveau module) — correctif de mai 2026 :
    Railway injecte parfois la variable après l'import du module, la lire à
    l'import figeait une valeur vide."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL absente des variables d'environnement")
    p = urlparse(url)
    return pg8000.connect(
        user=p.username,
        password=p.password,
        host=p.hostname,
        port=p.port or 5432,
        database=(p.path or "/postgres").lstrip("/"),
    )


def init_database():
    """Crée les tables si absentes (idempotent — ne touche jamais aux données
    existantes)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS etat_bot (
                id    INTEGER PRIMARY KEY DEFAULT 1,
                etat  TEXT NOT NULL,
                maj   TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            SERIAL PRIMARY KEY,
                horodatage    TIMESTAMP DEFAULT NOW(),
                marche        TEXT,
                direction     TEXT,
                resultat      TEXT,
                prix_entree   DOUBLE PRECISION,
                prix_sortie   DOUBLE PRECISION,
                stop_loss     DOUBLE PRECISION,
                objectif      DOUBLE PRECISION,
                mise          DOUBLE PRECISION,
                gain          DOUBLE PRECISION,
                capital_apres DOUBLE PRECISION,
                duree_minutes DOUBLE PRECISION,
                score         DOUBLE PRECISION,
                adx           DOUBLE PRECISION,
                atr           DOUBLE PRECISION,
                rsi           DOUBLE PRECISION
            )
        """)
        conn.commit()
        log.info("  Base de données initialisée (etat_bot, trades)")
    finally:
        conn.close()


def charger_etat():
    """Charge l'état complet du bot (dict). Retourne {} si aucun état
    sauvegardé — le bot initialise alors ses champs par défaut."""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT etat FROM etat_bot WHERE id = 1")
            row = cur.fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return {}
        finally:
            conn.close()
    except Exception as e:
        log.error(f"  [DB] Échec chargement état : {e} — état vide utilisé")
        return {}


def sauvegarder_etat(etat):
    """Sauvegarde l'état complet du bot (upsert sur la ligne unique id=1).
    Ne lève jamais : un échec de sauvegarde ne doit pas interrompre le
    trading — l'erreur est loggée (et remontée sur Telegram via le miroir
    d'erreurs du bot)."""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO etat_bot (id, etat, maj) VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET etat = EXCLUDED.etat, maj = NOW()
            """, (json.dumps(etat),))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"  [DB] Échec sauvegarde état : {e}")


def enregistrer_trade(trade):
    """Insère un trade clôturé dans l'historique. Ne lève jamais — un échec
    d'enregistrement ne doit pas faire planter le cycle de trading."""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO trades
                    (marche, direction, resultat, prix_entree, prix_sortie,
                     stop_loss, objectif, mise, gain, capital_apres,
                     duree_minutes, score, adx, atr, rsi)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                trade.get("marche"),
                trade.get("direction"),
                trade.get("resultat"),
                trade.get("prix_entree"),
                trade.get("prix_sortie"),
                trade.get("stop_loss"),
                trade.get("objectif"),
                trade.get("mise"),
                trade.get("gain"),
                trade.get("capital_apres"),
                trade.get("duree_minutes"),
                trade.get("score"),
                trade.get("adx"),
                trade.get("atr"),
                trade.get("rsi"),
            ))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.error(f"  [DB] Échec enregistrement trade {trade.get('marche')} : {e}")
