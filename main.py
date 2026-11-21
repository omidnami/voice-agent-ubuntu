#!/usr/bin/env python3
"""Ubuntu tray assistant: local wake word -> bounded Gemini Live -> standby."""
import argparse
import asyncio
import contextlib
import fcntl
import importlib.util
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from array import array

from audio import AudioUnavailable, Capture, EchoBridge, Playback, listing
from runtime import (APP_ID, CONFIG_DIR, DATA_DIR, STATE_DIR, PROXY_KEYS, PrivateFormatter,
                     WakeDetector, configure_proxy, read_config, redact, setting)
from session import run_session


def offer(queue_, item):
    try:
        queue_.put_nowait(item)
    except (queue.Full, asyncio.QueueFull):
        with contextlib.suppress(queue.Empty, asyncio.QueueEmpty):
            queue_.get_nowait()
        with contextlib.suppress(queue.Full, asyncio.QueueFull):
            queue_.put_nowait(item)


def drain(queue_):
    while True:
        try:
            queue_.get_nowait()
        except queue.Empty:
            return


class Assistant:
    LABELS = {
        'starting': 'در حال آماده‌سازی صدا و تشخیص واژه…',
        'waiting': 'در انتظار سرویس صدا یا میکروفون…',
        'standby': 'آماده‌باش — بگو: اوبونتو',
        'connecting': 'در حال اتصال به گوگل…',
        'live': 'مکالمه فعال',
        'muted': 'میکروفون خاموش',
    }
    ICONS = {
        'starting': 'view-refresh-symbolic', 'waiting': 'dialog-warning-symbolic',
        'standby': 'audio-input-microphone-symbolic',
        'connecting': 'network-transmit-receive-symbolic',
        'live': 'media-record-symbolic', 'muted': 'microphone-sensitivity-muted-symbolic',
    }

    def __init__(self):
        self.config = read_config()
        self.state = 'starting'
        self.detail = ''
        self.running = threading.Event()
        self.running.set()
        self.mic_enabled = threading.Event()
        self.mic_enabled.set()
        self.stop_requested = threading.Event()
        self.lock = threading.RLock()
        self.session_thread = None
        self.wake_ready = False
        self.wake_not_before = 0
        self.mic_data = queue.Queue(maxsize=25)
        self.audio_queue = None
        self.live_loop = None
        self.last_activity = [time.monotonic()]
        self.capture = None
        self.bridge = EchoBridge(
            self.config.get('audio', 'source', fallback='').strip(),
            self.config.get('audio', 'sink', fallback='').strip(),
        )
        # Terminal exports and GNOME proxy preferences are read separately.
        self.initial_proxy_env = {k: v for k, v in os.environ.items() if k.lower() in PROXY_KEYS}
        self.indicator = AppIndicator.Indicator.new(APP_ID, self.ICONS['starting'],
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label=self.LABELS['starting'])
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)
        self.detail_item = Gtk.MenuItem(label='')
        self.detail_item.set_sensitive(False)
        menu.append(self.detail_item)
        menu.append(Gtk.SeparatorMenuItem())
        self.start_item = Gtk.MenuItem(label='شروع مکالمه')
        self.start_item.connect('activate', lambda *_: self.start_conversation())
        menu.append(self.start_item)
        self.stop_item = Gtk.MenuItem(label='پایان مکالمه')
        self.stop_item.connect('activate', lambda *_: self.stop_requested.set())
        menu.append(self.stop_item)
        self.mute_item = Gtk.MenuItem(label='خاموش‌کردن میکروفون')
        self.mute_item.connect('activate', self.toggle_mic)
        menu.append(self.mute_item)
        logs = Gtk.MenuItem(label='نمایش گزارش')
        logs.connect('activate', lambda *_: subprocess.Popen(['xdg-open', str(STATE_DIR / 'agent.log')]))
        menu.append(logs)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label='خروج')
        quit_item.connect('activate', self.quit)
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self._update_tray()
        self.audio_worker = threading.Thread(target=self.supervise_audio, name='audio-supervisor', daemon=True)
        self.wake_worker = threading.Thread(target=self.listen, name='wake-listener', daemon=True)
        self.audio_worker.start()
        self.wake_worker.start()

    def set_state(self, state, detail=None):
        with self.lock:
            self.state = state
            if detail is not None:
                self.detail = redact(detail, [self.config.get('gemini', 'api_key', fallback='')])[:180]
        GLib.idle_add(self._update_tray)

    def _update_tray(self):
        self.indicator.set_icon_full(self.ICONS[self.state], self.LABELS[self.state])
        self.status_item.set_label(self.LABELS[self.state])
        self.detail_item.set_label(self.detail)
        self.detail_item.set_visible(bool(self.detail))
        self.start_item.set_sensitive(self.state == 'standby')
        self.stop_item.set_sensitive(self.state in ('connecting', 'live'))
        self.mute_item.set_label('خاموش‌کردن میکروفون' if self.mic_enabled.is_set() else 'روشن‌کردن میکروفون')
        return False

    def toggle_mic(self, *_):
        if self.mic_enabled.is_set():
            self.mic_enabled.clear()
            self.stop_requested.set()
            self.set_state('muted', '')
        else:
            self.mic_enabled.set()
            self.set_state('starting', '')

    def supervise_audio(self):
        last_error = None
        last_probe = 0
        while self.running.is_set():
            try:
                if not self.mic_enabled.is_set():
                    if self.capture:
                        self.capture.close()
                        self.capture = None
                    drain(self.mic_data)
                    time.sleep(0.2)
                    continue
                if self.capture and not self.capture.healthy():
                    raise AudioUnavailable('ضبط صدا قطع شده است؛ در حال اتصال دوباره…')
                if self.capture and time.monotonic() - last_probe >= 5:
                    last_probe = time.monotonic()
                    if not self.bridge.ready() or not self.bridge.current_devices():
                        raise AudioUnavailable('دستگاه صدا تغییر کرده است؛ در حال اتصال دوباره…')
                if self.capture is None:
                    self.bridge.prepare()
                    self.capture = Capture(lambda pcm: offer(self.mic_data, pcm))
                    self.capture.start()
                    last_error = None
                if self.wake_ready and self.state in ('starting', 'waiting'):
                    self.set_state('standby', '')
                time.sleep(1)
            except Exception as exc:
                self.stop_requested.set()
                if self.capture:
                    self.capture.close()
                    self.capture = None
                self.bridge.close()
                if str(exc) != last_error:
                    logging.exception('Audio not ready; will retry automatically')
                    last_error = str(exc)
                if self.mic_enabled.is_set():
                    self.set_state('waiting', str(exc))
                for _ in range(20):
                    if not self.running.is_set() or not self.mic_enabled.is_set():
                        break
                    time.sleep(0.1)
        if self.capture:
            self.capture.close()
            self.capture = None
        self.bridge.close()

    def start_conversation(self):
        with self.lock:
            if self.state != 'standby' or not self.capture or not self.capture.healthy():
                return
            if self.session_thread and self.session_thread.is_alive():
                return
            self.config = read_config()
            if not self.config.get('gemini', 'api_key', fallback='').strip():
                self.set_state('standby', 'کلید گوگل را در config.ini وارد کنید.')
                self.wake_not_before = time.monotonic() + 3
                return
            self.stop_requested.clear()
            self.set_state('connecting', '')
            self.session_thread = threading.Thread(target=self._run_live, name='gemini-live', daemon=True)
            self.session_thread.start()

    def listen(self):
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            detector = WakeDetector(KaldiRecognizer(Model(str(DATA_DIR / 'model')), 16000))
            self.wake_ready = True
            previous_state = None
            while self.running.is_set():
                try:
                    pcm = self.mic_data.get(timeout=0.2)
                except queue.Empty:
                    continue
                state = self.state
                if state != previous_state:
                    detector.reset()
                    previous_state = state
                if state == 'standby' and time.monotonic() >= self.wake_not_before:
                    if detector.feed(pcm):
                        logging.info('Wake word detected')
                        self.start_conversation()
                elif state == 'live' and self.live_loop and self.audio_queue is not None:
                    if max(map(abs, array('h', pcm)), default=0) > 1400:
                        self.last_activity[0] = time.monotonic()
                    try:
                        self.live_loop.call_soon_threadsafe(self._queue_audio, pcm)
                    except RuntimeError:
                        pass
        except Exception as exc:
            logging.exception('Wake listener failed')
            self.wake_ready = False
            self.set_state('waiting', 'مدل تشخیص واژه آماده نیست؛ نصب‌کننده را دوباره اجرا کنید. ' + str(exc))

    def _queue_audio(self, pcm):
        if self.audio_queue is not None and self.state == 'live':
            offer(self.audio_queue, pcm)

    def _run_live(self):
        detail = ''
        try:
            # Restore the inherited snapshot so changes in GNOME proxy settings
            # are discovered at the next wake, instead of reusing our old values.
            for key in PROXY_KEYS:
                os.environ.pop(key, None)
                os.environ.pop(key.upper(), None)
            os.environ.update(self.initial_proxy_env)
            origin = configure_proxy(self.config)
            logging.info('Connecting Gemini Live; proxy configuration=%s', origin)
            asyncio.run(self._live())
        except Exception as exc:
            logging.exception('Gemini Live session failed')
            detail = str(exc)
        finally:
            self.audio_queue = None
            self.live_loop = None
            drain(self.mic_data)
            self.wake_not_before = time.monotonic() + 1
            if self.running.is_set() and self.mic_enabled.is_set() and self.state in ('connecting', 'live'):
                self.set_state('standby', detail)

    def mark_live(self):
        with self.lock:
            if self.state == 'connecting' and self.mic_enabled.is_set() and not self.stop_requested.is_set():
                self.set_state('live', '')
            else:
                self.stop_requested.set()

    async def _live(self):
        self.live_loop = asyncio.get_running_loop()
        self.audio_queue = asyncio.Queue(maxsize=20)
        player = Playback()
        player.start()
        try:
            await run_session(
                api_key=self.config.get('gemini', 'api_key').strip(),
                model=self.config.get('gemini', 'model', fallback='gemini-3.8-live').strip(),
                instruction=self.config.get('gemini', 'instruction', fallback='فارسی، طبیعی و کوتاه جواب بده.'),
                audio_queue=self.audio_queue, player=player, stop_requested=self.stop_requested,
                on_ready=self.mark_live, last_activity=self.last_activity,
                connection_timeout=setting(self.config, 'network', 'connect_timeout', 20, maximum=120),
                idle_timeout=setting(self.config, 'gemini', 'idle_timeout', 60),
            )
        finally:
            player.close()

    def quit(self, *_):
        self.running.clear()
        self.stop_requested.set()
        Gtk.main_quit()

    def close(self):
        self.running.clear()
        self.stop_requested.set()
        if self.session_thread:
            self.session_thread.join(timeout=10)
        self.audio_worker.join(timeout=10)


