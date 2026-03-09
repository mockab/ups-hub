#!/usr/bin/env python3
"""
agent.py  –  UPS Monitor Agent
Runs on any host with NUT installed.  Polls upsc every POLL_SECONDS
and POSTs a telemetry payload to the hub.

Works for:
  - Raspberry Pi Zero running nut (APC Back-UPS 850)
  - UniFi device running nut server  (query via TCP)
  - Any other host with upsc available

Install:
  pip install requests

Configure the three constants below, then run:
  python3 agent.py

Systemd unit is at the bottom of this file as a comment.
"""

import subprocess, time, os, json, socket
import requests

# ── Agent config (edit these) ─────────────────────────────────────────────────
HUB_URL      = 'http://localhost:8000/api/report'  # hub IP:port
AGENT_ID     = 'office-ups'          # unique slug, no spaces
AGENT_LABEL  = 'Office Unifi UPS'      # human-readable name shown in dashboard
AGENT_LOC    = 'Office'       # physical location (optional)
UPS_NAME     = 'office-ups'              # name in /etc/nut/ups.conf
UPS_HOST     = '10.0.0.10'           # 'localhost' for local NUT, or IP for remote
UPS_PORT     = 3493                  # NUT default port

POLL_SECONDS = 10
RETRY_DELAY  = 30   # seconds to wait before retrying after a failed upsc
# ─────────────────────────────────────────────────────────────────────────────


def upsc():
    """Query NUT and return dict of key→value strings."""
    target = f'{UPS_NAME}@{UPS_HOST}:{UPS_PORT}' if UPS_PORT != 3493 else f'{UPS_NAME}@{UPS_HOST}'
    try:
        r = subprocess.run(
            ['upsc', target],
            capture_output=True, text=True, timeout=8
        )
        data = {}
        for line in r.stdout.splitlines():
            if ': ' in line:
                k, v = line.split(': ', 1)
                data[k.strip()] = v.strip()
        return data if data else None
    except Exception as e:
        print(f'[upsc] {e}')
        return None


def gf(d, k):
    try:    return float(d[k])
    except: return None

def gi(d, k):
    try:    return int(float(d[k]))
    except: return None


def build_payload(d):
    model = d.get('device.model') or d.get('ups.model') or ''
    mfr   = d.get('device.mfr')   or d.get('ups.mfr')   or ''
    return {
        'agent_id':  AGENT_ID,
        'label':     AGENT_LABEL,
        'location':  AGENT_LOC,
        'ts':        int(time.time() * 1000),
        'charge':    gf(d, 'battery.charge'),
        'load':      gf(d, 'ups.load'),
        'input_v':   gf(d, 'input.voltage'),
        'output_v':  gf(d, 'output.voltage'),
        'batt_v':    gf(d, 'battery.voltage'),
        'runtime_s': gi(d, 'battery.runtime'),
        'input_freq':gf(d, 'input.frequency'),
        'output_a':  gf(d, 'output.current'),
        'status':    d.get('ups.status', ''),
        'ups_model': f'{mfr} {model}'.strip() or None,
        'raw':       d,
    }


def post(payload, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.post(HUB_URL, json=payload, timeout=5)
            resp.raise_for_status()
            return True
        except requests.exceptions.ConnectionError:
            if attempt == 0:
                print(f'[post] Hub unreachable at {HUB_URL}, will retry')
        except Exception as e:
            print(f'[post] Error: {e}')
        time.sleep(2 ** attempt)
    return False


def run():
    print(f'[agent] {AGENT_ID} ({AGENT_LABEL}) starting')
    print(f'[agent] Polling {UPS_NAME}@{UPS_HOST} every {POLL_SECONDS}s')
    print(f'[agent] Reporting to {HUB_URL}')

    consecutive_failures = 0
    while True:
        d = upsc()
        if d:
            consecutive_failures = 0
            payload = build_payload(d)
            ok = post(payload)
            status = d.get('ups.status', '?')
            charge = d.get('battery.charge', '?')
            if ok:
                print(f'[{time.strftime("%H:%M:%S")}] {status} {charge}% → hub ok')
            else:
                print(f'[{time.strftime("%H:%M:%S")}] {status} {charge}% → hub FAILED')
        else:
            consecutive_failures += 1
            print(f'[{time.strftime("%H:%M:%S")}] upsc failed (x{consecutive_failures})')
            if consecutive_failures >= 3:
                time.sleep(RETRY_DELAY)
                continue

        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    run()


# ── Systemd unit (save as /etc/systemd/system/ups-agent.service) ──────────────
# [Unit]
# Description=UPS Monitor Agent
# After=network.target nut-server.service
# Wants=nut-server.service
#
# [Service]
# Type=simple
# User=pi
# WorkingDirectory=/home/pi/ups-agent
# ExecStart=/usr/bin/python3 /home/pi/ups-agent/agent.py
# Restart=always
# RestartSec=10
# StandardOutput=journal
# StandardError=journal
#
# [Install]
# WantedBy=multi-user.target
