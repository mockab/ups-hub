**Vibe Coded**

# UPS Hub

A self-hosted UPS monitoring system built around a hub/agent architecture. A central hub VM collects telemetry from multiple UPS devices, stores 30 days of history in SQLite, and serves a real-time dashboard.

Agents run on any host with NUT installed, or the hub itself can query APC UPS devices directly over SNMP — no agent process required on the UPS hardware.

```
┌─────────────────────────────────────────────────────────────┐
│                        Hub VM (Ubuntu 24)                   │
│                                                             │
│   hub.py  ──  FastAPI + SQLite  ──  dashboard.html          │
│      ▲               ▲                                      │
│      │               │                                      │
│  POST /api/report   agent_snmp.py (runs here, queries       │
│      │               APC NMC cards over SNMP)               │
└──────┼──────────────────────────────────────────────────────┘
       │
       ├── Raspberry Pi Zero  (NUT + usbhid-ups → APC Back-UPS 850)
       │     agent.py  →  upsc apcups@localhost
       │
       ├── UniFi device       (built-in NUT server)
       │     agent.py  →  upsc default@<unifi-ip>
       │
       └── APC Smart-UPS + NMC2/NMC3  (SNMP)
             agent_snmp.py  →  SNMP GET 10.x.x.x
```

---

## Features

- **Multi-UPS dashboard** — overview cards for all devices, click any card for full detail view
- **Live metrics** — battery charge, load %, input voltage, runtime remaining, battery voltage, updated every 10 seconds
- **30-day history** — all samples stored in SQLite on the hub, ring-buffer in the browser for fast rendering
- **Interactive charts** — 5 charts per UPS with hover crosshair tooltip showing exact timestamp and value, time windows from 15 minutes to 30 days
- **Persistent events** — status transitions (on battery, mains restored, low battery, overload) detected server-side and stored in the DB, survive hub restarts and page refreshes
- **Per-device and global event log** — global log across all UPS on the main page, per-device log in the detail panel
- **All-time statistics** — charge range/avg, voltage range, load range/avg per device
- **Raw NUT data** — collapsible table of every variable exposed by the UPS driver
- **Battery gauge** — visual charge indicator with colour coding (green → yellow → red)
- **SNMP support** — native APC PowerNet MIB polling for Smart-UPS with NMC2/NMC3 cards, multiple devices from one agent process
- **NUT support** — works with any UPS driver NUT supports, local or remote
- **Offline detection** — cards go red after 60 seconds without a report
- **Zero dependencies on dashboard** — single HTML file, no build step, no npm

---

## Hardware Compatibility

### NUT agents (`agent.py`)

Any UPS that NUT supports. Tested with:

