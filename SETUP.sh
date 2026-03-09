# UPS Hub — Setup Guide
# Ubuntu 24 VM

# ── 1. Install dependencies ───────────────────────────────────────────────────
sudo apt update
sudo apt install python3-pip nut-client -y   # nut-client gives us upsc for the remote agent
pip3 install fastapi uvicorn requests --break-system-packages

# ── 2. Place files ────────────────────────────────────────────────────────────
sudo mkdir -p /opt/ups-hub
sudo cp hub.py dashboard.html /opt/ups-hub/
sudo chown -R $USER:$USER /opt/ups-hub

# ── 3. Systemd unit for hub ───────────────────────────────────────────────────
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
sudo systemctl enable ups-hub
sudo systemctl start  ups-hub
sudo systemctl status ups-hub

# Dashboard is now at:  http://<hub-ip>:8000/

# ── 4. If running the UniFi agent ON the hub ──────────────────────────────────
# (because UniFi device can't run Python)
# Copy agent.py to /opt/ups-hub/agent_unifi.py and edit:
#   AGENT_ID   = 'unifi-ups'
#   AGENT_LABEL= 'UniFi UPS'
#   UPS_HOST   = '192.168.1.X'   ← UniFi device LAN IP
#   HUB_URL    = 'http://localhost:8000/api/report'

sudo tee /etc/systemd/system/ups-agent-unifi.service << 'EOF'
[Unit]
Description=UPS Agent - UniFi (remote query)
After=network.target ups-hub.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/ups-hub
ExecStart=/usr/bin/python3 /opt/ups-hub/agent_unifi.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ups-agent-unifi
sudo systemctl start  ups-agent-unifi

# ── 5. On each Pi / NUT host (agents 1 and 3) ────────────────────────────────
# Copy agent.py, edit the config block at the top, then:

mkdir -p /home/pi/ups-agent
cp agent.py /home/pi/ups-agent/

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
sudo systemctl enable ups-agent
sudo systemctl start  ups-agent
sudo journalctl -u ups-agent -f

# ── 6. Finding your UniFi UPS name ───────────────────────────────────────────
# From any host that can reach the UniFi device:
upsc -l 192.168.1.X        # list UPS names
upsc default@192.168.1.X   # query it (replace 'default' with actual name)

# ── 7. Verify hub is receiving data ──────────────────────────────────────────
curl http://localhost:8000/api/agents
curl http://localhost:8000/api/summary

# ── 8. Firewall (if needed) ───────────────────────────────────────────────────
sudo ufw allow 8000/tcp comment 'UPS Hub dashboard + API'
# Agents only need outbound TCP to hub:8000 — no inbound rules needed on agentsv
