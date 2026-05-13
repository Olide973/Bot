```
ModuleNotFoundError: No module named 'database'
File "/app/bot_trading-48.py", line 19, in <module>
from database import init_database, charger_etat, sauvegarder_etat, enregistrer_trade
```

Le bot cherche un fichier **`database.py`** qui n'existe **pas** dans ton repo Railway. Il y a 2 possibilités :

1. Tu as renommé ton fichier bot en `bot_trading-48.py` mais le fichier `database.py` n'est pas dans le même dossier
2. Tu n'as jamais créé le fichier `database.py` (celui pour PostgreSQL)

---

## ✅ Solution rapide — 2 options

### 🅰️ OPTION A (RECOMMANDÉE) — Créer le fichier `database.py`

Crée un fichier **`database.py`** à la racine de ton repo (au même endroit que `bot_trading-48.py`) avec ce contenu :

```python
"""database.py — Persistance PostgreSQL Railway"""
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
                    CONSTRAINT single CHECK (id = 1)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trade_history (
                    id SERIAL PRIMARY KEY,
                    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
                    marche VARCHAR(20), direction VARCHAR(10), resultat VARCHAR(10),
                    prix_entree NUMERIC(20,8), prix_sortie NUMERIC(20,8),
                    stop_loss NUMERIC(20,8), objectif NUMERIC(20,8),
                    mise NUMERIC(15,2), gain NUMERIC(15,2),
                    capital_apres NUMERIC(15,2), duree_minutes INTEGER,
                    score INTEGER, adx NUMERIC(6,2),
                    atr NUMERIC(20,8), rsi NUMERIC(6,2)
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
            for k in ['capital','total_gagne','total_perdu','cumul_net','avg_win_pct','avg_loss_pct']:
                if etat.get(k) is not None:
                    etat[k] = float(etat[k])
            cur.execute("SELECT * FROM trade_history ORDER BY timestamp DESC LIMIT 5")
            rows = cur.fetchall()
            etat['historique'] = [{
                'heure': h['timestamp'].strftime('%Y-%m-%d %H:%M'),
                'marche': h['marche'], 'direction': h['direction'],
                'resultat': h['resultat'], 'gain': float(h['gain']),
                'mise': float(h['mise']), 'capital': float(h['capital_apres'])
            } for h in reversed(rows)]
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
                    capital=%s, total_gagne=%s, total_perdu=%s, cumul_net=%s,
                    nb_trades=%s, nb_wins=%s, nb_losses=%s, nb_skips=%s,
                    pertes_consecutives=%s, avg_win_pct=%s, avg_loss_pct=%s,
                    pause_until=%s
                WHERE id = 1
            """, (
                etat.get('capital',215.0), etat.get('total_gagne',0), etat.get('total_perdu',0),
                etat.get('cumul_net',0), etat.get('nb_trades',0), etat.get('nb_wins',0),
                etat.get('nb_losses',0), etat.get('nb_skips',0),
                etat.get('pertes_consecutives',0), etat.get('avg_win_pct',0),
                etat.get('avg_loss_pct',0), int(etat.get('pause_until',0)),
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
                    marche, direction, resultat, prix_entree, prix_sortie,
                    stop_loss, objectif, mise, gain, capital_apres,
                    duree_minutes, score, adx, atr, rsi
                ) VALUES (
                    %(marche)s, %(direction)s, %(resultat)s, %(prix_entree)s, %(prix_sortie)s,
                    %(stop_loss)s, %(objectif)s, %(mise)s, %(gain)s, %(capital_apres)s,
                    %(duree_minutes)s, %(score)s, %(adx)s, %(atr)s, %(rsi)s
                )
            """, data)
            conn.commit()
    finally:
        conn.close()
```

---

### Étape 2 — Vérifie ton `requirements.txt`

Il doit contenir :

```txt
requests==2.31.0
pandas==2.2.3
numpy==1.26.4
ta==0.11.0
psycopg2-binary==2.9.9
```

---

### Étape 3 — Vérifie que PostgreSQL est lié sur Railway

1. Dashboard Railway → ton projet
2. Vérifie qu'il y a un service **PostgreSQL** à côté du service `Bot`
3. Sur le service `Bot` → **Variables** → tu dois voir `DATABASE_URL`

❌ Si tu ne vois pas PostgreSQL :
- Clic **"+ New"** → **Database** → **PostgreSQL**
- Railway ajoute automatiquement `DATABASE_URL` à ton service Bot

---

### Étape 4 — Push

```bash
git add database.py requirements.txt
git commit -m "Add database.py for V7.5 persistence"
git push
```

Railway redéploie. Tu devrais voir :
```
Starting Container
DEMARRAGE V7.5 — 2026-05-13 ...
```

Au lieu de l'erreur `ModuleNotFoundError`.

---

## 🅱️ OPTION B (rapide mais sans persistance) — Stub temporaire

Si tu veux juste **tester rapidement** sans PostgreSQL, crée un `database.py` minimaliste :

```python
"""database.py — Stub temporaire (PAS de persistance)"""

_etat = {
    "capital": 215.0, "total_gagne": 0.0, "total_perdu": 0.0,
    "cumul_net": 0.0, "nb_trades": 0, "nb_wins": 0, "nb_losses": 0,
    "nb_skips": 0, "pertes_consecutives": 0,
    "avg_win_pct": 0.0, "avg_loss_pct": 0.0,
    "pause_until": 0, "historique": []
}

def init_database():
    pass

def charger_etat():
    return _etat

def sauvegarder_etat(etat):
    _etat.update(etat)

def enregistrer_trade(data):
    pass
```
