import json
import subprocess
import webbrowser

from badminton import operations


def test_remote_timeout_opens_feature_page_without_crashing(system, monkeypatch):
    settings, store, _ = system
    monkeypatch.setattr(operations, 'load_settings', lambda: settings)
    monkeypatch.setattr(operations, 'tailscale', lambda: 'tailscale')
    opened = []
    monkeypatch.setattr(webbrowser, 'open', lambda url: opened.append(url))
    def call(args, **kwargs):
        if args[1] == 'status':
            return subprocess.CompletedProcess(args, 0, json.dumps({'BackendState': 'Running'}), '')
        raise subprocess.TimeoutExpired(args, 15, output=b'', stderr=b'Enable Serve: https://login.tailscale.com/f/serve?node=test')
    monkeypatch.setattr(operations.subprocess, 'run', call)
    assert operations.remote() == 1
    assert opened == ['https://login.tailscale.com/f/serve?node=test']
    assert not (settings.root / 'remote-url.txt').exists()


def test_remote_saves_https_url_and_restarts_idle_services(system, monkeypatch):
    settings, store, _ = system
    monkeypatch.setattr(operations, 'load_settings', lambda: settings)
    monkeypatch.setattr(operations, 'tailscale', lambda: 'tailscale')
    actions = []
    monkeypatch.setattr(operations, 'stop', lambda: actions.append('stop'))
    monkeypatch.setattr(operations, 'start', lambda: actions.append('start'))
    def call(args, **kwargs):
        if args[1] == 'status':
            return subprocess.CompletedProcess(args, 0, json.dumps({'BackendState': 'Running', 'Self': {'DNSName': 'badminton-pc.test.ts.net.'}}), '')
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(operations.subprocess, 'run', call)
    assert operations.remote() == 0
    assert (settings.root / 'remote-url.txt').read_text() == 'https://badminton-pc.test.ts.net'
    assert actions == ['stop', 'start']
