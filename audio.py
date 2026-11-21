"""Full-duplex PulseAudio/PipeWire AEC with explicitly named capture and playback.

Both streams go through the same echo-cancel module. No global default changes,
no raw-device fallback, and no microphone muting during assistant speech.
"""
import json
import logging
import os
import queue
import shlex
import subprocess
import threading
import time

SOURCE = 'voice_agent_aec_mic'
SINK = 'voice_agent_aec_speaker'
FRAME_BYTES = 1280  # 40 ms, 16 kHz mono signed 16-bit little endian


class AudioUnavailable(RuntimeError):
    pass


def pactl(*args):
    try:
        result = subprocess.run(['pactl', *args], capture_output=True, text=True,
                                timeout=5, check=True)
        return result.stdout.strip()
    except FileNotFoundError:
        raise AudioUnavailable('ابزار pactl نصب نیست؛ نصب‌کننده را دوباره اجرا کنید.') from None
    except subprocess.TimeoutExpired:
        raise AudioUnavailable('سرویس صدای سیستم پاسخ نمی‌دهد.') from None
    except subprocess.CalledProcessError as exc:
        raise AudioUnavailable('PulseAudio/PipeWire: ' + (exc.stderr or '').strip()[:240]) from None


def listing(kind):
    nodes = json.loads(pactl('--format=json', 'list', kind))
    # Some pactl versions omit module indexes in JSON. The short listing keeps
    # stable server IDs; never infer them from the position in the JSON array.
    if kind == 'modules' and any('index' not in node for node in nodes):
        nodes = []
        # "short" controls fields, not serialization. Pin text explicitly so
        # JSON output can never be mistaken for tab-separated module rows.
        for line in pactl('--format=text', 'list', 'short', 'modules').splitlines():
            if not line.strip():
                continue
            columns = line.split('\t', 3)
            index = columns[0].strip()
            if len(columns) < 2 or not index.isascii() or not index.isdecimal() or not columns[1]:
                raise AudioUnavailable('خروجی فهرست ماژول‌های pactl قالب متنی معتبر ندارد؛ شناسهٔ ماژول قابل تشخیص نیست.')
            nodes.append({'index': int(index), 'name': columns[1],
                          'argument': columns[2] if len(columns) > 2 else ''})
    return nodes


def module_options(module):
    return dict(item.split('=', 1) for item in shlex.split(module.get('argument', '')) if '=' in item)


def physical(node, kind, echo_modules):
    props = node.get('properties', {})
    if node['name'] in (SOURCE, SINK) or node.get('owner_module') in echo_modules:
        return False
    if str(props.get('node.virtual', '')).lower() in ('true', '1') or props.get('device.class') in ('filter', 'monitor'):
        return False
    if kind == 'sources':
        monitor = node.get('monitor_of_sink')
        if node['name'].endswith('.monitor') or monitor not in (None, 4294967295, '4294967295', 'n/a'):
            return False
    return True


class EchoBridge:
    def __init__(self, source='', sink=''):
        self.requested_source, self.requested_sink = source, sink
        self.module_id = None
        self.masters = None

    def _select(self, kind, explicit, modules):
        nodes = listing(kind)
        echo_ids = {m['index'] for m in modules if m['name'] == 'module-echo-cancel'}
        candidates = [n['name'] for n in nodes if physical(n, kind, echo_ids)]
        if explicit:
            if explicit not in candidates:
                raise AudioUnavailable(f'دستگاه صدای انتخاب‌شده در دسترس نیست: {explicit}')
            return explicit
        default = pactl('get-default-source' if kind == 'sources' else 'get-default-sink')
        if default in candidates:
            return default
        # WirePlumber can temporarily select a virtual node as default; retain the
        # last physical master instead of nesting one echo-canceler in another.
        previous = self.masters[0 if kind == 'sources' else 1] if self.masters else None
        if previous in candidates:
            return previous
        if not candidates:
            raise AudioUnavailable('میکروفون یا اسپیکر واقعی هنوز آماده نیست.')
        return candidates[0]

    def prepare(self):
        modules = listing('modules')
        ours = next((m for m in modules if m['name'] == 'module-echo-cancel'
                     and module_options(m).get('source_name') == SOURCE
                     and module_options(m).get('sink_name') == SINK), None)
        if ours and self.masters is None:
            previous = module_options(ours)
            self.masters = (previous.get('source_master'), previous.get('sink_master'))
        source = self._select('sources', self.requested_source, modules)
        sink = self._select('sinks', self.requested_sink, modules)
        if ours:
            opts = module_options(ours)
            self.module_id = ours['index']
            if opts.get('source_master') != source or opts.get('sink_master') != sink:
                self.close()
                ours = None
        if not ours:
            if any(n['name'] == SOURCE for n in listing('sources')) or any(n['name'] == SINK for n in listing('sinks')):
                raise AudioUnavailable('نام مسیر حذف اکو قبلاً توسط ماژول دیگری استفاده شده است.')
            self.module_id = int(pactl('load-module', 'module-echo-cancel',
                'aec_method=webrtc', 'source_name=' + SOURCE, 'sink_name=' + SINK,
                'source_master=' + source, 'sink_master=' + sink,
                'rate=48000', 'channels=1', 'channel_map=mono',
                'aec_args="analog_gain_control=0 digital_gain_control=0 noise_suppression=1"'))
        self.masters = (source, sink)
        if not self.ready():
            raise AudioUnavailable('ورودی و خروجی حذف اکو ساخته نشدند.')
        logging.info('AEC ready: capture=%s playback=%s', source, sink)

    def ready(self):
        if not self.masters:
            return False
        sources, sinks = listing('sources'), listing('sinks')
        return ({SOURCE, self.masters[0]} <= {n['name'] for n in sources}
                and {SINK, self.masters[1]} <= {n['name'] for n in sinks})

    def current_devices(self):
        modules = listing('modules')
        return self.masters == (self._select('sources', self.requested_source, modules),
                                self._select('sinks', self.requested_sink, modules))

    def close(self):
        if self.module_id is not None:
            try:
                # Only unload this application's named echo-cancel instance.
                for module in listing('modules'):
                    if module['index'] == self.module_id:
                        opts = module_options(module)
                        if opts.get('source_name') == SOURCE and opts.get('sink_name') == SINK:
                            pactl('unload-module', str(self.module_id))
                        break
            except (AudioUnavailable, ValueError):
                pass
            self.module_id = None


