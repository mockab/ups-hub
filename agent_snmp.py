#!/usr/bin/env python3
"""
agent_snmp.py  –  Multi-UPS SNMP Agent
Runs on the hub VM (or any host with network access to the UPS cards).
Polls any number of APC UPS devices via SNMP in parallel and POSTs
each one to the hub as a separate agent — hub and dashboard need no changes.

Compatible with NMC1 (AP9617/AP9618/AP9619) where UPS data is available,
NMC2 (AP9630/AP9631), and NMC3 (AP9640/AP9641).

Install:
  pip install pysnmp requests --break-system-packages

Run:
  python3 agent_snmp.py

Add as many entries to DEVICES as you like.
"""

import time, threading, requests
from pysnmp.hlapi import (
    getCmd, SnmpEngine, CommunityData, UdpTransportTarget,
    ContextData, ObjectType, ObjectIdentity
)

# ── Global config ─────────────────────────────────────────────────────────────
HUB_URL      = 'http://localhost:8000/api/report'
POLL_SECONDS = 10
SNMP_TIMEOUT = 3
SNMP_RETRIES = 2

# ── Device list — add one dict per UPS ───────────────────────────────────────
# Each entry becomes a separate agent on the hub dashboard.
# Fields:
#   agent_id  : unique slug (no spaces), shown in hub DB and dashboard
#   label     : human name shown on dashboard cards
#   location  : physical location label (optional)
#   host      : IP or hostname of the NMC card
#   community : SNMP community string (default 'public')
#   port      : SNMP port (default 161)

DEVICES = [
    {
        'agent_id':  'srt5000',
        'label':     'Smart-UPS SRT 5000',
        'location':  'Garage',
        'host':      '10.0.0.122',
        'community': 'public',
        'port':      161,
    },
    # Add more devices here, e.g.:
    # {
    #     'agent_id':  'office-smt1500',
    #     'label':     'Smart-UPS SMT 1500',
    #     'location':  'Office Rack',
    #     'host':      '10.0.0.10',
    #     'community': 'public',
    #     'port':      161,
    # },
]

# ── APC PowerNet MIB OIDs ─────────────────────────────────────────────────────
OIDS = {
    'model':       '1.3.6.1.4.1.318.1.1.1.1.1.1.0',  # STRING
    'sku':         '1.3.6.1.4.1.318.1.1.1.1.1.2.0',  # STRING
    'firmware':    '1.3.6.1.4.1.318.1.1.1.1.2.1.0',  # STRING
    'serial':      '1.3.6.1.4.1.318.1.1.1.1.2.3.0',  # STRING
    'batt_status': '1.3.6.1.4.1.318.1.1.1.2.1.1.0',  # INTEGER 1=unknown 2=normal 3=low 4=fault
    'batt_charge': '1.3.6.1.4.1.318.1.1.1.2.2.1.0',  # Gauge32 %
    'batt_volt':   '1.3.6.1.4.1.318.1.1.1.2.2.8.0',  # Gauge32 0.1V
    'batt_temp':   '1.3.6.1.4.1.318.1.1.1.2.2.2.0',  # Gauge32 °C
    'runtime':     '1.3.6.1.4.1.318.1.1.1.2.2.3.0',  # TimeTicks 1/100s
    'input_volt':  '1.3.6.1.4.1.318.1.1.1.3.2.1.0',  # Gauge32 V
    'input_freq':  '1.3.6.1.4.1.318.1.1.1.3.2.4.0',  # Gauge32 0.1Hz
    'output_volt': '1.3.6.1.4.1.318.1.1.1.4.2.1.0',  # Gauge32 V
    'output_freq': '1.3.6.1.4.1.318.1.1.1.4.2.2.0',  # Gauge32 0.1Hz
    'output_load': '1.3.6.1.4.1.318.1.1.1.4.2.3.0',  # Gauge32 %
    'output_amps': '1.3.6.1.4.1.318.1.1.1.4.2.4.0',  # Gauge32 0.1A
    'output_watt': '1.3.6.1.4.1.318.1.1.1.4.2.8.0',  # Gauge32 W
    'status_bits': '1.3.6.1.4.1.318.1.1.1.11.1.1.0', # STRING bitfield
}

