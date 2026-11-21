#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
ROOT="$PWD"
CONFIG="$HOME/.config/voice-agent-ubuntu"
DATA="$HOME/.local/share/voice-agent-ubuntu"
AUTOSTART="$HOME/.config/autostart"

sudo apt-get update
sudo apt-get install -y python3-venv python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 libportaudio2 curl unzip
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p "$CONFIG" "$DATA" "$AUTOSTART"
if [[ ! -f "$CONFIG/config.ini" ]]; then
  cat > "$CONFIG/config.ini" <<'INI'
[gemini]
api_key =
model = gemini-3.8-live
instruction = تو دستیار صوتی شخصی من هستی. فارسی، طبیعی و کوتاه جواب بده.
INI
  chmod 600 "$CONFIG/config.ini"
fi
if [[ ! -d "$DATA/model" ]]; then
  echo 'Downloading small Persian wake-word model (~53 MB)...'
  TEMP="$(mktemp -d)"
  trap 'rm -rf "$TEMP"' EXIT
  curl -fLsS --retry 3 -o "$TEMP/model.zip" 'https://alphacephei.com/vosk/models/vosk-model-small-fa-0.42.zip'
  unzip -q "$TEMP/model.zip" -d "$TEMP"
  mv "$TEMP/vosk-model-small-fa-0.42" "$DATA/model"
  rm -rf "$TEMP"
  trap - EXIT
fi
cat > "$AUTOSTART/voice-agent-ubuntu.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Ubuntu Voice Agent
Comment=Offline wake word + Google Gemini Live
Exec=$ROOT/.venv/bin/python $ROOT/main.py
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
chmod +x install.sh
printf '\nInstalled. Set your API key in: %s/config.ini\n' "$CONFIG"
printf 'Start now: %q %q\n' "$ROOT/.venv/bin/python" "$ROOT/main.py"
