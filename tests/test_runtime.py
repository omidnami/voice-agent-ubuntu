import configparser
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import bootstrap
import runtime


def config(text=''):
    c = configparser.ConfigParser(interpolation=None)
    c.read_string(text)
    return c


class ProxyTests(unittest.TestCase):
    def test_normalizes_both_case_variants_for_http_and_websocket(self):
        env = {'ALL_PROXY':'socks://127.0.0.1:10808/', 'https_proxy':'socks://127.0.0.1:10808/'}
        self.assertEqual('environment', runtime.configure_proxy(config(), env))
        self.assertEqual('socks5h://127.0.0.1:10808/', env['https_proxy'])
        self.assertEqual(env['https_proxy'], env['wss_proxy'])
        self.assertEqual(env['all_proxy'], env['ALL_PROXY'])

    def test_gnome_proxy_used_without_terminal_exports(self):
        def setting(schema,key):
            return {('org.gnome.system.proxy','mode'):'manual',
                    ('org.gnome.system.proxy.socks','host'):'127.0.0.1',
                    ('org.gnome.system.proxy.socks','port'):10808}.get((schema,key))
        env = {}
        with patch('runtime._gsetting', side_effect=setting):
            self.assertEqual('GNOME', runtime.configure_proxy(config(), env))
        self.assertEqual('socks5h://127.0.0.1:10808', env['wss_proxy'])

    def test_explicit_config_wins_and_direct_removes_proxy(self):
        env = {'HTTPS_PROXY':'http://stale:9999'}
        runtime.configure_proxy(config('[network]\nproxy=socks://other:1234'), env)
        self.assertEqual('socks5h://other:1234', env['HTTPS_PROXY'])
        runtime.configure_proxy(config('[network]\nproxy=direct'), env)
        self.assertEqual({}, env)

    def test_specific_websocket_proxy_preserved_and_no_ports_guessed(self):
        env={'https_proxy':'http://main:1234','wss_proxy':'socks://ws:5678'}
        runtime.configure_proxy(config(), env)
        self.assertEqual('socks5h://ws:5678', env['wss_proxy'])
        with patch('runtime.system_proxy', return_value=''):
            env={}
            self.assertEqual('direct', runtime.configure_proxy(config(),env))
            self.assertEqual({},env)

    def test_invalid_scheme_does_not_leak_credentials(self):
        with self.assertRaises(ValueError) as caught:
            runtime.normalize_proxy('invalid://user:password@localhost:1')
        self.assertNotIn('password', str(caught.exception))

    def test_httpx_accepts_normalized_socks_without_a_network_call(self):
        import httpx
        with httpx.Client(proxy=runtime.normalize_proxy('socks://127.0.0.1:10808')):
            pass

    def test_private_formatter_redacts_traceback_and_proxy_password(self):
        record=logging.LogRecord('test',logging.ERROR,__file__,1,'key=secret-key http://user:password@host:10/?key=another',(),None)
        result=runtime.PrivateFormatter(['secret-key']).format(record)
        self.assertNotIn('secret-key',result)
        self.assertNotIn('password',result)
        self.assertNotIn('another',result)


class WakeTests(unittest.TestCase):
    def test_persian_variants_and_split_syllables(self):
        for text in ['اوبونتو','ابونتو','او بون تو','اُوبونتو','سلام اوبونتو']:
            self.assertTrue(runtime.is_wake_word(text), text)
        self.assertFalse(runtime.is_wake_word('سلام حالت چطوره'))

    def test_partial_result_can_wake_without_waiting_for_sentence_end(self):
        recognizer=Mock()
        recognizer.AcceptWaveform.return_value=False
        recognizer.PartialResult.return_value='{"partial":"او بون تو"}'
        self.assertTrue(runtime.WakeDetector(recognizer).feed(b'pcm'))
        recognizer.Reset.assert_called_once()
        recognizer.Result.assert_not_called()

    def test_final_result_and_empty_partial(self):
        r=Mock()
        r.AcceptWaveform.return_value=True
        r.Result.return_value='{"text":"اوبونتو"}'
        self.assertTrue(runtime.WakeDetector(r).feed(b'pcm'))
        r.AcceptWaveform.return_value=False
        r.PartialResult.return_value='{"partial":""}'
        self.assertFalse(runtime.WakeDetector(r).feed(b'pcm'))


class InstallTests(unittest.TestCase):
    def test_existing_config_and_key_are_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp, patch('bootstrap.CONFIG_DIR',Path(tmp)):
            path=Path(tmp)/'config.ini'
            original='[gemini]\napi_key = a-private-key\nmodel = my-model\n'
            path.write_text(original)
            bootstrap.create_config()
            self.assertEqual(original,path.read_text())
            self.assertEqual(0o600,path.stat().st_mode & 0o777)

    def test_autostart_points_to_supervisor_and_supports_spaces(self):
        with tempfile.TemporaryDirectory() as tmp, patch('bootstrap.Path.home',return_value=Path(tmp)), patch('bootstrap.ROOT',Path(tmp)/'folder with space'):
            bootstrap.install_autostart()
            text=(Path(tmp)/'.config/autostart/voice-agent-ubuntu.desktop').read_text()
            self.assertIn('launcher.py"',text)
            self.assertIn('folder with space/.venv/bin/python"',text)
            self.assertIn('X-GNOME-Autostart-enabled=true',text)
            self.assertNotIn('PULSE_SOURCE=',text)

    def test_default_config_needs_no_manual_audio_or_proxy_exports(self):
        with tempfile.TemporaryDirectory() as tmp, patch('bootstrap.CONFIG_DIR',Path(tmp)):
            c=config(bootstrap.create_config().read_text())
            self.assertEqual('auto',c['network']['proxy'])
            self.assertEqual('',c['audio']['source'])
            self.assertEqual('',c['audio']['sink'])
