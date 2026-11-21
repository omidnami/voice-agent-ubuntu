#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
if [[ "$EUID" -eq 0 ]]; then
  echo 'Run bash install.sh as your normal desktop user (not sudo). It asks for sudo for apt only.' >&2
  exit 1
fi
sudo apt-get update
sudo apt-get install -y python3-venv python3-gi python3-socks \
  gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
  pulseaudio-utils libspa-0.2-modules curl
python3 -m venv --system-site-packages .venv
.venv/bin/python bootstrap.py dependencies
.venv/bin/python bootstrap.py finish