| Device | Driver | Notes |
|---|---|---|
| APC Back-UPS 850 (BE850G2) | `usbhid-ups` | USB OTG on Pi Zero |
| APC Back-UPS ES 850G2 | `usbhid-ups` | USB |
| UniFi UPS (ULTE-UPS, UUPS) | built-in NUT server | query over TCP |
| Any NUT-supported UPS | various | see [NUT compatibility list](https://www.networkupstools.org/stable-hcl.html) |

### SNMP agents (`agent_snmp.py`)

APC Smart-UPS with a network management card in the SmartSlot:

| Card | Generation | Notes |
|---|---|---|
| AP9630 / AP9631 | NMC2 | Recommended. AP9631 adds environmental ports |
| AP9640 / AP9641 | NMC3 | Newest, longest support life |
| AP9617 / AP9618 / AP9619 | NMC1 | **Only if** UPS model is on the [NMC1 compatibility list](https://www.se.com/ca/en/faqs/FA237786/) |

> **Note:** The AP9618 is also an environmental monitor (temperature sensors, dry contacts). That data is not currently collected by the SNMP agent but the OIDs are under `1.3.6.1.4.1.318.1.1.10`.

---

## File Overview

| File | Purpose | Where it runs |
|---|---|---|
| `hub.py` | FastAPI hub server, SQLite storage, REST API | Ubuntu 24 hub VM |
| `dashboard.html` | Single-file web dashboard, served by hub | Browser |
| `agent.py` | NUT-based agent, one copy per NUT host | Each Pi / NUT device |
| `agent_snmp.py` | SNMP agent, polls multiple APC UPS over network | Hub VM (or any host) |
| `patch_persistent_events.py` | One-time patch script to add persistent events | Run once on hub VM |
| `SETUP.sh` | Step-by-step setup commands | Reference / run manually |

---

## Hub Setup (Ubuntu 24 VM)

### 1. Install dependencies

```bash
sudo apt update
sudo apt install python3-pip nut-client -y
pip3 install fastapi uvicorn requests --break-system-packages
```

`nut-client` provides `upsc` so the hub can query remote NUT servers directly if needed (e.g. UniFi UPS agent running on the hub itself).

### 2. Place files

```bash
sudo mkdir -p /opt/ups-hub
sudo cp hub.py dashboard.html agent_snmp.py /opt/ups-hub/
sudo chown -R $USER:$USER /opt/ups-hub
```

### 3. Create systemd service

```bash
sudo tee /etc/systemd/system/ups-hub.service << 'EOF'
[Unit]
Description=UPS Monitor Hub
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/ups-hub
ExecStart=/usr/bin/python3 /opt/ups-hub/hub.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ups-hub
```

### 4. Firewall

```bash
sudo ufw allow 8000/tcp comment 'UPS Hub'
```

Dashboard is now at `http://<hub-ip>:8000/`

---

## Agent Setup — NUT (`agent.py`)

Run one copy per host that has NUT installed. Edit the config block at the top of `agent.py`:

```python
HUB_URL      = 'http://192.168.1.100:8000/api/report'   # hub IP
AGENT_ID     = 'closet-apc'          # unique slug, no spaces
AGENT_LABEL  = 'Closet APC 850'      # shown on dashboard
AGENT_LOC    = 'Server Closet'       # physical location
UPS_NAME     = 'apcups'              # name in /etc/nut/ups.conf
UPS_HOST     = 'localhost'           # 'localhost' or remote NUT server IP
UPS_PORT     = 3493
```

### Install on the device

```bash
pip3 install requests --break-system-packages
mkdir -p /home/pi/ups-agent
cp agent.py /home/pi/ups-agent/
```

### Systemd service

```bash
sudo tee /etc/systemd/system/ups-agent.service << 'EOF'
[Unit]
Description=UPS Monitor Agent
After=network.target nut-server.service
Wants=nut-server.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/ups-agent
ExecStart=/usr/bin/python3 /home/pi/ups-agent/agent.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ups-agent
sudo journalctl -u ups-agent -f
```

### UniFi UPS

If the UniFi device can't run Python (most can't), run `agent.py` on the hub VM itself and point it at the UniFi NUT server:

```python
HUB_URL   = 'http://localhost:8000/api/report'
AGENT_ID  = 'unifi-ups'
UPS_NAME  = 'default'          # check with: upsc -l <unifi-ip>
UPS_HOST  = '192.168.1.X'      # UniFi device LAN IP
```

Find the UPS name with:
```bash
upsc -l 192.168.1.X
```

---

## Agent Setup — SNMP (`agent_snmp.py`)

Runs on the hub VM. Polls any number of APC UPS devices in parallel threads. Edit the `DEVICES` list at the top:

```python
HUB_URL      = 'http://localhost:8000/api/report'
POLL_SECONDS = 10

DEVICES = [
    {
        'agent_id':  'rack-srt5000',
        'label':     'Smart-UPS SRT 5000',
        'location':  'Server Rack',
        'host':      '10.0.0.5',
        'community': 'public',
        'port':      161,
    },
    {
        'agent_id':  'office-smt1500',
        'label':     'Smart-UPS SMT 1500',
        'location':  'Office',
        'host':      '10.0.0.6',
        'community': 'public',
        'port':      161,
    },
]
```

Add as many entries as needed. Each becomes a separate card on the dashboard.

### Install dependency

```bash
pip install pysnmp-lextudio requests --break-system-packages
```

> Use `pysnmp-lextudio` — it is the actively maintained fork that keeps the original `pysnmp` API. The original `pysnmp` v6 broke the import names.

### Systemd service

```bash
sudo tee /etc/systemd/system/ups-agent-snmp.service << 'EOF'
[Unit]
Description=UPS SNMP Agent (multi-device)
After=network.target ups-hub.service
Wants=ups-hub.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/ups-hub
ExecStart=/usr/bin/python3 /opt/ups-hub/agent_snmp.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ups-agent-snmp
sudo journalctl -u ups-agent-snmp -f
```

### Verify SNMP is working before deploying

```bash
# Check the UPS responds
snmpwalk -v1 -c public <ups-ip> 1.3.6.1.4.1.318.1.1.1 | head -20

# Check key metrics specifically
snmpget -v1 -c public <ups-ip> \
  1.3.6.1.4.1.318.1.1.1.1.1.1.0 \
  1.3.6.1.4.1.318.1.1.1.2.2.1.0 \
  1.3.6.1.4.1.318.1.1.1.4.2.3.0 \
  1.3.6.1.4.1.318.1.1.1.3.2.1.0
# Returns: model name, battery charge %, load %, input voltage
```

---

## Persistent Events Patch

The base install detects status transitions in the browser session only — they are lost on page refresh. The patch adds server-side detection and DB storage so events survive restarts.

```bash
cd /opt/ups-hub
python3 patch_persistent_events.py .
sudo systemctl restart ups-hub
```

The script patches `hub.py` and `dashboard.html` in-place and creates `.bak_<timestamp>` backups of both before modifying them. It is safe to run on an existing installation — it adds a new `events` table to the SQLite DB without modifying existing tables.

### What the patch adds

**hub.py:**
- `events` table with columns: `agent_id`, `ts`, `status_from`, `status_to`, `cls`, `msg`
- `record_event()` — called on every `/api/report` POST, detects transitions and writes to DB
- Status cache seeded from DB on hub boot so restarts don't generate false events
- `GET /api/events` — global event feed, supports `?since=<unix_ms>&limit=N`
- `GET /api/agents/{id}/events` — per-agent event feed

**dashboard.html:**
- Fetches full event history from hub on first page load
- Polls incrementally every 10s using the last known event timestamp as cursor
- Per-agent events loaded when a detail panel is opened
- Deduplicates events by timestamp so overlapping fetches never show duplicates

---

## REST API Reference

All endpoints served by `hub.py` on port 8000.

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Serves `dashboard.html` |
| `POST` | `/api/report` | Agents POST telemetry here every 10s |
| `GET` | `/api/agents` | All known agents with online status |
| `GET` | `/api/summary` | Latest reading for every agent (used by dashboard) |
| `GET` | `/api/agents/{id}/history` | Historical samples, `?hours=N` (1–720) |
| `GET` | `/api/agents/{id}/stats` | All-time min/max/avg aggregates |
| `GET` | `/api/events` | Global event log, `?since=<ms>&limit=N` |
| `GET` | `/api/agents/{id}/events` | Per-agent event log, `?since=<ms>&limit=N` |

### Example responses

```bash
# All agents
curl http://localhost:8000/api/agents

# Latest state of all UPS (what the dashboard polls)
curl http://localhost:8000/api/summary

# 24 hours of history for one UPS
curl "http://localhost:8000/api/agents/closet-apc/history?hours=24"

# Last 50 events across all UPS
curl "http://localhost:8000/api/events?limit=50"
```

---

## Dashboard Usage

### Overview cards

Each connected UPS gets a card showing charge %, load %, input voltage, runtime remaining, status pill (ON LINE / ON BATT / LOW BATT / OFFLINE), and a sparkline of battery charge over the selected time window. Click any card to open the detail panel.

### Detail panel

Five charts: Battery Charge, UPS Load, Input Voltage, Runtime Remaining, Battery Voltage. Hover over any chart for a crosshair and exact value tooltip. Below the charts: all-time statistics, per-device event log, and a collapsible raw NUT data table.

### Time windows

Buttons in the top bar apply to all charts and sparklines simultaneously: 15M / 1H / 6H / 24H / 7D / 30D.

### Event log

The global event log at the bottom of the page shows status transitions across all UPS, newest first, with colour coding: green = mains restored, yellow = on battery, red = low battery / overload / forced shutdown.

---

## NUT Quick Reference (for Pi Zero / NUT hosts)

```bash
# Check NUT sees the UPS
upsc apcups@localhost

# List all UPS on a remote server
upsc -l 192.168.1.X

# Watch live
watch -n 2 upsc apcups@localhost

# Service status
sudo systemctl status nut-server nut-monitor nut-driver-enumerator

# Logs
sudo journalctl -u nut-server -f
```

### Minimal NUT config for USB UPS

`/etc/nut/nut.conf`
```ini
MODE=standalone
```

`/etc/nut/ups.conf`
```ini
[apcups]
    driver = usbhid-ups
    port = auto
    vendorid = 051d
    productid = 0002
    desc = "APC Back-UPS 850"
```

`/etc/nut/upsd.conf`
```ini
LISTEN 0.0.0.0 3493
```

`/etc/nut/upsd.users`
```ini
[upsmon]
    password = yourpassword
    upsmon master
```

`/etc/nut/upsmon.conf`
```ini
MONITOR apcups@localhost 1 upsmon yourpassword master
MINSUPPLIES 1
SHUTDOWNCMD "/sbin/shutdown -h +0"
POWERDOWNFLAG /etc/killpower
```

---

## Troubleshooting

**Agents not reporting**
```bash
# Check hub is up
curl http://localhost:8000/api/summary

# Check agent logs
sudo journalctl -u ups-agent -n 30 --no-pager
sudo journalctl -u ups-agent-snmp -n 30 --no-pager
```

**`upsc: command not found` on hub VM**
```bash
sudo apt install nut-client -y
```

**`ImportError: cannot import name 'getCmd' from 'pysnmp.hlapi'`**

pysnmp v6 broke the API. Install the maintained fork instead:
```bash
pip install pysnmp-lextudio --break-system-packages
```

**NUT `insufficient power configured`**

The `MONITOR` line is missing or commented out in `/etc/nut/upsmon.conf`. Ensure it reads:
```
MONITOR apcups@localhost 1 upsmon yourpassword master
```

**Dashboard shows stale data after hub restart**

Hard refresh the browser: `Ctrl+Shift+R`. The hub seeds its status cache from the DB on boot so no false events should appear.

**UPS card shows OFFLINE**

The hub marks an agent offline after 60 seconds without a report (`STALE_SECS = 60` in `hub.py`). Check the agent service is running and can reach the hub IP on port 8000.

**AP9618 / NMC1 returns no UPS data**

The AP9618 is an environmental monitor. It only relays UPS data if the connected UPS model is on APC's NMC1 compatibility list. Many newer Smart-UPS models (including the SMX series) require NMC2 (AP9630/AP9631) or NMC3 (AP9640/AP9641).

---

## License

MIT
