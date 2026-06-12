#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────────
# One-shot setup for running the IQ Option bot on a fresh Ubuntu VM
# (Google Cloud Compute Engine, Hostinger VPS, etc.)
#
# Usage — from inside the cloned repo:
#     bash deploy/setup.sh
#
# It installs Python + deps, adds swap (helps on 1GB VMs), sets the
# timezone, and registers a systemd service so the bot runs 24/7 and
# restarts on crash/reboot. It does NOT start the bot — you create the
# .env credentials file first, then `sudo systemctl start iqbot`.
# ───────────────────────────────────────────────────────────────────
set -euo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(whoami)"

echo ">> Project dir : $PROJ"
echo ">> Run as user : $USER_NAME"

echo ">> Installing system packages (python3, venv, pip, git)..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

echo ">> Setting timezone to Asia/Bangkok..."
sudo timedatectl set-timezone Asia/Bangkok || true

# Swap — prevents out-of-memory on small VMs (e.g. GCP e2-micro 1GB). Harmless on bigger VMs.
if ! sudo swapon --show 2>/dev/null | grep -q '/swapfile'; then
  echo ">> Adding 2GB swap file..."
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
  echo ">> Swap already present, skipping."
fi

echo ">> Creating Python venv and installing dependencies..."
cd "$PROJ"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo ">> Writing systemd service (iqbot.service)..."
sudo tee /etc/systemd/system/iqbot.service >/dev/null <<EOF
[Unit]
Description=IQ Option AI Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$PROJ
ExecStart=$PROJ/venv/bin/python $PROJ/main.py
Restart=always
RestartSec=10
Environment=TZ=Asia/Bangkok

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable iqbot

cat <<DONE

============================================================
 ✅ Setup complete.

 NEXT STEPS:
   1) Create your credentials file:
        cp .env.example .env
        nano .env            # fill IQ_EMAIL / IQ_PASSWORD / TG_TOKEN / TG_CHAT_ID

   2) Start the bot:
        sudo systemctl start iqbot

   3) Watch live logs:
        journalctl -u iqbot -f

 Useful:
   sudo systemctl status iqbot      # is it running?
   sudo systemctl restart iqbot     # restart after editing .env
   sudo systemctl stop iqbot        # stop
============================================================
DONE