def terminate(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)


def pcm_command(program, device, rate):
    return [program, '--raw', '--device=' + device, '--rate=' + str(rate),
            '--channels=1', '--format=s16le', '--latency-msec=40',
            '--client-name=voice-agent-ubuntu']


class Capture:
    def __init__(self, callback):
        self.callback = callback
        self.process = None
        self.stopping = threading.Event()
        self.first_frame = threading.Event()
        self.failed = None
        self.last_frame = time.monotonic()
        self.worker = threading.Thread(target=self._run, name='voice-capture', daemon=True)

    def start(self):
        self.process = subprocess.Popen(pcm_command('parec', SOURCE, 16000),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        self.worker.start()
        if not self.first_frame.wait(5) or self.failed:
            self.close()
            raise AudioUnavailable('ضبط از ورودی حذف اکو آغاز نشد.')

    def _run(self):
        try:
            buffer = bytearray()
            while not self.stopping.is_set():
                block = os.read(self.process.stdout.fileno(), FRAME_BYTES)
                if not block:
                    raise AudioUnavailable('ارتباط ضبط صدا قطع شد.')
                buffer.extend(block)
                while len(buffer) >= FRAME_BYTES:
                    pcm = bytes(buffer[:FRAME_BYTES])
                    del buffer[:FRAME_BYTES]
                    self.last_frame = time.monotonic()
                    self.first_frame.set()
                    self.callback(pcm)
        except Exception as exc:
            if not self.stopping.is_set():
                self.failed = exc
                self.first_frame.set()
                logging.exception('Capture failed')

    def healthy(self):
        return (self.process is not None and self.process.poll() is None
                and self.failed is None and time.monotonic() - self.last_frame < 6)

    def close(self):
        self.stopping.set()
        terminate(self.process)
        if self.worker.is_alive():
            self.worker.join(timeout=2)
        if self.process:
            for pipe in (self.process.stdout, self.process.stderr):
                if pipe:
                    pipe.close()


class Playback:
    def __init__(self):
        self.chunks = queue.Queue(maxsize=40)
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.process = None
        self.generation = 0
        self.failed = None
        self.worker = threading.Thread(target=self._run, name='voice-playback', daemon=True)

    def start(self):
        self.worker.start()

    def play(self, chunk):
        if self.failed:
            raise AudioUnavailable('پخش صدا متوقف شد.') from self.failed
        if not self.stopping.is_set():
            try:
                self.chunks.put((self.generation, chunk), timeout=2)
            except queue.Full:
                raise AudioUnavailable('صف پخش بیش از حد تأخیر دارد.') from None

    def _run(self):
        try:
            while not self.stopping.is_set():
                try:
                    generation, chunk = self.chunks.get(timeout=0.1)
                except queue.Empty:
                    continue
                if generation != self.generation:
                    continue
                with self.lock:
                    if self.stopping.is_set() or generation != self.generation:
                        continue
                    if self.process is None:
                        self.process = subprocess.Popen(pcm_command('pacat', SINK, 24000),
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
                    process = self.process
                try:
                    for start in range(0, len(chunk), 1920):
                        if self.stopping.is_set() or generation != self.generation:
                            break
                        data = memoryview(chunk)[start:start + 1920]
                        while data:
                            written = process.stdin.write(data)
                            if not written:
                                raise BrokenPipeError('Audio sink closed')
                            data = data[written:]
                except (BrokenPipeError, OSError, ValueError):
                    if not self.stopping.is_set() and generation == self.generation:
                        raise AudioUnavailable('خروجی حذف اکو در دسترس نیست.') from None
        except Exception as exc:
            self.failed = exc
            logging.exception('Playback failed')

    def clear(self):
        with self.lock:
            self.generation += 1
            process, self.process = self.process, None
        while True:
            try:
                self.chunks.get_nowait()
            except queue.Empty:
                break
        # Cancel PulseAudio's already-buffered audio as well as our Python queue.
        terminate(process)
        if process:
            for pipe in (process.stdin, process.stderr):
                if pipe:
                    pipe.close()

    def close(self):
        self.stopping.set()
        self.clear()
        if self.worker.is_alive():
            self.worker.join(timeout=2)
