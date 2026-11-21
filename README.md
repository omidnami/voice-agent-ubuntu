# Ubuntu Voice Agent

A personal tray assistant for **Ubuntu 24.04 / GNOME**: starts after desktop login,
waits locally for **«اوبونتو»**, then opens Gemini Live. Ending the call, silence or
an unsuccessful connection returns it to local standby. No audio/transcript files
are saved and there is no Google connection while waiting for the wake word.

## Install / update (once)

Quit the old app from its tray menu first. From the repository directory:

```bash
git pull --ff-only origin main
bash install.sh
```

For a new clone, use `git clone https://github.com/omidnami/voice-agent-ubuntu.git`
first. Run the installer as your normal desktop user; only apt uses sudo. It:

- installs the tray packages, PulseAudio utilities and PipeWire WebRTC AEC library;
- installs apt's `python3-socks` **before pip** so SOCKS-only networks can bootstrap;
- preserves the existing `~/.config/voice-agent-ubuntu/config.ini` and API key;
- downloads/validates the offline Persian Vosk model if absent;
- updates the GNOME autostart entry and launches the tray app in the current desktop.

Enter the Gemini key once if this is a fresh install. Following desktop logins need
no terminal, proxy exports, module-loading commands or manual start. The clone's
absolute path is recorded in autostart; rerun the installer after moving it.
The launcher restarts crashes with a bounded delay; choosing **خروج** stops it
until the next login/manual launch. A lock prevents duplicate instances.

## Sound / echo cancellation

The app creates/reuses its own `module-echo-cancel` instance with WebRTC AEC:

- `parec` captures **only** `voice_agent_aec_mic`, at 16 kHz mono PCM16;
- `pacat` plays **only** into `voice_agent_aec_speaker`, at 24 kHz;
- the canceller receives that playback as its reference and feeds the selected
  physical speaker, removing its echo from captured microphone audio;
- capture stays open during playback, allowing speech to interrupt the model;
- monitor sources and other echo-cancel modules are not selected as physical mics;
- global device defaults, volume and other applications' modules are not changed.

If AEC or the microphone isn't ready at login, the icon shows a waiting state and
retries automatically. Lost audio, changed default devices and resume are checked
and recovered; it does **not** silently fall back to unprocessed microphone input.
A model interruption flushes both queued Python audio and the PulseAudio playback
buffer. On normal exit only this app's own AEC instance is unloaded.

Actual acoustic suppression depends on hardware, volume, clipping and the room.
AEC routing can be tested without a Google call; **laptop speaker/microphone tests
are still required** before treating residual echo as resolved. The app does not
mute the microphone to disguise echo. Other applications playing directly to the
physical speaker are not in this app's AEC reference.

## Configuration

Existing files work without adding sections. Optional settings:

```ini
[gemini]
api_key = YOUR_EXISTING_KEY
model = gemini-3.8-live
instruction = فارسی، طبیعی و کوتاه جواب بده.
idle_timeout = 60

[network]
proxy = auto
connect_timeout = 20

[audio]
source =
sink =
```

`proxy=auto` uses inherited proxy variables, otherwise GNOME's manual proxy
settings. `socks://` is normalized to `socks5h://`, with HTTPX and WebSocket SOCKS
support installed. A specific proxy can be set once here; no port is guessed.
`proxy=direct` explicitly disables proxy use for this app. PAC scripts aren't
interpreted: use a concrete proxy URL if your desktop uses PAC without proxy env
variables. Do not put credentials in the repository.

Blank audio values choose available physical default devices. For explicit
selection use exact names from `.venv/bin/python main.py --check`.
Google model access, quota and connectivity still depend on your own API key and
network; the app does not change the configured model or buy API credit.

## Use and acceptance check

1. Login: the microphone icon should reach **آماده‌باش — بگو: اوبونتو**.
2. Say **«اوبونتو»**: partial and final local recognition can activate it. Wait for
   the recording icon before asking a question. The connection phase is bounded
   (20 seconds by default), and Stop works even while connecting.
3. Ask for a spoken response using the laptop speakers. The model should not
   respond to its own voice. Speak during its reply to test interruption.
4. End the call: local standby resumes. Mute closes the capture stream; unmute
   recovers it. Repeat after a logout/login and a suspend/resume.

Vosk remains an offline general speech recognizer, not a separately trained
wake-word model: recognition and false wakes need testing with your accent.
A failed Google connection is shown in the tray and logged, then the wake word
can be used again; there is no background Google retry/billing loop.

## Diagnostics and tests

The tray menu has **نمایش گزارش**. One local check (no Google connection):

```bash
.venv/bin/python main.py --check
tail -n 60 ~/.local/state/voice-agent-ubuntu/agent.log
```

Logs rotate and redact API keys/proxy credentials. `launcher.log` records early
startup/crash output. After a manual tray Quit, start with:

```bash
.venv/bin/python launcher.py
```

Tests run headlessly with fake audio/Google transports; they do not prove acoustic
performance on a physical laptop:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

The app still has no function calling or terminal-execution tools.
