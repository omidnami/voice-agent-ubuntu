#!/usr/bin/env python3
"""GNOME autostart supervisor. Explicit tray Quit exits; crashes restart."""
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from runtime import STATE_DIR, PrivateFormatter, read_config


def main():
    os.umask(0o077)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(__file__).resolve().parent
    handler = RotatingFileHandler(STATE_DIR / 'launcher.log', maxBytes=1_000_000, backupCount=2, encoding='utf-8')
    handler.setFormatter(PrivateFormatter([read_config().get('gemini', 'api_key', fallback='')]))
    logging.basicConfig(handlers=[handler], level=logging.INFO)
    stopping = threading.Event()
    child = None

    def stop(*_):
        stopping.set()
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    with open(STATE_DIR / 'launcher.lock', 'w', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        delay = 2
        while not stopping.is_set():
            started = time.monotonic()
            child = subprocess.Popen([sys.executable, str(root / 'main.py')], cwd=root,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            def report(process):
                for line in iter(process.stderr.readline, b''):
                    logging.warning('Application stderr: %s', line.decode('utf-8', errors='replace').rstrip())
                process.stderr.close()

            reader = threading.Thread(target=report, args=(child,), daemon=True)
            reader.start()
            code = child.wait()
            reader.join(timeout=1)
            if code == 0 or stopping.is_set() or code in (-signal.SIGTERM, -signal.SIGINT):
                return
            if time.monotonic() - started > 60:
                delay = 2
            logging.error('Application exited %s; retrying in %s seconds', code, delay)
            stopping.wait(delay)
            delay = min(30, delay * 2)


if __name__ == '__main__':
    main()
