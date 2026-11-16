import json
import subprocess
import webbrowser

import httpx

from badminton import operations


def test_launch_waits_for_both_services_before_opening_browser(system,monkeypatch):
    settings,_,_=system
    events=[]
    monkeypatch.setattr(operations,'load_settings',lambda:settings)
    monkeypatch.setattr(operations,'start',lambda:events.append('start'))
    monkeypatch.setattr(operations,'resume_phone_connection',lambda:events.append('network'))
    monkeypatch.setattr(operations,'wait_ready',lambda s:events.append('ready') or True)
    monkeypatch.setattr(webbrowser,'open',lambda url:events.append(url) or True)
    monkeypatch.setattr(operations,'phone_status',lambda s:events.append('phone_status') or 'ready')
    assert operations.launch()==0
    assert events==['start','network','ready','http://127.0.0.1:8080','phone_status']


def test_failed_startup_does_not_open_browser(system,monkeypatch):
    settings,_,_=system
    opened=[]
    monkeypatch.setattr(operations,'load_settings',lambda:settings)
    monkeypatch.setattr(operations,'start',lambda:None)
    monkeypatch.setattr(operations,'resume_phone_connection',lambda:None)
    monkeypatch.setattr(operations,'wait_ready',lambda s:False)
    monkeypatch.setattr(webbrowser,'open',lambda url:opened.append(url))
    assert operations.launch()==1
    assert not opened


def test_readiness_requires_ui_and_backend(system,monkeypatch):
    settings,_,_=system
    requests=[]
    def handle(request):
        requests.append(request.url.port)
        return httpx.Response(200 if request.url.port==settings.port else 503)
    real_client=httpx.Client
    monkeypatch.setattr(operations.httpx,'Client',lambda **kwargs:real_client(transport=httpx.MockTransport(handle),**kwargs))
    assert operations.wait_ready(settings,timeout=0) is False
    assert requests==[settings.port,8080]


def test_network_recovery_only_reloads_saved_active_profile(monkeypatch):
    commands=[]
    monkeypatch.setattr(operations,'tailscale',lambda:'tailscale')
    def run(args,**kwargs):
        commands.append(args)
        if args[1]=='status':return subprocess.CompletedProcess(args,0,json.dumps({'BackendState':'NoState'}),'')
        if args[2]=='--list':return subprocess.CompletedProcess(args,0,'ID Tailnet Account\n22e4 network user*\n33f5 other another\n','')
        return subprocess.CompletedProcess(args,0,'','')
    monkeypatch.setattr(operations.subprocess,'run',run)
    operations.resume_phone_connection()
    assert commands[-1]==['tailscale','switch','22e4']


def test_network_recovery_does_not_change_stopped_or_logged_out_profile(monkeypatch):
    monkeypatch.setattr(operations,'tailscale',lambda:'tailscale')
    for state in ['Stopped','NeedsLogin','Running']:
        commands=[]
        def run(args,**kwargs):
            commands.append(args)
            return subprocess.CompletedProcess(args,0,json.dumps({'BackendState':state}),'')
        monkeypatch.setattr(operations.subprocess,'run',run)
        operations.resume_phone_connection()
        assert commands==[['tailscale','status','--json']]
