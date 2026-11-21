"""Configuration, proxy discovery and local wake recognition (no GUI/network imports)."""
import ast
import configparser
import logging
import os
import re
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

APP_ID = 'voice-agent-ubuntu'
CONFIG_DIR = Path.home() / '.config' / APP_ID
DATA_DIR = Path.home() / '.local' / 'share' / APP_ID
STATE_DIR = Path.home() / '.local' / 'state' / APP_ID
PROXY_KEYS = ('http_proxy', 'https_proxy', 'all_proxy', 'ws_proxy', 'wss_proxy')


def read_config():
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(CONFIG_DIR / 'config.ini', encoding='utf-8')
    return parser


def setting(config, section, name, fallback, minimum=1, maximum=3600):
    value = config.getint(section, name, fallback=fallback)
    if not minimum <= value <= maximum:
        raise ValueError(f'[{section}] {name} must be between {minimum} and {maximum}')
    return value


def normalize_proxy(value):
    value = value.strip()
    if not value:
        return ''
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme == 'socks':
        scheme = 'socks5h'
    if scheme not in ('http', 'https', 'socks5', 'socks5h') or not parts.hostname:
        raise ValueError('آدرس پراکسی معتبر نیست؛ http:// یا socks5h:// لازم است.')
    # Accessing port validates malformed ports without logging credentials.
    try:
        parts.port
    except ValueError:
        raise ValueError('پورت پراکسی معتبر نیست.') from None
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


def _gsetting(schema, key):
    try:
        value = subprocess.run(['gsettings', 'get', schema, key], capture_output=True,
                               text=True, timeout=3, check=True).stdout.strip()
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value
    except (OSError, subprocess.SubprocessError):
        return None


def system_proxy():
    if _gsetting('org.gnome.system.proxy', 'mode') != 'manual':
        return ''
    for kind, scheme in (('https', 'http'), ('socks', 'socks5h'), ('http', 'http')):
        host = _gsetting('org.gnome.system.proxy.' + kind, 'host')
        port = _gsetting('org.gnome.system.proxy.' + kind, 'port')
        try:
            port = int(str(port).split()[-1])
        except (ValueError, IndexError):
            continue
        if host and 0 < port < 65536:
            host = str(host)
            if ':' in host and not host.startswith('['):
                host = '[' + host + ']'
            return normalize_proxy(f'{scheme}://{host}:{port}')
    return ''


def configure_proxy(config, environ=None):
    """Also works at GNOME login, when terminal exports aren't inherited."""
    env = os.environ if environ is None else environ
    choice = config.get('network', 'proxy', fallback='auto').strip()
    if choice.lower() == 'direct':
        for key in PROXY_KEYS:
            env.pop(key, None)
            env.pop(key.upper(), None)
        return 'direct'
    if choice.lower() not in ('auto', ''):
        proxy, origin = normalize_proxy(choice), 'config'
        # Explicit selection applies to HTTP and WebSockets, regardless of shell exports.
        for key in PROXY_KEYS:
            env[key] = env[key.upper()] = proxy
        return origin
    for key in PROXY_KEYS:
        value = env.get(key) or env.get(key.upper())
        if value:
            env[key] = env[key.upper()] = normalize_proxy(value)
    proxy = (env.get('https_proxy') or env.get('all_proxy') or env.get('http_proxy') or '')
    origin = 'environment'
    if not proxy:
        proxy, origin = system_proxy(), 'GNOME'
    if proxy:
        # Give WSS the same proxy as HTTPS unless the user explicitly selected one.
        for key in ('http_proxy', 'https_proxy', 'all_proxy', 'wss_proxy'):
            if not env.get(key):
                env[key] = env[key.upper()] = proxy
        return origin
    return 'direct'


def redact(text, secrets=()):
    text = str(text)
    text = re.sub(r'AIza[0-9A-Za-z_-]{30,}', '[REDACTED]', text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'((?:https?|socks5?h?)://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', text)
    return re.sub(r'([?&](?:key|access_token)=)[^&\s\'\"]+', r'\1[REDACTED]', text)


class PrivateFormatter(logging.Formatter):
    def __init__(self, secrets=()):
        super().__init__('%(asctime)s %(levelname)s %(message)s')
        self.secrets = secrets

    def format(self, record):
        return redact(super().format(record), self.secrets)


def is_wake_word(text):
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'[\s\u200c\u064b-\u065f\u0670]+', '', text)
    text = text.replace('آ', 'ا').replace('أ', 'ا').replace('إ', 'ا')
    return 'اوبونتو' in text or 'ابونتو' in text


class WakeDetector:
    def __init__(self, recognizer):
        self.recognizer = recognizer

    def feed(self, pcm):
        import json
        final = self.recognizer.AcceptWaveform(pcm)
        result = self.recognizer.Result() if final else self.recognizer.PartialResult()
        words = json.loads(result).get('text' if final else 'partial', '')
        if is_wake_word(words):
            self.recognizer.Reset()
            return True
        return False

    def reset(self):
        self.recognizer.Reset()
