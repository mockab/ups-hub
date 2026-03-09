#!/usr/bin/env python3
"""
patch_persistent_events.py
Adds persistent event logging to hub.py and dashboard.html.

Run from the directory containing hub.py and dashboard.html:
  python3 patch_persistent_events.py

Or point at a specific directory:
  python3 patch_persistent_events.py /opt/ups-hub

What it does:
  hub.py      — adds an `events` table to SQLite, detects status transitions
                on each /api/report POST, writes them to the DB, and exposes
                GET /api/events and GET /api/agents/{id}/events endpoints.
  dashboard.html — on load fetches full event history from the hub, merges
                with any new in-session events, and re-renders persistently.
"""

import sys, os, re, shutil, datetime

# ── Target directory ──────────────────────────────────────────────────────────
TARGET_DIR = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
HUB_PATH   = os.path.join(TARGET_DIR, 'hub.py')
DASH_PATH  = os.path.join(TARGET_DIR, 'dashboard.html')

for p in (HUB_PATH, DASH_PATH):
    if not os.path.exists(p):
        print(f'ERROR: {p} not found.')
        sys.exit(1)

def backup(path):
    ts  = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    dst = path + f'.bak_{ts}'
    shutil.copy2(path, dst)
    print(f'  Backed up → {dst}')

def patch_hub():
    print('\n── Patching hub.py ──────────────────────────────────────────────')
    backup(HUB_PATH)
    src = open(HUB_PATH).read()

    # ── 1. Add events table to init_db ───────────────────────────────────────
    OLD_INIT = "        db.execute('CREATE INDEX IF NOT EXISTS idx_samples_ts       ON samples(ts)')\n        db.commit()\n    print(f'[db] Ready at {DB_PATH}')"
    NEW_INIT = """        db.execute('CREATE INDEX IF NOT EXISTS idx_samples_ts       ON samples(ts)')
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
    print(f'[db] Ready at {DB_PATH}')"""

    if OLD_INIT not in src:
        print('  WARN: init_db block not found — skipping events table creation')
    else:
        src = src.replace(OLD_INIT, NEW_INIT)
        print('  ✓ Added events table to init_db()')

    # ── 2. Add in-memory last_status cache + transition helper after init_db ──
    OLD_PRUNER = "# ── Prune old data (runs in background) ──────────────────────────────────────"
    NEW_PRUNER = """# ── In-memory status cache (agent_id → last known status string) ─────────────
_last_status: dict = {}

def record_event(db, agent_id: str, label: str, new_status: str, ts: int):
    \"\"\"Write a status-transition event to DB if status changed.\"\"\"
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

# ── Prune old data (runs in background) ──────────────────────────────────────"""

    if OLD_PRUNER not in src:
        print('  WARN: pruner anchor not found — skipping status cache insertion')
    else:
        src = src.replace(OLD_PRUNER, NEW_PRUNER)
        print('  ✓ Added _last_status cache and record_event()')

    # ── 3. Call record_event inside /api/report ───────────────────────────────
    OLD_REPORT_END = "        db.commit()\n\n    return {'ok': True, 'ts': ts}"
    NEW_REPORT_END = """        record_event(db, payload.agent_id, payload.label, payload.status or '', ts)
        db.commit()

    return {'ok': True, 'ts': ts}"""

    if OLD_REPORT_END not in src:
        print('  WARN: report commit block not found — skipping record_event call')
    else:
        src = src.replace(OLD_REPORT_END, NEW_REPORT_END)
        print('  ✓ Wired record_event() into /api/report')

    # ── 4. Add /api/events and /api/agents/{id}/events endpoints ─────────────
    OLD_DASHBOARD = "@app.get('/')\ndef dashboard():"
    NEW_EVENTS = """@app.get('/api/events')
def global_events(
    limit: int = Query(200, ge=1, le=2000),
    since: int = Query(0),           # unix ms — return events newer than this
):
    \"\"\"All events across all agents, newest first.\"\"\"
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
    \"\"\"Events for a single agent, newest first.\"\"\"
    db   = get_db()
    rows = db.execute(
        '''SELECT * FROM events WHERE agent_id=? AND ts > ?
           ORDER BY ts DESC LIMIT ?''',
        (agent_id, since, limit)
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


@app.get('/')
def dashboard():"""

    if OLD_DASHBOARD not in src:
        print('  WARN: dashboard route anchor not found — skipping event endpoints')
    else:
        src = src.replace(OLD_DASHBOARD, NEW_EVENTS)
        print('  ✓ Added /api/events and /api/agents/{id}/events endpoints')

    # ── 5. Seed _last_status from DB on boot ─────────────────────────────────
    OLD_BOOT = "init_db()\nthreading.Thread(target=pruner, daemon=True).start()"
    NEW_BOOT = """init_db()

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
threading.Thread(target=pruner, daemon=True).start()"""

    if OLD_BOOT not in src:
        print('  WARN: boot block not found — skipping status cache seeding')
    else:
        src = src.replace(OLD_BOOT, NEW_BOOT)
        print('  ✓ Added status cache seeding on boot')

    open(HUB_PATH, 'w').write(src)
    print('  hub.py written.')


