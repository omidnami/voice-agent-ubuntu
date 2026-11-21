#!/usr/bin/env python3
"""Small Ubuntu tray voice assistant: offline wake word, then Gemini Live."""

import asyncio
import configparser
import fcntl
import json
import logging
import queue
import re
import threading
import time
from array import array
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator, GLib, Gtk

import sounddevice as sd
from google import genai
from google.genai import types
from vosk import KaldiRecognizer, Model, SetLogLevel

APP_ID = "voice-agent-ubuntu"
CONFIG_DIR = Path.home() / ".config" / APP_ID
DATA_DIR = Path.home() / ".local" / "share" / APP_ID
STATE_DIR = Path.home() / ".local" / "state" / APP_ID
SAMPLE_RATE = 16000


def is_wake_word(text):
    """Accept common Persian spellings and Vosk's separated syllables."""
    text = re.sub(r"[\s\u200c\u064b-\u065f\u0670]+", "", text)
    text = text.replace("آ", "ا").replace("أ", "ا").replace("إ", "ا")
    return "اوبونتو" in text or "ابونتو" in text


class Playback:
    """Play received 24 kHz PCM without blocking websocket reads."""

    def __init__(self):
        self.chunks = queue.Queue(maxsize=80)
        self.stopping = threading.Event()
        self.worker = threading.Thread(target=self._run, name="voice-playback", daemon=True)
        self.failed = None

    def start(self):
        self.worker.start()

    def _run(self):
        try:
            with sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as output:
                while not self.stopping.is_set():
                    try:
                        chunk = self.chunks.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if chunk:
                        output.write(chunk)
        except Exception as exc:
            self.failed = exc
            logging.exception("Audio output failed")

    def play(self, chunk):
        if self.failed:
            raise RuntimeError("Audio output unavailable") from self.failed
        try:
            self.chunks.put_nowait(chunk)
        except queue.Full:
            logging.warning("Audio output queue full; dropping delayed chunk")

    def clear(self):
        while True:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                return

    def close(self):
        self.stopping.set()
        self.clear()
        self.worker.join(timeout=1)


