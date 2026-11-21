import io
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import patch, Mock

import audio


class FakePulse:
    def __init__(self):
        self.modules=[]
        self.sources=[{'name':'mic','owner_module':1,'monitor_of_sink':4294967295,'properties':{}},
                      {'name':'speaker.monitor','owner_module':1,'monitor_of_sink':2,'properties':{}}]
        self.sinks=[{'name':'speaker','owner_module':1,'properties':{}}]
        self.calls=[]
        self.source='mic'
        self.sink='speaker'
    def __call__(self,*args):
        self.calls.append(args)
        if args[:2]==('--format=json','list'):
            return json.dumps(getattr(self,args[2]))
        if args[0]=='get-default-source':return self.source
        if args[0]=='get-default-sink':return self.sink
        if args[0]=='load-module':
            self.modules.append({'index':99,'name':'module-echo-cancel','argument':' '.join(args[2:])})
            self.sources.append({'name':audio.SOURCE,'owner_module':99,'properties':{}})
            self.sinks.append({'name':audio.SINK,'owner_module':99,'properties':{}})
            return '99'
        if args[0]=='unload-module':
            self.modules=[m for m in self.modules if str(m['index'])!=args[1]]
            self.sources=[n for n in self.sources if n['name']!=audio.SOURCE]
            self.sinks=[n for n in self.sinks if n['name']!=audio.SINK]
            return ''
        raise AssertionError(args)


class EchoBridgeTests(unittest.TestCase):
    def test_json_without_module_indexes_uses_stable_server_ids(self):
        with patch('audio.pactl',side_effect=[
            '[{"name":"module-echo-cancel","argument":"source_name=mic"}]',
            '42\tmodule-echo-cancel\tsource_name=mic\t0\n',
        ]) as command:
            self.assertEqual([{'index':42,'name':'module-echo-cancel','argument':'source_name=mic'}],audio.listing('modules'))
        self.assertEqual(('--format=text','list','short','modules'),command.call_args.args)

    def test_unexpected_json_in_text_fallback_is_rejected_without_value_error(self):
        for output in ('[\n\t{\n        }\t\n]', '[{"index":42,"name":"module-echo-cancel"}]'):
            with self.subTest(output=output), patch('audio.pactl',side_effect=[
                '[{"name":"module-echo-cancel"}]', output,
            ]):
                with self.assertRaises(audio.AudioUnavailable):
                    audio.listing('modules')

    def test_blank_module_rows_are_ignored_and_empty_arguments_preserved(self):
        with patch('audio.pactl',side_effect=[
            '[{"name":"module-echo-cancel"}]', '\n42\tmodule-echo-cancel\t\t\n\n',
        ]):
            self.assertEqual([{'index':42,'name':'module-echo-cancel','argument':''}],audio.listing('modules'))

    def test_json_module_ids_do_not_need_a_text_fallback(self):
        with patch('audio.pactl',return_value='[{"index":42,"name":"module-echo-cancel","argument":""}]') as command:
            self.assertEqual(42,audio.listing('modules')[0]['index'])
            command.assert_called_once_with('--format=json','list','modules')

    def test_both_explicit_aec_routes_created_once_without_changing_system_defaults(self):
        fake=FakePulse()
        with patch('audio.pactl',side_effect=fake):
            bridge=audio.EchoBridge()
            bridge.prepare()
            bridge.prepare()
            self.assertTrue(bridge.ready())
            self.assertEqual(('mic','speaker'),bridge.masters)
            self.assertEqual(1,len([c for c in fake.calls if c[0]=='load-module']))
            self.assertFalse(any(c[0].startswith('set-default') for c in fake.calls))
            bridge.close()
        self.assertFalse(fake.modules)

    def test_virtual_defaults_are_not_nested_and_a_monitor_is_never_a_microphone(self):
        fake=FakePulse()
        with patch('audio.pactl',side_effect=fake):
            bridge=audio.EchoBridge();bridge.prepare()
            fake.source=audio.SOURCE;fake.sink=audio.SINK
            restarted=audio.EchoBridge();restarted.prepare()
            self.assertEqual(('mic','speaker'),restarted.masters)
            self.assertEqual(1,len(fake.modules))
            with self.assertRaises(audio.AudioUnavailable):
                audio.EchoBridge(source='speaker.monitor').prepare()

    def test_audio_server_restart_or_device_change_is_detected(self):
        fake=FakePulse()
        with patch('audio.pactl',side_effect=fake):
            bridge=audio.EchoBridge();bridge.prepare()
            fake.sinks.append({'name':'headphones','properties':{}})
            fake.sink='headphones'
            self.assertFalse(bridge.current_devices())
            bridge.prepare()
            self.assertEqual(('mic','headphones'),bridge.masters)
            fake.sources=[n for n in fake.sources if n['name']!=audio.SOURCE]
            self.assertFalse(bridge.ready())

    def test_no_raw_fallback_when_microphone_missing_or_module_load_fails(self):
        fake=FakePulse();fake.sources=[]
        with patch('audio.pactl',side_effect=fake), self.assertRaises(audio.AudioUnavailable):
            audio.EchoBridge().prepare()
        self.assertFalse(any(c[0]=='load-module' for c in fake.calls))

    def test_capture_and_playback_target_the_same_echo_canceller(self):
        capture=audio.pcm_command('parec',audio.SOURCE,16000)
        playback=audio.pcm_command('pacat',audio.SINK,24000)
        self.assertIn('--device='+audio.SOURCE,capture)
        self.assertIn('--device='+audio.SINK,playback)
        self.assertIn('--format=s16le',capture)
        self.assertIn('--rate=24000',playback)

    def test_only_own_module_is_unloaded(self):
        fake=FakePulse();fake.modules=[{'index':99,'name':'module-echo-cancel','argument':'source_name=someone_else sink_name=other'}]
        bridge=audio.EchoBridge();bridge.module_id=99
        with patch('audio.pactl',side_effect=fake):bridge.close()
        self.assertFalse(any(c[0]=='unload-module' for c in fake.calls))


class PlaybackTests(unittest.TestCase):
    def test_interruption_kills_buffered_output_and_invalidates_queued_generation(self):
        player=audio.Playback();process=Mock()
        process.poll.return_value=None
        player.process=process
        player.play(b'old audio')
        player.clear()
        process.terminate.assert_called_once()
        self.assertTrue(player.chunks.empty())
        self.assertEqual(1,player.generation)
        self.assertIsNone(player.process)
        player.play(b'new audio')
        self.assertEqual((1,b'new audio'),player.chunks.get_nowait())
        player.close()

    def test_capture_reads_complete_pcm_frames_and_close_unblocks_real_pipe(self):
        # Real pipe/process exercise; the subprocess emulates parec, not a microphone.
        import sys
        original=subprocess.Popen
        args_seen=[]
        def spawn(args,**kwargs):
            args_seen.append(args)
            code='import sys,time;sys.stdout.buffer.write(b"x"*1280);sys.stdout.buffer.flush();time.sleep(10)'
            return original([sys.executable,'-c',code],**kwargs)
        received=[]
        with patch('audio.subprocess.Popen',side_effect=spawn):
            capture=audio.Capture(received.append)
            capture.start()
            self.assertTrue(capture.healthy())
            self.assertEqual([b'x'*1280],received)
            capture.close()
            self.assertFalse(capture.worker.is_alive())
            self.assertIsNotNone(capture.process.poll())
        self.assertIn('--device='+audio.SOURCE,args_seen[0])
