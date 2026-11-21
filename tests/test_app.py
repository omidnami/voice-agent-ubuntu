import asyncio
import configparser
import os
import queue
import threading
import unittest
from unittest.mock import Mock, patch

import main


def config():
    c=configparser.ConfigParser(interpolation=None)
    c.read_string('[gemini]\napi_key=test-secret\nmodel=gemini-3.8-live\n[network]\nproxy=direct\n')
    return c


class AppTests(unittest.TestCase):
    def setUp(self):
        self.patches=[patch('main.read_config',return_value=config()),
            patch('main.AppIndicator',create=True),patch('main.Gtk',create=True),
            patch('main.GLib',create=True),patch('main.threading.Thread',side_effect=lambda *args,**kwargs:Mock())]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.app=main.Assistant()
        self.addCleanup(self.app.running.clear)

    def test_startup_only_starts_local_audio_and_wake_threads(self):
        self.assertEqual('starting',self.app.state)
        self.assertIsNone(self.app.session_thread)
        self.assertIsNone(self.app.live_loop)
        self.assertIsNone(self.app.audio_queue)
        self.app.audio_worker.start.assert_called()
        self.app.wake_worker.start.assert_called()

    def test_wake_cannot_open_duplicate_or_unready_sessions(self):
        self.app.start_conversation()
        self.assertIsNone(self.app.session_thread)
        self.app.state='standby';self.app.capture=Mock();self.app.capture.healthy.return_value=True
        self.app.start_conversation()
        self.assertEqual('connecting',self.app.state)
        thread=self.app.session_thread
        self.app.start_conversation()
        self.assertIs(thread,self.app.session_thread)
        thread.start.assert_called_once()

    def test_muting_closes_session_but_late_handshake_cannot_set_live(self):
        self.app.state='connecting'
        self.app.toggle_mic()
        self.assertEqual('muted',self.app.state)
        self.assertTrue(self.app.stop_requested.is_set())
        self.app.mark_live()
        self.assertEqual('muted',self.app.state)
        self.app.toggle_mic()
        self.assertEqual('starting',self.app.state)
        self.assertTrue(self.app.mic_enabled.is_set())

    def test_connection_failure_returns_to_wake_ready_standby(self):
        self.app.state='connecting'
        with patch.dict(os.environ,{},clear=True),patch('main.asyncio.run',side_effect=RuntimeError('connection failed')):
            with patch.object(self.app,'_live',new=Mock(return_value=None)):
                self.app._run_live()
        self.assertEqual('standby',self.app.state)
        self.assertIn('connection failed',self.app.detail)
        self.assertIsNone(self.app.audio_queue)
        self.assertIsNone(self.app.live_loop)

    def test_audio_not_ready_at_login_is_retried_automatically(self):
        self.app.wake_ready=True
        self.app.bridge=Mock()
        self.app.bridge.prepare.side_effect=[RuntimeError('not ready'),None]
        capture=Mock()
        capture.start.side_effect=lambda:self.app.running.clear()
        with patch('main.Capture',return_value=capture),patch('main.time.sleep'):
            self.app.supervise_audio()
        self.assertEqual(2,self.app.bridge.prepare.call_count)
        self.assertEqual('standby',self.app.state)
        capture.close.assert_called_once()

    def test_input_queue_is_bounded_and_only_used_in_live_state(self):
        self.app.audio_queue=asyncio.Queue(maxsize=2)
        self.app.state='standby';self.app._queue_audio(b'not sent')
        self.assertTrue(self.app.audio_queue.empty())
        self.app.state='live'
        for chunk in (b'old',b'one',b'two'):self.app._queue_audio(chunk)
        self.assertEqual(b'one',self.app.audio_queue.get_nowait())
        self.assertEqual(b'two',self.app.audio_queue.get_nowait())