class Assistant:
    ICONS = {
        "standby": "audio-input-microphone-symbolic",
        "connecting": "network-transmit-receive-symbolic",
        "live": "media-record-symbolic",
        "muted": "microphone-sensitivity-muted-symbolic",
        "error": "dialog-error-symbolic",
    }
    LABELS = {
        "standby": "آماده‌باش — بگو: اوبونتو",
        "connecting": "در حال اتصال به گوگل...",
        "live": "مکالمه فعال",
        "muted": "میکروفون خاموش",
        "error": "خطا — گزارش را بررسی کن",
    }

    def __init__(self):
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(CONFIG_DIR / "config.ini", encoding="utf-8")
        self.api_key = parser.get("gemini", "api_key", fallback="").strip()
        self.model_name = parser.get("gemini", "model", fallback="gemini-3.8-live").strip()
        self.instruction = parser.get(
            "gemini", "instruction",
            fallback="تو دستیار صوتی شخصی من هستی. فارسی، طبیعی و کوتاه جواب بده.",
        )
        self.state = "standby"
        self.running = threading.Event()
        self.running.set()
        self.stop_requested = threading.Event()
        self.mic_data = queue.Queue(maxsize=40)
        self.audio_queue = None
        self.live_loop = None
        self.mic = None
        self.last_activity = time.monotonic()
        SetLogLevel(-1)
        self.wake_model = Model(str(DATA_DIR / "model"))

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, self.ICONS["standby"], AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label=self.LABELS["standby"])
        self.status_item.set_sensitive(False)
        menu.append(self.status_item)
        menu.append(Gtk.SeparatorMenuItem())
        self.start_item = Gtk.MenuItem(label="شروع مکالمه")
        self.start_item.connect("activate", lambda *_: self.start_conversation())
        menu.append(self.start_item)
        self.stop_item = Gtk.MenuItem(label="پایان مکالمه")
        self.stop_item.connect("activate", lambda *_: self.stop_requested.set())
        menu.append(self.stop_item)
        self.mute_item = Gtk.MenuItem(label="خاموش‌کردن میکروفون")
        self.mute_item.connect("activate", self.toggle_mic)
        menu.append(self.mute_item)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="خروج")
        quit_item.connect("activate", self.quit)
        menu.append(quit_item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self._update_tray()
        self.open_mic()
        threading.Thread(target=self.listen, name="wake-listener", daemon=True).start()

    def set_state(self, name):
        self.state = name
        GLib.idle_add(self._update_tray)

    def _update_tray(self):
        self.indicator.set_icon_full(self.ICONS[self.state], self.LABELS[self.state])
        self.status_item.set_label(self.LABELS[self.state])
        self.start_item.set_sensitive(self.state in ("standby", "error"))
        self.stop_item.set_sensitive(self.state in ("connecting", "live"))
        self.mute_item.set_label(
            "روشن‌کردن میکروفون" if self.state == "muted" else "خاموش‌کردن میکروفون"
        )
        return False

    def _mic_callback(self, indata, frames, timestamp, status):
        if status:
            logging.debug("Microphone status: %s", status)
        try:
            self.mic_data.put_nowait(bytes(indata))
        except queue.Full:
            try:
                self.mic_data.get_nowait()
                self.mic_data.put_nowait(bytes(indata))
            except queue.Empty:
                pass

    def open_mic(self):
        if self.mic is not None:
            return
        try:
            self.mic = sd.RawInputStream(
                samplerate=SAMPLE_RATE, blocksize=1600, channels=1,
                dtype="int16", callback=self._mic_callback,
            )
            self.mic.start()
            self.set_state("standby")
        except Exception:
            logging.exception("Cannot open microphone")
            self.mic = None
            self.set_state("error")

    def toggle_mic(self, *_):
        if self.state == "muted":
            self.open_mic()
            return
        self.stop_requested.set()
        if self.mic:
            self.mic.close()
            self.mic = None
        with self.mic_data.mutex:
            self.mic_data.queue.clear()
        self.set_state("muted")

    def start_conversation(self):
        if self.mic is None or self.state not in ("standby", "error"):
            return
        if not self.api_key:
            logging.error("Set [gemini] api_key in %s/config.ini", CONFIG_DIR)
            self.set_state("error")
            return
        self.stop_requested.clear()
        self.set_state("connecting")
        threading.Thread(target=self._run_live, name="gemini-live", daemon=True).start()

    def listen(self):
        recognizer = KaldiRecognizer(self.wake_model, SAMPLE_RATE)
        while self.running.is_set():
            try:
                audio = self.mic_data.get(timeout=0.25)
            except queue.Empty:
                continue
            if self.state == "standby":
                if recognizer.AcceptWaveform(audio):
                    words = json.loads(recognizer.Result()).get("text", "")
                    if is_wake_word(words):
                        logging.info("Wake word detected")
                        self.start_conversation()
                        recognizer.Reset()
            elif self.state == "live" and self.audio_queue and self.live_loop:
                if max(map(abs, array("h", audio)), default=0) > 1400:
                    self.last_activity = time.monotonic()
                try:
                    self.live_loop.call_soon_threadsafe(self._queue_audio, audio)
                except RuntimeError:
                    pass
            elif self.state != "standby":
                recognizer.Reset()

    def _queue_audio(self, audio):
        if self.audio_queue is None:
            return
        if self.audio_queue.full():
            self.audio_queue.get_nowait()
        self.audio_queue.put_nowait(audio)

    def _run_live(self):
        failed = False
        try:
            asyncio.run(self._live())
        except Exception:
            failed = True
            logging.exception("Gemini Live session failed")
        finally:
            self.audio_queue = None
            self.live_loop = None
            if self.running.is_set() and self.mic is not None and self.state != "muted":
                self.set_state("error" if failed else "standby")

    async def _live(self):
        self.live_loop = asyncio.get_running_loop()
        self.audio_queue = asyncio.Queue(maxsize=16)
        player = Playback()
        player.start()
        client = None
        try:
            client = genai.Client(api_key=self.api_key, http_options={"api_version": "v1beta"})
            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"], system_instruction=self.instruction
            )
            async with client.aio.live.connect(model=self.model_name, config=config) as session:
                if self.stop_requested.is_set():
                    return
                self.last_activity = time.monotonic()
                self.set_state("live")

                async def microphone():
                    while not self.stop_requested.is_set():
                        audio = await self.audio_queue.get()
                        await session.send_realtime_input(
                            audio=types.Blob(data=audio, mime_type="audio/pcm;rate=16000")
                        )

                async def speakers():
                    while not self.stop_requested.is_set():
                        # receive() ends after each model turn, not after the session.
                        async for message in session.receive():
                            if message.go_away:
                                logging.info("Google requested session end")
                                return
                            content = message.server_content
                            if not content:
                                continue
                            if content.interrupted:
                                player.clear()
                            for part in content.model_turn.parts if content.model_turn else ():
                                if part.inline_data and part.inline_data.data:
                                    player.play(part.inline_data.data)
                                    self.last_activity = time.monotonic()

                async def timeout():
                    while not self.stop_requested.is_set():
                        await asyncio.sleep(1)
                        if player.failed:
                            raise RuntimeError("Audio output failed") from player.failed
                        if time.monotonic() - self.last_activity > 60:
                            logging.info("Ending idle conversation")
                            return

                tasks = [asyncio.create_task(fn()) for fn in (microphone, speakers, timeout)]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            player.close()
            if client is not None:
                client.close()

    def quit(self, *_):
        self.running.clear()
        self.stop_requested.set()
        if self.mic:
            self.mic.close()
            self.mic = None
        Gtk.main_quit()


def main():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=STATE_DIR / "agent.log", level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
    )
    lock = open(STATE_DIR / "instance.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Voice Agent is already running")
        return
    try:
        Assistant()
        Gtk.main()
    except Exception:
        logging.exception("Failed to start voice assistant")
        raise


if __name__ == "__main__":
    main()