def patch_dashboard():
    print('\n── Patching dashboard.html ──────────────────────────────────────')
    backup(DASH_PATH)
    src = open(DASH_PATH).read()

    # ── 1. Replace in-memory-only events section with persistent version ──────
    OLD_EVENTS_JS = """// ── Events ────────────────────────────────────────────────────────────────────
const lastStatusMap = {};

function checkEvent(id, status, label) {
  const prev = lastStatusMap[id];
  if (prev === undefined) { lastStatusMap[id] = status; return; }
  if (prev === status)    return;
  lastStatusMap[id] = status;

  const t = new Date().toLocaleTimeString();
  let cls = 'blue', msg = `${status}`;
  if (status.includes('OB'))                             { cls='yellow'; msg='⚡ On battery'; }
  if (status.includes('OL') && prev.includes('OB'))     { cls='green';  msg='✓ Mains restored'; }
  if (status.includes('LB'))                             { cls='red';    msg='⚠ Low battery!'; }

  if (!agentEvents[id]) agentEvents[id] = [];
  agentEvents[id].unshift({ time:t, cls, msg });
  if (agentEvents[id].length > 100) agentEvents[id].pop();

  globalEvts.unshift({ time:t, agent:label||id, cls, msg });
  if (globalEvts.length > 200) globalEvts.pop();

  renderGlobalEvents();
  if (selectedAgent === id) renderDetailEvents(id);
}

function renderGlobalEvents() {
  const el = document.getElementById('globalEvents');
  if (!globalEvts.length) { el.innerHTML = '<div class="event-row"><span class="event-time">–</span><span class="event-agent">–</span><div class="event-dot blue"></div><span class="event-msg">No events yet</span></div>'; return; }
  el.innerHTML = globalEvts.slice(0,50).map(e =>
    `<div class="event-row"><span class="event-time">${e.time}</span><span class="event-agent">${e.agent}</span><div class="event-dot ${e.cls}"></div><span class="event-msg">${e.msg}</span></div>`
  ).join('');
}

function renderDetailEvents(id) {
  const el  = document.getElementById('detailEvents');
  const evs = agentEvents[id] || [];
  if (!evs.length) { el.innerHTML = '<div class="event-row"><span class="event-time">–</span><div class="event-dot blue"></div><span class="event-msg">No events yet</span></div>'; return; }
  el.innerHTML = evs.slice(0,50).map(e =>
    `<div class="event-row"><span class="event-time">${e.time}</span><div class="event-dot ${e.cls}"></div><span class="event-msg">${e.msg}</span></div>`
  ).join('');
}"""

    NEW_EVENTS_JS = """// ── Events (persistent — loaded from hub DB on boot) ─────────────────────────
// globalEvts and agentEvents are populated from /api/events on load,
// then new in-session transitions are appended and also stored by the hub.
let eventsBootstrapped = false;
let latestEventTs = 0;  // track newest event ts for incremental polling

async function fetchEvents(since = 0) {
  try {
    const res  = await fetch(`${HUB_BASE}/api/events?limit=500&since=${since}`, { signal: AbortSignal.timeout(4000) });
    if (!res.ok) return;
    const rows = await res.json();   // newest first
    if (!rows.length) return;

    rows.forEach(e => {
      const ts    = e.ts || 0;
      const t     = new Date(ts).toLocaleTimeString();
      const label = e.label || e.agent_id;
      const entry = { time: t, ts, agent: label, cls: e.cls || 'blue', msg: e.msg || e.status_to || '' };

      // Global list (avoid duplicates by ts+agent_id)
      if (!globalEvts.find(x => x.ts === ts && x.agent === label)) {
        globalEvts.push(entry);
        if (ts > latestEventTs) latestEventTs = ts;
      }

      // Per-agent list
      const id = e.agent_id;
      if (!agentEvents[id]) agentEvents[id] = [];
      if (!agentEvents[id].find(x => x.ts === ts)) {
        agentEvents[id].push({ time: t, ts, cls: e.cls || 'blue', msg: e.msg || '' });
      }
    });

    // Sort newest first
    globalEvts.sort((a,b) => b.ts - a.ts);
    Object.values(agentEvents).forEach(arr => arr.sort((a,b) => b.ts - a.ts));

    renderGlobalEvents();
    if (selectedAgent) renderDetailEvents(selectedAgent);
  } catch(e) {
    console.log('[events]', e.message);
  }
}

async function fetchAgentEvents(id, since = 0) {
  try {
    const res  = await fetch(`${HUB_BASE}/api/agents/${id}/events?limit=200&since=${since}`, { signal: AbortSignal.timeout(4000) });
    if (!res.ok) return;
    const rows = await res.json();
    if (!rows.length) return;

    if (!agentEvents[id]) agentEvents[id] = [];
    rows.forEach(e => {
      const ts = e.ts || 0;
      const t  = new Date(ts).toLocaleTimeString();
      if (!agentEvents[id].find(x => x.ts === ts)) {
        agentEvents[id].push({ time: t, ts, cls: e.cls || 'blue', msg: e.msg || '' });
      }
    });
    agentEvents[id].sort((a,b) => b.ts - a.ts);
    if (selectedAgent === id) renderDetailEvents(id);
  } catch(e) {}
}

function renderGlobalEvents() {
  const el = document.getElementById('globalEvents');
  if (!globalEvts.length) {
    el.innerHTML = '<div class="event-row"><span class="event-time">–</span><span class="event-agent">–</span><div class="event-dot blue"></div><span class="event-msg">No events yet</span></div>';
    return;
  }
  el.innerHTML = globalEvts.slice(0,100).map(e =>
    `<div class="event-row">
       <span class="event-time">${e.time}</span>
       <span class="event-agent">${e.agent}</span>
       <div class="event-dot ${e.cls}"></div>
       <span class="event-msg">${e.msg}</span>
     </div>`
  ).join('');
}

function renderDetailEvents(id) {
  const el  = document.getElementById('detailEvents');
  const evs = agentEvents[id] || [];
  if (!evs.length) {
    el.innerHTML = '<div class="event-row"><span class="event-time">–</span><div class="event-dot blue"></div><span class="event-msg">No events yet</span></div>';
    return;
  }
  el.innerHTML = evs.slice(0,100).map(e =>
    `<div class="event-row">
       <span class="event-time">${e.time}</span>
       <div class="event-dot ${e.cls}"></div>
       <span class="event-msg">${e.msg}</span>
     </div>`
  ).join('');
}"""

    if OLD_EVENTS_JS not in src:
        print('  WARN: events JS block not found — skipping events JS replacement')
    else:
        src = src.replace(OLD_EVENTS_JS, NEW_EVENTS_JS)
        print('  ✓ Replaced in-memory events with persistent fetch-from-hub version')

    # ── 2. Fetch events on first poll ─────────────────────────────────────────
    OLD_POLL_END = """    renderOverview(agents);
    if (selectedAgent) renderDetail(selectedAgent);

    const n = agents.length;"""

    NEW_POLL_END = """    renderOverview(agents);
    if (selectedAgent) renderDetail(selectedAgent);

    // Fetch event history once on first successful poll, then only new events
    if (!eventsBootstrapped) {
      eventsBootstrapped = true;
      fetchEvents(0);   // full history (up to 500 events)
    } else {
      fetchEvents(latestEventTs);   // only events since last known
    }

    const n = agents.length;"""

    if OLD_POLL_END not in src:
        print('  WARN: poll end anchor not found — skipping event fetch wiring')
    else:
        src = src.replace(OLD_POLL_END, NEW_POLL_END)
        print('  ✓ Wired event fetching into poll()')

    # ── 3. Fetch per-agent events when detail panel opens ────────────────────
    OLD_SELECT = """  document.getElementById('detailSection').classList.add('visible');

  renderDetail(id);
  fetchAgentHistory(id);
  fetchAgentStats(id);"""

    NEW_SELECT = """  document.getElementById('detailSection').classList.add('visible');

  renderDetail(id);
  fetchAgentHistory(id);
  fetchAgentStats(id);
  fetchAgentEvents(id);   // load full persistent event history for this agent"""

    if OLD_SELECT not in src:
        print('  WARN: selectAgent anchor not found — skipping per-agent event fetch')
    else:
        src = src.replace(OLD_SELECT, NEW_SELECT)
        print('  ✓ Added fetchAgentEvents() call when detail panel opens')

    open(DASH_PATH, 'w').write(src)
    print('  dashboard.html written.')


# ── Run ───────────────────────────────────────────────────────────────────────
print(f'Patching files in: {TARGET_DIR}')
patch_hub()
patch_dashboard()
print('\n✓ Done.')
print()
print('Next steps:')
print('  1. Copy hub.py and dashboard.html to your hub VM (/opt/ups-hub/)')
print('  2. sudo systemctl restart ups-hub')
print('  3. The events table will be created automatically on first start')
print('     (existing ups_hub.db is safe — ALTER-free, new table only)')
