"""
╔══════════════════════════════════════════════════════════════════╗
║   MODULE BASE DE DONNÉES — REIVAX284 V4 OKX (PostgreSQL/Railway) ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import logging
import psycopg2
from psycopg2.extras import Json

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_connection():
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_database():
    """Crée les tables si elles n'existent pas encore."""
    if not DATABASE_URL:
        log.warning("DATABASE_URL non défini — base de données désactivée")
        return
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS etat_bot (
                id INTEGER PRIMARY KEY DEFAULT 1,
                data JSONB NOT NULL,
                maj_le TIMESTAMP DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id SERIAL PRIMARY KEY,
                marche VARCHAR(20),
                direction VARCHAR(10),
                resultat VARCHAR(10),
                prix_entree DOUBLE PRECISION,
                prix_sortie DOUBLE PRECISION,
                stop_loss DOUBLE PRECISION,
                objectif DOUBLE PRECISION,
                mise DOUBLE PRECISION,
                gain DOUBLE PRECISION,
                capital_apres DOUBLE PRECISION,
                duree_minutes INTEGER,
                score DOUBLE PRECISION,
                adx DOUBLE PRECISION,
                atr DOUBLE PRECISION,
                rsi DOUBLE PRECISION,
                cree_le TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        log.info("  Base de données initialisée (etat_bot, trades)")
    except Exception as e:
        log.error(f"Erreur init_database : {e}")


def charger_etat():
    """Charge l'état global depuis la base. Retourne {} si absent ou en cas d'erreur."""
    if not DATABASE_URL:
        return {}
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT data FROM etat_bot WHERE id = 1;")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row and row[0]:
            return row[0]
        return {}
    except Exception as e:
        log.error(f"Erreur charger_etat : {e}")
        return {}


def sauvegarder_etat(etat):
    """Sauvegarde (upsert) l'état global complet en base."""
    if not DATABASE_URL:
        return
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO etat_bot (id, data, maj_le)
            VALUES (1, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, maj_le = NOW();
        """, (Json(etat),))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        log.error(f"Erreur sauvegarder_etat : {e}")


def enregistrer_trade(trade):
    """Enregistre un trade clôturé dans la table trades (historique brut)."""
    if not DATABASE_URL:
        return
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO trades (
                marche, direction, resultat, prix_entree, prix_sortie,
                stop_loss, objectif, mise, gain, capital_apres,
                duree_minutes, score, adx, atr, rsi
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
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
        cur.close()
        conn.close()
    except Exception as e:
        log.error(f"Erreur enregistrer_trade : {e}")
