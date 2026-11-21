#!/usr/bin/env python3
"""One-time installer helpers; preserves the user's existing key/config."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from runtime import CONFIG_DIR, DATA_DIR, STATE_DIR, configure_proxy, read_config

ROOT = Path(__file__).resolve().parent
MODEL_URL = 'https://alphacephei.com/vosk/models/vosk-model-small-fa-0.42.zip'


def create_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = CONFIG_DIR / 'config.ini'
    if not path.exists():
        path.write_text('''[gemini]
api_key =
model = gemini-3.8-live
instruction = تو دستیار صوتی شخصی من هستی. فارسی، طبیعی و کوتاه جواب بده.
idle_timeout = 60

[network]
proxy = auto
connect_timeout = 20

[audio]
source =
sink =
''', encoding='utf-8')
    path.chmod(0o600)
    return path


def install_dependencies():
    create_config()
    configure_proxy(read_config())
    # python3-socks is installed by apt first and visible in this system-site venv.
    # pip's own Requests client needs PySocks BEFORE it can install SOCKS extras.
    import socks  # noqa: F401
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)


def install_model():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / 'model'
    if (target / 'am' / 'final.mdl').is_file() and (target / 'conf' / 'model.conf').is_file():
        return
    configure_proxy(read_config())
    with tempfile.TemporaryDirectory(prefix='voice-model-', dir=DATA_DIR) as temp:
        temp = Path(temp)
        archive = temp / 'model.zip'
        subprocess.run(['curl', '-fLsS', '--connect-timeout', '20', '--max-time', '300',
                        '--retry', '3', '-o', str(archive), MODEL_URL], check=True)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if not (temp / member.filename).resolve().is_relative_to(temp.resolve()):
                    raise RuntimeError('Unsafe model archive path')
            bundle.extractall(temp)
        model = temp / 'vosk-model-small-fa-0.42'
        if not (model / 'am' / 'final.mdl').is_file() or not (model / 'conf' / 'model.conf').is_file():
            raise RuntimeError('Downloaded Vosk model is incomplete')
        if target.exists():
            # Retain an incomplete/custom old directory rather than deleting user data.
            backup = DATA_DIR / ('model.previous-' + str(os.getpid()))
            target.rename(backup)
        shutil.move(str(model), target)


def desktop_quote(value):
    # Desktop Exec is not a shell; protect field codes and its quoted-argument syntax.
    value = str(value).replace('%', '%%')
    for character in ('\\', '"', '`', '$'):
        value = value.replace(character, '\\' + character)
    # The desktop-file key parser unescapes backslashes before Exec parsing.
    return '"' + value.replace('\\', '\\\\') + '"'


def install_autostart():
    directory = Path.home() / '.config' / 'autostart'
    directory.mkdir(parents=True, exist_ok=True)
    executable = desktop_quote(ROOT / '.venv' / 'bin' / 'python')
    launcher = desktop_quote(ROOT / 'launcher.py')
    content = f'''[Desktop Entry]
Type=Application
Name=Ubuntu Voice Agent
Comment=Offline wake word and echo-cancelled Gemini Live
Exec={executable} {launcher}
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
'''
    (directory / 'voice-agent-ubuntu.desktop').write_text(content, encoding='utf-8')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['dependencies', 'finish'])
    args = parser.parse_args()
    if args.action == 'dependencies':
        install_dependencies()
        return
    install_model()
    install_autostart()
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    print('Installed. Existing API key/config preserved.')
    print('Config:', CONFIG_DIR / 'config.ini')
    if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
        subprocess.Popen([sys.executable, str(ROOT / 'launcher.py')], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        print('Starting tray app. Next login starts it automatically.')
    else:
        print('The tray app will start at your next graphical login.')


if __name__ == '__main__':
    main()