def run_gui():
    global AppIndicator, GLib, Gtk
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator, GLib, Gtk

    assistant = Assistant()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (assistant.quit(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (assistant.quit(), False)[1])
    try:
        Gtk.main()
    finally:
        assistant.close()


def diagnostics():
    config = read_config()
    print('Config:', CONFIG_DIR / 'config.ini')
    print('API key:', 'configured' if config.get('gemini', 'api_key', fallback='').strip() else 'MISSING')
    print('Wake model:', 'installed' if (DATA_DIR / 'model').is_dir() else 'MISSING')
    print('Proxy source:', configure_proxy(config))
    for package in ('google.genai', 'vosk', 'socks', 'socksio', 'python_socks'):
        try:
            available = importlib.util.find_spec(package) is not None
        except ModuleNotFoundError:
            available = False
        print(package + ':', 'OK' if available else 'MISSING')
    for kind in ('sources', 'sinks'):
        print(kind + ':', ', '.join(n['name'] for n in listing(kind)))
    print('Log:', STATE_DIR / 'agent.log')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Show local prerequisites without opening a Google connection')
    args = parser.parse_args()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.umask(0o077)
    handler = RotatingFileHandler(STATE_DIR / 'agent.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    config = read_config()
    handler.setFormatter(PrivateFormatter([config.get('gemini', 'api_key', fallback='')]))
    logging.basicConfig(handlers=[handler], level=logging.INFO)
    if args.check:
        diagnostics()
        return
    with open(STATE_DIR / 'instance.lock', 'w', encoding='utf-8') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            run_gui()
        except Exception:
            logging.exception('Failed to start voice assistant')
            raise


if __name__ == '__main__':
    main()
