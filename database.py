import os
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.environ.get('DATABASE_URL')


def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_database():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    capital NUMERIC(15,2) NOT NULL DEFAULT 215.0,
                    total_gagne NUMERIC(15,2) NOT NULL DEFAULT 0,
                    total_perdu NUMERIC(15,2) NOT NULL DEFAULT 0,
                    cumul_net NUMERIC(15,2) NOT NULL DEFAULT 0,
                    nb_trades INTEGER NOT NULL DEFAULT 0,
                    nb_wins INTEGER NOT NULL DEFAULT 0,
                    nb_losses INTEGER NOT NULL DEFAULT 0,
                    nb_skips INTEGER NOT NULL DEFAULT 0,
                    pertes_consecutives INTEGER NOT NULL DEFAULT 0,
                    avg_win_pct NUMERIC(10,4) NOT NULL DEFAULT 0,
                    avg_loss_pct NUMERIC(10,4) NOT NULL DEFAULT 0,
                    pause_until BIGINT NOT NULL DEFAULT 0,
                    CONSTRAINT single_row CHECK (id = 1)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
                    marche VARCHAR(20),
                    direction VARCHAR(10),
                    resultat VARCHAR(10),
                    prix_entree NUMERIC(20,8),
                    prix_sortie NUMERIC(20,8),
                    stop_loss NUMERIC(20,8),
                    objectif NUMERIC(20,8),
                    mise NUMERIC(15,2),
                    gain NUMERIC(15,2),
                    capital_apres NUMERIC(15,2),
                    duree_minutes INTEGER,
                    score INTEGER,
                    adx NUMERIC(6,2),
                    atr NUMERIC(20,8),
                    rsi NUMERIC(6,2)
                )
            """)
            cur.execute("INSERT INTO bot_state (id) VALUES (1) ON CONFLICT DO NOTHING")
            conn.commit()
    finally:
        conn.close()


def charger_etat():
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM bot_state WHERE id = 1")
            row = cur.fetchone()
            if not row:
                init_database()
                return charger_etat()
            etat = dict(row)
            for k in ['capital', 'total_gagne', 'total_perdu', 'cumul_net',
                      'avg_win_pct', 'avg_loss_pct']:
                if etat.get(k) is not None:
                    etat[k] = float(etat[k])
            cur.execute("SELECT * FROM trade_history ORDER BY timestamp DESC LIMIT 5")
            rows = cur.fetchall()
            etat['historique'] = [
                {
                    'heure':     h['timestamp'].strftime('%Y-%m-%d %H:%M'),
                    'marche':    h['marche'],
                    'direction': h['direction'],
                    'resultat':  h['resultat'],
                    'gain':      float(h['gain']) if h['gain'] is not None else 0,
                    'mise':      float(h['mise']) if h['mise'] is not None else 0,
                    'capital':   float(h['capital_apres']) if h['capital_apres'] is not None else 0
                }
                for h in reversed(rows)
            ]
            etat.pop('id', None)
            return etat
    finally:
        conn.close()


def sauvegarder_etat(etat):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE bot_state SET
                    capital = %s,
                    total_gagne = %s,
                    total_perdu = %s,
                    cumul_net = %s,
                    nb_trades = %s,
                    nb_wins = %s,
                    nb_losses = %s,
                    nb_skips = %s,
                    pertes_consecutives = %s,
                    avg_win_pct = %s,
                    avg_loss_pct = %s,
                    pause_until = %s
                WHERE id = 1
            """, (
                etat.get('capital', 215.0),
                etat.get('total_gagne', 0),
                etat.get('total_perdu', 0),
                etat.get('cumul_net', 0),
                etat.get('nb_trades', 0),
                etat.get('nb_wins', 0),
                etat.get('nb_losses', 0),
                etat.get('nb_skips', 0),
                etat.get('pertes_consecutives', 0),
                etat.get('avg_win_pct', 0),
                etat.get('avg_loss_pct', 0),
                int(etat.get('pause_until', 0)),
            ))
            conn.commit()
    finally:
        conn.close()


def enregistrer_trade(data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trade_history (
                    marche, direction, resultat,
                    prix_entree, prix_sortie, stop_loss, objectif,
                    mise, gain, capital_apres, duree_minutes,
                    score, adx, atr, rsi
                ) VALUES (
                    %(marche)s, %(direction)s, %(resultat)s,
                    %(prix_entree)s, %(prix_sortie)s, %(stop_loss)s, %(objectif)s,
                    %(mise)s, %(gain)s, %(capital_apres)s, %(duree_minutes)s,
                    %(score)s, %(adx)s, %(atr)s, %(rsi)s
                )
            """, data)
            conn.commit()
    finally:
        conn.close()
