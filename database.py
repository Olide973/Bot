"""
Gestion PostgreSQL - Bot Trading OLIDE973
"""

import pg8000
import os
import json
import logging
import re
from datetime import datetime

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get('DATABASE_URL', '')

def get_connection():
    try:
        url = DATABASE_URL
        pattern = r'postgres(?:ql)?://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)'
        match = re.match(pattern, url)
        if not match:
            raise Exception(f"URL invalide : {url}")
        user, password, host, port, database = match.groups()
        conn = pg8000.connect(
            host=host,
            port=int(port),
            database=database,
            user=user,
            password=password,
            ssl_context=True
        )
        return conn
    except Exception as e:
        log.error(f"Erreur connexion PostgreSQL : {e}")
        return None

def init_database():
    conn = get_connection()
    if conn is None:
        log.warning("PostgreSQL non disponible - mode JSON")
        return
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                capital REAL DEFAULT 1000.0,
                cumul_net REAL DEFAULT 0.0,
                total_gagne REAL DEFAULT 0.0,
                total_perdu REAL DEFAULT 0.0,
                nb_trades INTEGER DEFAULT 0,
                nb_wins INTEGER DEFAULT 0,
                nb_losses INTEGER DEFAULT 0,
                nb_skips INTEGER DEFAULT 0,
                pertes_consecutives INTEGER DEFAULT 0,
                avg_win_pct REAL DEFAULT 0.0,
                avg_loss_pct REAL DEFAULT 0.0,
                pause_until INTEGER DEFAULT 0,
                historique TEXT DEFAULT '[]',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_history (
                id SERIAL PRIMARY KEY,
                marche VARCHAR(20),
                direction VARCHAR(10),
                resultat VARCHAR(10),
                prix_entree REAL,
                prix_sortie REAL,
                stop_loss REAL,
                objectif REAL,
                mise REAL,
                gain REAL,
                capital_apres REAL,
                duree_minutes INTEGER,
                score REAL,
                adx REAL,
                atr REAL,
                rsi REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('SELECT COUNT(*) FROM bot_state')
        count = cursor.fetchone()[0]
        if count == 0:
            cursor.execute('INSERT INTO bot_state (id, capital) VALUES (1, 1000.0)')
        conn.commit()
        log.info("Base PostgreSQL initialisee")
    except Exception as e:
        log.error(f"Erreur init database : {e}")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

def charger_etat():
    conn = get_connection()
    if conn is None:
        return _etat_defaut()
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM bot_state WHERE id = 1')
        row = cursor.fetchone()
        if row is None:
            return _etat_defaut()
        colonnes = [
            'id', 'capital', 'cumul_net', 'total_gagne', 'total_perdu',
            'nb_trades', 'nb_wins', 'nb_losses', 'nb_skips',
            'pertes_consecutives', 'avg_win_pct', 'avg_loss_pct',
            'pause_until', 'historique', 'updated_at'
        ]
        etat = dict(zip(colonnes, row))
        etat['historique'] = json.loads(etat.get('historique') or '[]')
        return etat
    except Exception as e:
        log.error(f"Erreur chargement etat : {e}")
        return _etat_defaut()
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

def sauvegarder_etat(etat):
    conn = get_connection()
    if conn is None:
        return
    cursor = None
    try:
        cursor = conn.cursor()
        historique_json = json.dumps(etat.get('historique', [])[-20:], ensure_ascii=False)
        cursor.execute('''
            UPDATE bot_state SET
                capital = %s,
                cumul_net = %s,
                total_gagne = %s,
                total_perdu = %s,
                nb_trades = %s,
                nb_wins = %s,
                nb_losses = %s,
                nb_skips = %s,
                pertes_consecutives = %s,
                avg_win_pct = %s,
                avg_loss_pct = %s,
                pause_until = %s,
                historique = %s,
                updated_at = %s
            WHERE id = 1
        ''', (
            etat['capital'], etat['cumul_net'],
            etat['total_gagne'], etat['total_perdu'],
            etat['nb_trades'], etat['nb_wins'],
            etat['nb_losses'], etat.get('nb_skips', 0),
            etat['pertes_consecutives'],
            etat.get('avg_win_pct', 0), etat.get('avg_loss_pct', 0),
            etat.get('pause_until', 0),
            historique_json,
            datetime.now()
        ))
        conn.commit()
    except Exception as e:
        log.error(f"Erreur sauvegarde etat : {e}")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

def enregistrer_trade(trade):
    conn = get_connection()
    if conn is None:
        return
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO trade_history (
                marche, direction, resultat,
                prix_entree, prix_sortie, stop_loss, objectif,
                mise, gain, capital_apres, duree_minutes,
                score, adx, atr, rsi
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ''', (
            trade.get('marche'), trade.get('direction'), trade.get('resultat'),
            trade.get('prix_entree'), trade.get('prix_sortie'),
            trade.get('stop_loss'), trade.get('objectif'),
            trade.get('mise'), trade.get('gain'),
            trade.get('capital_apres'), trade.get('duree_minutes'),
            trade.get('score'), trade.get('adx'),
            trade.get('atr'), trade.get('rsi')
        ))
        conn.commit()
    except Exception as e:
        log.error(f"Erreur enregistrement trade : {e}")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

def _etat_defaut():
    return {
        'capital': 1000.0,
        'cumul_net': 0.0,
        'total_gagne': 0.0,
        'total_perdu': 0.0,
        'nb_trades': 0,
        'nb_wins': 0,
        'nb_losses': 0,
        'nb_skips': 0,
        'pertes_consecutives': 0,
        'avg_win_pct': 0.0,
        'avg_loss_pct': 0.0,
        'pause_until': 0,
        'historique': []
    }
