# Ubuntu Voice Agent

A small personal voice assistant for **Ubuntu 24.04 / GNOME**. It lives in the top bar, listens locally for **«اوبونتو»**, and connects **directly to Google Gemini Live** only when activated. No TurnX server, database, browser, web backend or desktop window.

## Install

```bash
git clone https://github.com/omidnami/voice-agent-ubuntu.git
cd voice-agent-ubuntu
bash install.sh
nano ~/.config/voice-agent-ubuntu/config.ini  # set api_key
.venv/bin/python main.py
```

The installer creates a Python virtual environment, installs the Ubuntu tray/audio packages and downloads a ~53 MB **Vosk Persian** model into `~/.local/share/voice-agent-ubuntu/model`. It also installs `~/.config/autostart/voice-agent-ubuntu.desktop` so the tray app starts after logging in. **Do not move the clone without rerunning the installer** (autostart contains its path).

Get a Gemini API key from Google AI Studio. The config file is user-owned, not committed:

```ini
[gemini]
api_key = YOUR_GOOGLE_API_KEY
model = gemini-3.8-live
instruction = تو دستیار صوتی شخصی من هستی. فارسی، طبیعی و کوتاه جواب بده.
```

## Use

- Say **«اوبونتو»** and wait for the tray icon to switch to the active state, then speak normally.
- Or use the tray menu **شروع مکالمه** (manual fallback if wake recognition is inaccurate).
- Use **پایان مکالمه** to disconnect; after ~60 seconds without detected speech/response, the session also ends automatically.
- **خاموش‌کردن میکروفون** closes the capture device; turn it back on from the tray menu.
- Voice input remains local **until activation**; during the call, 16 kHz PCM is sent to Google and 24 kHz PCM is played back. The app does not save audio or transcripts.

This is a first version: Vosk is a general Persian speech recognizer, **not a trained, reliable wake-word detector**; recognition/false activation must be tested with your microphone and accent. Headphones improve echo handling. Gemini access, quota, cost and regional availability depend on your own API key/network.

### Troubleshooting

```bash
tail -f ~/.local/state/voice-agent-ubuntu/agent.log
```

If the icon is missing, ensure Ubuntu AppIndicator support is enabled in GNOME and that your session is graphical. If microphone capture fails, inspect your PipeWire device/permissions. If Gemini fails, check the key, network and model access.

This app intentionally does **not** run terminal commands or use function calling yet.
