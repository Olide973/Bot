# ═══════════════════════════════════════════════════════════════
#  database.py — persistance PostgreSQL (Railway) pour bot_trading-48.py
#  Reconstruit le 09/07/2026 à partir de la structure d'origine
#  (mai 2026) : pg8000, DATABASE_URL lue DANS get_connection()
#  (correctif de mai — jamais au niveau module), tables etat_bot
#  et trades. Interface identique à l'original :
#    init_database(), charger_etat(), sauvegarder_etat(etat),
#    enregistrer_trade(trade)
#
#  ── CORRECTIF 11/07/2026 : bug d'EFFACEMENT de l'historique ──────
#  Avant, charger_etat() renvoyait {} sur N'IMPORTE QUELLE erreur (y
#  compris un simple aléa de connexion au démarrage, fréquent sur
#  Railway quand la base n'est pas encore prête). Le bot croyait alors
#  à un premier démarrage, peuplait ses valeurs par défaut et les
#  RÉ-ENREGISTRAIT — écrasant définitivement l'historique réel.
#  Deux changements :
#   1) get_connection() retente une connexion transitoirement échouée.
#   2) charger_etat() LÈVE si la lecture échoue, et ne renvoie {} QUE
#      si la base est réellement vide (aucune ligne). L'appelant ne
#      doit jamais écraser un état existant sur une simple erreur.
# ═══════════════════════════════════════════════════════════════
import os
import json
import time
import logging
from urllib.parse import urlparse

import pg8000

log = logging.getLogger(__name__)

# Nombre de tentatives et pause (secondes) pour absorber les coupures
# transitoires de connexion PostgreSQL (redémarrage Railway, base pas
# encore prête, blip réseau).
_DB_TENTATIVES = 4
_DB_PAUSE_SEC = 2


def get_connection():
    """Ouvre une connexion PostgreSQL à partir de DATABASE_URL, avec retries.

    La variable est lue ICI (pas au niveau module) — correctif de mai 2026 :
    Railway injecte parfois la variable après l'import du module, la lire à
    l'import figeait une valeur vide.

    Retente une connexion transitoirement échouée (_DB_TENTATIVES) plutôt que
    d'échouer au premier blip — sans ça, une coupure d'une seconde au démarrage
    faisait remonter une "base vide" et effaçait l'historique."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL absente des variables d'environnement")
    p = urlparse(url)
    derniere_exc = None
    for tentative in range(_DB_TENTATIVES):
        try:
            return pg8000.connect(
                user=p.username,
                password=p.password,
                host=p.hostname,
                port=p.port or 5432,
                database=(p.path or "/postgres").lstrip("/"),
            )
        except Exception as e:
            derniere_exc = e
            if tentative < _DB_TENTATIVES - 1:
                log.warning(f"  [DB] Connexion échouée (tentative {tentative + 1}/"
                            f"{_DB_TENTATIVES}) : {e} — nouvelle tentative dans {_DB_PAUSE_SEC}s")
                time.sleep(_DB_PAUSE_SEC)
    raise RuntimeError(
        f"Connexion PostgreSQL impossible après {_DB_TENTATIVES} tentatives : {derniere_exc}"
    )


def init_database():
    """Crée les tables si absentes (idempotent — ne touche jamais aux données
    existantes)."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS etat_bot (
                id     INTEGER PRIMARY KEY DEFAULT 1,
                data   TEXT NOT NULL,
                maj_le TIMESTAMP DEFAULT NOW()
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
    """Charge l'état complet du bot (dict).

    IMPORTANT (correctif 11/07) — distingue deux cas radicalement différents :
      • base RÉELLEMENT vide (aucune ligne id=1) → retourne {}  : c'est un vrai
        premier démarrage, le bot peuple ses valeurs par défaut, c'est correct.
      • lecture IMPOSSIBLE (DB injoignable même après retries) → LÈVE une
        exception. Ne JAMAIS renvoyer {} dans ce cas : l'appelant écraserait
        l'historique réel par un état vide (c'était le bug qui effaçait tout).

    L'appelant (bot_trading-48.py) attrape l'exception au démarrage et REFUSE
    d'écraser l'état plutôt que de repartir de zéro."""
    conn = get_connection()  # lève (avec retries internes) si la base est injoignable
    try:
        cur = conn.cursor()
        cur.execute("SELECT data FROM etat_bot WHERE id = 1")
        row = cur.fetchone()
        if row and row[0]:
            data = row[0]
            # ── CORRECTIF (12/07) — cause exacte du crash en boucle : la colonne
            # etat_bot.data est en réalité de type JSONB (créée avant cette
            # reconstruction du fichier), et pg8000 la renvoie déjà décodée en
            # dict Python, pas en texte. json.loads(dict) levait "the JSON
            # object must be str, bytes or bytearray, not dict" à CHAQUE
            # tentative → 6 échecs → SystemExit → boucle de crash sans fin,
            # aucun trade depuis 9h. On accepte les deux formats (JSONB déjà
            # décodé, ou TEXT à parser) selon ce que la colonne renvoie
            # réellement, pour ne plus dépendre du type exact de la colonne.
            if isinstance(data, (dict, list)):
                return data
            return json.loads(data)
        return {}  # base réellement vide (aucune ligne) — vrai premier démarrage
    finally:
        conn.close()


def sauvegarder_etat(etat):
    """Sauvegarde l'état complet du bot (upsert sur la ligne unique id=1).
    Ne lève jamais : un échec de sauvegarde ne doit pas interrompre le
    trading — l'erreur est loggée (et remontée sur Telegram via le miroir
    d'erreurs du bot). get_connection() retente déjà les coupures passagères."""
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO etat_bot (id, data, maj_le) VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, maj_le = NOW()
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
