#!/usr/bin/env python3
"""
hub.py  –  UPS Monitor Hub
Ubuntu 24 VM.  Receives telemetry from all agents, stores to SQLite,
serves REST API consumed by the dashboard.

Install:
  pip install fastapi uvicorn

Run:
  python3 hub.py
  # or for production:
  uvicorn hub:app --host 0.0.0.0 --port 8000 --workers 1
"""

import sqlite3, time, os, threading
from datetime  import datetime, timezone
from typing    import Optional
from fastapi   import FastAPI, HTTPException, Query
from fastapi.responses  import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic  import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
DB_PATH      = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ups_hub.db')
MAX_DAYS     = 30
STALE_SECS   = 60   # mark agent offline after this many seconds with no data

app = FastAPI(title='UPS Hub', version='1.0')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    return db

def init_db():
    with get_db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS agents (
            agent_id    TEXT PRIMARY KEY,
            label       TEXT,
            location    TEXT,
            ups_model   TEXT,
            last_seen   INTEGER,
            last_status TEXT
        )''')
        db.execute('''CREATE TABLE IF NOT EXISTS samples (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id    TEXT    NOT NULL,
            ts          INTEGER NOT NULL,
            charge      REAL,
            load        REAL,
            input_v     REAL,
            output_v    REAL,
            batt_v      REAL,
            runtime_s   INTEGER,
            input_freq  REAL,
            output_a    REAL,
            status      TEXT,
            raw         TEXT    -- full JSON of all NUT vars
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_samples_agent_ts ON samples(agent_id, ts)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_samples_ts       ON samples(ts)')
        db.execute('''CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id    TEXT    NOT NULL,
            ts          INTEGER NOT NULL,
            status_from TEXT,
            status_to   TEXT,
            cls         TEXT,
            msg         TEXT
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS idx_events_agent_ts ON events(agent_id, ts)')
        db.execute('CREATE INDEX IF NOT EXISTS idx_events_ts       ON events(ts)')
        db.commit()
    print(f'[db] Ready at {DB_PATH}')

# ── In-memory status cache (agent_id → last known status string) ─────────────
_last_status: dict = {}

def record_event(db, agent_id: str, label: str, new_status: str, ts: int):
    """Write a status-transition event to DB if status changed."""
    prev = _last_status.get(agent_id)
    _last_status[agent_id] = new_status
    if prev is None or prev == new_status:
        return   # no change or first-seen

    cls = 'blue'
    msg = f'Status → {new_status}'
    if 'OB' in new_status:
        cls, msg = 'yellow', '⚡ On battery'
    if 'LB' in new_status:
        cls, msg = 'red',    '⚠ Low battery!'
    if 'OL' in new_status and 'OB' not in new_status and prev and 'OB' in prev:
        cls, msg = 'green',  '✓ Mains restored'
    if 'FSD' in new_status:
        cls, msg = 'red',    '🔴 Forced shutdown'
    if 'OVER' in new_status:
        cls, msg = 'red',    '⚠ Overload!'

    db.execute(
        'INSERT INTO events(agent_id,ts,status_from,status_to,cls,msg) VALUES(?,?,?,?,?,?)',
        (agent_id, ts, prev, new_status, cls, msg)
    )

# ── Prune old data (runs in background) ──────────────────────────────────────
def pruner():
    while True:
        time.sleep(3600)
        cutoff = int(time.time() * 1000) - MAX_DAYS * 86400 * 1000
        try:
            with get_db() as db:
                db.execute('DELETE FROM samples WHERE ts < ?', (cutoff,))
                db.commit()
            print('[pruner] Old samples removed')
        except Exception as e:
            print(f'[pruner] {e}')

# ── Pydantic models ───────────────────────────────────────────────────────────
class TelemetryPayload(BaseModel):
    agent_id:  str              # unique slug e.g. "closet-apc", "unifi-ups", "office-apc"
    label:     str              # human name e.g. "Closet APC 850"
    location:  Optional[str] = None
    ts:        Optional[int] = None   # unix ms; hub fills in if missing
    # Core metrics
    charge:    Optional[float] = None
    load:      Optional[float] = None
    input_v:   Optional[float] = None
    output_v:  Optional[float] = None
    batt_v:    Optional[float] = None
    runtime_s: Optional[int]   = None
    input_freq:Optional[float] = None
    output_a:  Optional[float] = None
    status:    Optional[str]   = None
    ups_model: Optional[str]   = None
    raw:       Optional[dict]  = None  # full upsc dict

# ── Routes ────────────────────────────────────────────────────────────────────

@app.post('/api/report')
def report(payload: TelemetryPayload):
    """Agents POST here every 10 seconds."""
    ts = payload.ts or int(time.time() * 1000)
    import json

    with get_db() as db:
        # Upsert agent record
        db.execute('''INSERT INTO agents(agent_id, label, location, ups_model, last_seen, last_status)
                      VALUES (?,?,?,?,?,?)
                      ON CONFLICT(agent_id) DO UPDATE SET
                        label=excluded.label,
                        location=excluded.location,
                        ups_model=excluded.ups_model,
                        last_seen=excluded.last_seen,
                        last_status=excluded.last_status''',
                   (payload.agent_id, payload.label, payload.location,
                    payload.ups_model, ts, payload.status))

        # Insert sample
        db.execute('''INSERT INTO samples
                      (agent_id,ts,charge,load,input_v,output_v,batt_v,runtime_s,input_freq,output_a,status,raw)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (payload.agent_id, ts,
                    payload.charge, payload.load,
                    payload.input_v, payload.output_v, payload.batt_v,
                    payload.runtime_s, payload.input_freq, payload.output_a,
                    payload.status,
                    json.dumps(payload.raw) if payload.raw else None))
        record_event(db, payload.agent_id, payload.label, payload.status or '', ts)
        db.commit()

    return {'ok': True, 'ts': ts}


@app.get('/api/agents')
def list_agents():
    """Returns all known agents with their current status."""
    now = int(time.time() * 1000)
    db  = get_db()
    rows = db.execute('SELECT * FROM agents ORDER BY label').fetchall()
    db.close()
    result = []
    for r in rows:
        d = dict(r)
        d['online'] = (now - (r['last_seen'] or 0)) < STALE_SECS * 1000
        result.append(d)
    return result


@app.get('/api/agents/{agent_id}/history')
def agent_history(
    agent_id: str,
    hours: int = Query(24, ge=1, le=720),
):
    """Returns thinned historical samples for one agent."""
    cutoff = int(time.time() * 1000) - hours * 3600 * 1000
    db     = get_db()

    total = db.execute(
        'SELECT COUNT(*) FROM samples WHERE agent_id=? AND ts>=?',
        (agent_id, cutoff)
    ).fetchone()[0]

    step = max(1, total // 4000)   # cap at ~4000 points

    rows = db.execute(
        '''SELECT ts,charge,load,input_v,output_v,batt_v,runtime_s,status
           FROM samples WHERE agent_id=? AND ts>=? ORDER BY ts''',
        (agent_id, cutoff)
    ).fetchall()
    db.close()

    thinned = rows[::step]
    return {
        'agent_id': agent_id,
        'hours':    hours,
        'step_s':   10 * step,
        'count':    len(thinned),
        'samples':  [dict(r) for r in thinned],
    }


@app.get('/api/agents/{agent_id}/stats')
def agent_stats(agent_id: str):
    """Aggregate min/max/avg for one agent over all stored data."""
    db  = get_db()
    row = db.execute('''SELECT
        COUNT(*)            total,
        MIN(ts)             oldest,
        MAX(ts)             newest,
        ROUND(MIN(charge),1) min_charge, ROUND(MAX(charge),1) max_charge, ROUND(AVG(charge),1) avg_charge,
        ROUND(MIN(input_v),1) min_v,     ROUND(MAX(input_v),1) max_v,
        ROUND(MIN(load),1)   min_load,   ROUND(MAX(load),1)   max_load,   ROUND(AVG(load),1) avg_load
        FROM samples WHERE agent_id=?''', (agent_id,)).fetchone()
    db.close()
    return dict(row)


@app.get('/api/summary')
def summary():
    """Latest reading for every agent — used by dashboard overview."""
    db   = get_db()
    agents = db.execute('SELECT * FROM agents ORDER BY label').fetchall()
    now  = int(time.time() * 1000)
    result = []
    for a in agents:
        latest = db.execute(
            '''SELECT * FROM samples WHERE agent_id=? ORDER BY ts DESC LIMIT 1''',
            (a['agent_id'],)
        ).fetchone()
        d = dict(a)
        d['online']  = (now - (a['last_seen'] or 0)) < STALE_SECS * 1000
        d['latest']  = dict(latest) if latest else None
        result.append(d)
    db.close()
    return result


@app.get('/api/events')
def global_events(
    limit: int = Query(200, ge=1, le=2000),
    since: int = Query(0),           # unix ms — return events newer than this
):
    """All events across all agents, newest first."""
    db   = get_db()
    rows = db.execute(
        '''SELECT e.*, a.label FROM events e
           LEFT JOIN agents a ON a.agent_id = e.agent_id
           WHERE e.ts > ?
           ORDER BY e.ts DESC LIMIT ?''',
        (since, limit)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get('/api/agents/{agent_id}/events')
def agent_events(
    agent_id: str,
    limit: int = Query(100, ge=1, le=1000),
    since: int = Query(0),
):
    """Events for a single agent, newest first."""
    db   = get_db()
    rows = db.execute(
        '''SELECT * FROM events WHERE agent_id=? AND ts > ?
           ORDER BY ts DESC LIMIT ?''',
        (agent_id, since, limit)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get('/')
def dashboard():
    """Serve the dashboard HTML."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dashboard.html')
    if os.path.exists(path):
        return FileResponse(path, media_type='text/html')
    return JSONResponse({'error': 'dashboard.html not found next to hub.py'}, status_code=404)


# ── Boot ──────────────────────────────────────────────────────────────────────
init_db()

# Seed _last_status from the most recent sample per agent so we don't
# generate spurious events on hub restart.
def _seed_status_cache():
    db = get_db()
    rows = db.execute(
        '''SELECT agent_id, status FROM samples
           WHERE ts = (SELECT MAX(ts) FROM samples s2 WHERE s2.agent_id = samples.agent_id)
           GROUP BY agent_id'''
    ).fetchall()
    db.close()
    for r in rows:
        _last_status[r['agent_id']] = r['status'] or ''
    if rows:
        print(f'[db] Seeded status cache for {len(rows)} agent(s)')

_seed_status_cache()
threading.Thread(target=pruner, daemon=True).start()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('hub:app', host='0.0.0.0', port=8000, reload=False)
