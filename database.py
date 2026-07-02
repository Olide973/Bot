"""
╔══════════════════════════════════════════════════════════════════╗
║   MODULE BASE DE DONNÉES — REIVAX284 V4 OKX (PostgreSQL/pg8000)  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import os
import json
import logging
import ssl
from urllib.parse import urlparse
import pg8000.native as pg8000

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def get_connection():
    url = urlparse(DATABASE_URL)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return pg8000.Connection(
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port or 5432,
        database=url.path.lstrip("/"),
        ssl_context=ctx,
    )


def init_database():
    """Crée les tables si elles n'existent pas encore."""
    if not DATABASE_URL:
        log.warning("DATABASE_URL non défini — base de données désactivée")
        return
    try:
        conn = get_connection()
        conn.run("""
            CREATE TABLE IF NOT EXISTS etat_bot (
                id INTEGER PRIMARY KEY DEFAULT 1,
                data JSONB NOT NULL,
                maj_le TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.run("""
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
                frais DOUBLE PRECISION,
                funding DOUBLE PRECISION,
                capital_apres DOUBLE PRECISION,
                duree_minutes INTEGER,
                score DOUBLE PRECISION,
                adx DOUBLE PRECISION,
                atr DOUBLE PRECISION,
                rsi DOUBLE PRECISION,
                cree_le TIMESTAMP DEFAULT NOW()
            );
        """)
        # Migration : ajoute les colonnes ajoutées après la création initiale
        # de la table 'trades' (déploiements précédents)
        conn.run("ALTER TABLE trades ADD COLUMN IF NOT EXISTS frais DOUBLE PRECISION;")
        conn.run("ALTER TABLE trades ADD COLUMN IF NOT EXISTS funding DOUBLE PRECISION;")
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
        rows = conn.run("SELECT data FROM etat_bot WHERE id = 1;")
        conn.close()
        if rows and rows[0][0]:
            return rows[0][0]
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
        conn.run("""
            INSERT INTO etat_bot (id, data, maj_le)
            VALUES (1, :data, NOW())
            ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, maj_le = NOW();
        """, data=json.dumps(etat))
        conn.close()
    except Exception as e:
        log.error(f"Erreur sauvegarder_etat : {e}")


def enregistrer_trade(trade):
    """Enregistre un trade clôturé dans la table trades (historique brut)."""
    if not DATABASE_URL:
        return
    try:
        conn = get_connection()
        conn.run("""
            INSERT INTO trades (
                marche, direction, resultat, prix_entree, prix_sortie,
                stop_loss, objectif, mise, gain, frais, funding, capital_apres,
                duree_minutes, score, adx, atr, rsi
            ) VALUES (
                :marche, :direction, :resultat, :prix_entree, :prix_sortie,
                :stop_loss, :objectif, :mise, :gain, :frais, :funding, :capital_apres,
                :duree_minutes, :score, :adx, :atr, :rsi
            );
        """,
            marche=trade.get("marche"),
            direction=trade.get("direction"),
            resultat=trade.get("resultat"),
            prix_entree=trade.get("prix_entree"),
            prix_sortie=trade.get("prix_sortie"),
            stop_loss=trade.get("stop_loss"),
            objectif=trade.get("objectif"),
            mise=trade.get("mise"),
            gain=trade.get("gain"),
            frais=trade.get("frais"),
            funding=trade.get("funding"),
            capital_apres=trade.get("capital_apres"),
            duree_minutes=trade.get("duree_minutes"),
            score=trade.get("score"),
            adx=trade.get("adx"),
            atr=trade.get("atr"),
            rsi=trade.get("rsi"),
        )
        conn.close()
    except Exception as e:
        log.error(f"Erreur enregistrer_trade : {e}")