# ── Status bit decoder ────────────────────────────────────────────────────────
# Bit positions (0-indexed from left of the 64-char bitstring)
STATUS_BIT_MAP = {
    0:  'OL',    # on line / mains present
    1:  'OB',    # on battery
    2:  'LB',    # low battery
    3:  'FSD',   # forced shutdown
    7:  'OVER',  # overload
    8:  'TRIM',  # SmartTrim
    9:  'BOOST', # SmartBoost
    12: 'CAL',   # calibration
}

def decode_status(bits_str):
    if not bits_str or not isinstance(bits_str, str):
        return 'OL'
    active = [label for pos, label in STATUS_BIT_MAP.items()
              if pos < len(bits_str) and bits_str[pos] == '1']
    return ' '.join(active) if active else 'OL'

# ── SNMP fetch ────────────────────────────────────────────────────────────────
def snmp_get(host, community, port, oids_dict):
    """
    Fetch all OIDs in a single SNMP GET request.
    Returns {name: value} with Python native types.
    Silently drops OIDs that return noSuchObject/noSuchInstance.
    """
    engine    = SnmpEngine()
    auth      = CommunityData(community, mpModel=0)  # 0 = SNMPv1
    transport = UdpTransportTarget(
        (host, port),
        timeout=SNMP_TIMEOUT,
        retries=SNMP_RETRIES,
    )
    ctx          = ContextData()
    names        = list(oids_dict.keys())
    object_types = [ObjectType(ObjectIdentity(oid)) for oid in oids_dict.values()]

    errorIndication, errorStatus, _, varBinds = next(
        getCmd(engine, auth, transport, ctx, *object_types)
    )

    if errorIndication:
        raise ConnectionError(str(errorIndication))

    result = {}
    for i, vb in enumerate(varBinds):
        cls = type(vb[1]).__name__
        if 'NoSuch' in cls or 'Unspecified' in cls:
            continue
        try:
            result[names[i]] = int(vb[1])
        except (TypeError, ValueError):
            result[names[i]] = str(vb[1])

    return result

# ── Build hub payload from raw SNMP values ────────────────────────────────────
def build_payload(device, raw):
    charge    = raw.get('batt_charge')
    load      = raw.get('output_load')
    input_v   = raw.get('input_volt')
    output_v  = raw.get('output_volt')
    runtime_s = int(raw.get('runtime', 0)) // 100       # timeticks → seconds
    batt_v    = (raw.get('batt_volt')   or 0)
    input_hz  = (raw.get('input_freq')  or 0) / 10      # 0.1Hz → Hz
    output_a  = (raw.get('output_amps') or 0) / 10      # 0.1A → A
    output_w  = raw.get('output_watt')
    batt_temp = raw.get('batt_temp')
    status    = decode_status(raw.get('status_bits', ''))
    model     = raw.get('model') or device['label']

    nut_raw = {k: v for k, v in {
        'device.model':        model,
        'device.serial':       str(raw.get('serial', '')),
        'ups.firmware':        str(raw.get('firmware', '')),
        'ups.status':          status,
        'battery.charge':      str(charge),
        'battery.voltage':     f'{batt_v:.1f}',
        'battery.temperature': str(batt_temp) if batt_temp else None,
        'battery.runtime':     str(runtime_s),
        'input.voltage':       str(input_v),
        'input.frequency':     f'{input_hz:.1f}',
        'output.voltage':      str(output_v),
        'output.current':      f'{output_a:.1f}',
        'ups.load':            str(load),
        'ups.realpower':       str(output_w) if output_w else None,
    }.items() if v is not None}

    return {
        'agent_id':  device['agent_id'],
        'label':     device['label'],
        'location':  device.get('location'),
        'ts':        int(time.time() * 1000),
        'charge':    float(charge)   if charge   is not None else None,
        'load':      float(load)     if load     is not None else None,
        'input_v':   float(input_v)  if input_v  is not None else None,
        'output_v':  float(output_v) if output_v is not None else None,
        'batt_v':    batt_v          if batt_v   else None,
        'runtime_s': runtime_s,
        'input_freq':input_hz        if input_hz else None,
        'output_a':  output_a        if output_a else None,
        'status':    status,
        'ups_model': model,
        'raw':       nut_raw,
    }

# ── HTTP POST to hub ──────────────────────────────────────────────────────────
def post(payload, retries=3):
    for attempt in range(retries):
        try:
            r = requests.post(HUB_URL, json=payload, timeout=5)
            r.raise_for_status()
            return True
        except requests.exceptions.ConnectionError:
            if attempt == 0:
                print(f'[post] Hub unreachable at {HUB_URL}')
        except Exception as e:
            print(f'[post] {e}')
        time.sleep(2 ** attempt)
    return False

# ── Per-device polling loop (runs in its own thread) ─────────────────────────
def device_loop(device):
    label     = device['label']
    host      = device['host']
    community = device.get('community', 'public')
    port      = device.get('port', 161)
    tag       = f'[{device["agent_id"]}]'

    print(f'{tag} Starting — polling {host} every {POLL_SECONDS}s')

    # Announce on startup
    try:
        info = snmp_get(host, community, port, {'model': OIDS['model'], 'sku': OIDS['sku']})
        print(f'{tag} Connected: {info.get("model", "?")} {info.get("sku", "")}')
    except Exception as e:
        print(f'{tag} Warning: {e} — will keep retrying')

    failures = 0
    while True:
        try:
            raw     = snmp_get(host, community, port, OIDS)
            payload = build_payload(device, raw)
            ok      = post(payload)
            failures = 0

            charge  = payload.get('charge', '?')
            load    = payload.get('load',   '?')
            rt      = payload.get('runtime_s', 0)
            status  = payload.get('status', '?')
            print(f'[{time.strftime("%H:%M:%S")}] {tag} {status}  '
                  f'charge={charge}%  load={load}%  '
                  f'runtime={rt//60}min  '
                  f'→ {"ok" if ok else "FAILED"}')

        except ConnectionError as e:
            failures += 1
            print(f'[{time.strftime("%H:%M:%S")}] {tag} SNMP error (x{failures}): {e}')
            # Back off up to 60s on repeated failures
            time.sleep(min(POLL_SECONDS * failures, 60))
            continue

        except Exception as e:
            failures += 1
            print(f'[{time.strftime("%H:%M:%S")}] {tag} Unexpected error (x{failures}): {e}')

        time.sleep(POLL_SECONDS)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not DEVICES:
        print('ERROR: No devices configured in DEVICES list.')
        return

    print(f'[snmp-agent] Starting with {len(DEVICES)} device(s)')
    print(f'[snmp-agent] Reporting to {HUB_URL}')
    print()

    threads = []
    for device in DEVICES:
        t = threading.Thread(
            target=device_loop,
            args=(device,),
            name=device['agent_id'],
            daemon=True,
        )
        t.start()
        threads.append(t)
        time.sleep(0.5)  # stagger starts slightly to avoid SNMP collisions

    # Keep main thread alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print('\n[snmp-agent] Shutting down')

if __name__ == '__main__':
    main()


# ── Systemd unit ──────────────────────────────────────────────────────────────
# Save as /etc/systemd/system/ups-agent-snmp.service
#
# [Unit]
# Description=UPS SNMP Agent (multi-device)
# After=network.target ups-hub.service
# Wants=ups-hub.service
#
# [Service]
# Type=simple
# User=ubuntu
# WorkingDirectory=/opt/ups-hub
# ExecStart=/usr/bin/python3 /opt/ups-hub/agent_snmp.py
# Restart=always
# RestartSec=10
# StandardOutput=journal
# StandardError=journal
#
# [Install]
# WantedBy=multi-user.target
