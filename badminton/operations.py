import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .config import load_settings


def start():
    from .service import WorkerLease
    settings = load_settings().initialize()
    lease = WorkerLease(settings.root / 'startup.lock')
    try:
        lease.acquire()
    except RuntimeError:
        print('이미 실행을 준비하고 있습니다.', flush=True)
        return
    try:
        _start_services(settings)
    finally:
        lease.release()


def _start_services(settings):
    from .bootstrap import environment, PROJECT
    env = environment(settings)
    with httpx.Client(timeout=2, trust_env=False) as client:
        for name, module, port in [('agent', 'badminton.service', settings.port), ('webui', 'badminton.webui', 8080)]:
            try:
                if client.get(f'http://127.0.0.1:{port}/health').status_code == 200:
                    print(name + ': running')
                    continue
            except httpx.HTTPError:
                pass
            pid_file = settings.root / (name + '.pid')
            if pid_file.exists():
                import psutil
                try:
                    process = psutil.Process(int(pid_file.read_text()))
                    if module in ' '.join(process.cmdline()) and Path(process.cwd()).resolve() == PROJECT.resolve():
                        print(name + ': starting; check status shortly')
                        continue
                except (psutil.NoSuchProcess, ValueError):
                    pass
            with (settings.root / 'logs' / (name + '.log')).open('a', encoding='utf-8') as output, (settings.root / 'logs' / (name + '-error.log')).open('a', encoding='utf-8') as error:
                process = subprocess.Popen([sys.executable, '-m', module], cwd=PROJECT, env=env, stdout=output, stderr=error,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            pid_file.write_text(str(process.pid))
            print(name + ': starting')
    print('Local UI: http://127.0.0.1:8080 - first startup may take a few minutes.')


def wait_ready(settings, timeout=180):
    targets = {'백엔드': settings.port, '채팅 화면': 8080}
    deadline = time.monotonic() + timeout
    announced = None
    with httpx.Client(timeout=2, trust_env=False) as client:
        while True:
            pending = dict(targets)
            for name, port in targets.items():
                try:
                    response = client.get(f'http://127.0.0.1:{port}/health')
                    if response.status_code == 200:
                        del pending[name]
                except httpx.HTTPError:
                    pass
            if not pending:
                return True
            names = ', '.join(pending)
            if announced != names:
                print(names + ' 준비를 기다리는 중입니다…', flush=True)
                announced = names
            if time.monotonic() >= deadline:
                print(names + '가 아직 준비되지 않았습니다. 상태확인.cmd와 아래 로그를 확인해 주세요:', flush=True)
                print(str(settings.root / 'logs'), flush=True)
                return False
            time.sleep(1)


def resume_phone_connection():
    """Recover a saved active profile if the Windows daemon is stuck in NoState."""
    binary = tailscale()
    if not binary:
        return
    try:
        state = subprocess.run([binary, 'status', '--json'], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=8)
        if json.loads(state.stdout).get('BackendState') != 'NoState':
            return
        profiles = subprocess.run([binary, 'switch', '--list'], capture_output=True, text=True,
                                  encoding='utf-8', errors='replace', timeout=8)
        if profiles.returncode:
            return
        active = [line.split()[0] for line in profiles.stdout.splitlines()[1:] if line.rstrip().endswith('*')]
        if len(active) == 1:
            subprocess.run([binary, 'switch', active[0]], capture_output=True, timeout=10)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass  # Local chat remains usable even if the private network is unavailable.


def phone_status(settings):
    """Read-only check: launch must not restart services or change Tailscale settings."""
    binary = tailscale()
    if not binary:
        return '휴대폰 접속: Tailscale이 설치되어 있지 않습니다.'
    try:
        process = subprocess.run([binary, 'status', '--json'], capture_output=True, text=True,
                                 encoding='utf-8', errors='replace', timeout=8)
        data = json.loads(process.stdout)
        state = data.get('BackendState')
        if state != 'Running':
            return f'휴대폰 접속: Tailscale 연결 대기 중({state or "확인 필요"}). PC의 Tailscale 앱을 확인해 주세요.'
        saved = settings.root / 'remote-url.txt'
        if not saved.exists():
            return '휴대폰 주소 설정은 휴대폰연결.cmd에서 할 수 있습니다.'
        url = saved.read_text().strip()
        with httpx.Client(timeout=5, trust_env=False) as client:
            response = client.get(url + '/health')
        if response.status_code == 200:
            return '휴대폰 접속 준비 완료: ' + url
        return '휴대폰 주소 확인이 필요합니다. 휴대폰연결.cmd를 실행해 주세요.'
    except (OSError, ValueError, subprocess.TimeoutExpired, httpx.HTTPError):
        return '휴대폰 접속 상태를 확인하지 못했습니다. PC 채팅은 사용할 수 있습니다. 상태확인.cmd로 확인해 주세요.'


def launch():
    import webbrowser
    settings = load_settings().initialize()
    print('배드민턴 코치를 시작합니다. 처음 켤 때는 잠시 걸릴 수 있습니다.', flush=True)
    start()
    resume_phone_connection()
    if not wait_ready(settings):
        return 1
    print('백엔드와 채팅 화면 준비 완료.', flush=True)
    if not webbrowser.open('http://127.0.0.1:8080'):
        print('브라우저에서 http://127.0.0.1:8080 을 열어 주세요.', flush=True)
    print(phone_status(settings), flush=True)
    print('이 실행 창을 닫아도 서버는 계속 실행됩니다.', flush=True)
    return 0


def shortcut():
    import win32com.client
    from .bootstrap import PROJECT
    shell = win32com.client.Dispatch('WScript.Shell')
    path = Path(shell.SpecialFolders('Desktop')) / '배드민턴 코치 실행.lnk'
    link = shell.CreateShortCut(str(path))
    link.TargetPath = str(PROJECT / '지금실행.cmd')
    link.WorkingDirectory = str(PROJECT)
    link.Description = '백엔드와 Open WebUI를 시작하고 채팅 화면을 엽니다.'
    link.WindowStyle = 1
    link.save()
    print('바탕화면 바로가기: ' + str(path))


def setup():
    from .bootstrap import main
    main()


def autostart():
    import win32com.client
    from .bootstrap import PROJECT
    startup = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup'
    shell = win32com.client.Dispatch('WScript.Shell')
    shortcut = shell.CreateShortCut(str(startup / 'BadmintonAgent.lnk'))
    shortcut.TargetPath = str(Path(sys.executable).with_name('pythonw.exe'))
    shortcut.Arguments = '-m badminton.operations start'
    shortcut.WorkingDirectory = str(PROJECT)
    shortcut.WindowStyle = 7
    shortcut.save()
    print('로그인 시 자동 실행을 설정했습니다.')


def no_autostart():
    shortcut = Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs/Startup/BadmintonAgent.lnk'
    shortcut.unlink(missing_ok=True)
    print('자동 실행을 해제했습니다.')


def status():
    settings = load_settings().initialize()
    result = {'data_directory': str(settings.root), 'runtime': settings.runtime, 'model': settings.model}
    try:
        from .config import api_key
        result['api_key_registered'] = bool(api_key())
    except Exception:
        result['api_key_registered'] = False
    with httpx.Client(timeout=3, trust_env=False) as client:
        for name, port in [('agent', settings.port), ('webui', 8080)]:
            try:
                response = client.get(f'http://127.0.0.1:{port}/health')
                result[name] = 'running' if response.status_code == 200 else 'not_ready'
            except httpx.HTTPError:
                result[name] = 'stopped'
    result['free_disk_GiB'] = round(shutil.disk_usage(settings.root).free / 1024**3, 1)
    result['tailscale_installed'] = bool(tailscale())
    remote = settings.root / 'remote-url.txt'
    result['phone_url'] = remote.read_text().strip() if remote.exists() else 'not configured'
    result['logs_directory'] = str(settings.root / 'logs')
    import sqlite3
    db_path = settings.webui / 'webui.db'
    if db_path.exists():
        with sqlite3.connect(db_path) as db:
            try:
                result['admin_account_created'] = db.execute('SELECT COUNT(*) FROM user').fetchone()[0] > 0
            except sqlite3.OperationalError:
                result['admin_account_created'] = False
    if (settings.root / 'agent.db').exists():
        from .store import Store
        store = Store(settings.root / 'agent.db')
        result['queued_or_running'] = store.one("SELECT COUNT(*) AS count FROM runs WHERE status IN ('queued','preparing','analyzing')")['count']
        result['api_calls'] = store.all('SELECT status,COUNT(*) AS count FROM calls GROUP BY status')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    power = subprocess.run(['powercfg', '/query', 'SCHEME_CURRENT', 'SUB_SLEEP', 'STANDBYIDLE'], capture_output=True)
    print(power.stdout.decode('utf-8', errors='replace') if not power.returncode else 'Sleep settings unavailable.')


def tailscale():
    path = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Tailscale/tailscale.exe'
    return str(path) if path.exists() else shutil.which('tailscale')


def remote():
    binary = tailscale()
    if not binary:
        print('Tailscale을 설치해 주세요: https://tailscale.com/download/windows')
        return 1
    process = subprocess.run([binary, 'status', '--json'], capture_output=True, text=True, encoding='utf-8', errors='replace')
    try:
        data = json.loads(process.stdout)
    except ValueError:
        print('Tailscale 서비스를 시작한 뒤 다시 실행해 주세요.')
        return 1
    if data.get('BackendState') != 'Running':
        print('PC의 Tailscale 앱에서 로그인해 주세요. 휴대폰에도 같은 계정으로 로그인한 뒤 이 파일을 다시 실행하세요.')
        return 1
    try:
        process = subprocess.run([binary, 'serve', '--bg', '--yes', 'http://127.0.0.1:8080'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15)
    except subprocess.TimeoutExpired as exc:
        import webbrowser
        text = '\n'.join(v.decode('utf-8', errors='replace') if isinstance(v, bytes) else (v or '') for v in (exc.stdout, exc.stderr))
        urls = re.findall(r'https://login\.tailscale\.com/[^\s]+', text)
        if urls:
            webbrowser.open(urls[0])
            print('Tailscale의 HTTPS/Serve 활성화 확인 화면을 열었습니다. 화면에서 활성화한 뒤 휴대폰연결.cmd를 다시 실행해 주세요.')
        else:
            print('Tailscale 주소 설정이 지연되고 있습니다. Tailscale 관리 화면의 HTTPS 설정을 확인한 뒤 다시 실행해 주세요.')
        return 1
    if process.returncode:
        print('Tailscale Serve 설정이 필요합니다. 관리자 터미널에서 다음 명령을 실행해 주세요:')
        print('tailscale serve --bg http://127.0.0.1:8080')
        print(process.stdout + process.stderr)
        return 1
    dns = data.get('Self', {}).get('DNSName', '').rstrip('.')
    if not dns:
        print('Tailscale 관리 화면에서 MagicDNS를 켜 주세요.')
        return 1
    url = 'https://' + dns
    settings = load_settings().initialize()
    (settings.root / 'remote-url.txt').write_text(url, encoding='utf-8')
    print('휴대폰 접속 주소: ' + url)
    from .store import Store
    store = Store(settings.root / 'agent.db')
    if store.one("SELECT id FROM runs WHERE status IN ('queued','preparing','analyzing') LIMIT 1"):
        print('분석이 진행 중입니다. 완료 후 중지.cmd → 지금실행.cmd를 실행하면 주소 설정이 적용됩니다.')
    else:
        stop()
        start()
        print('휴대폰 주소 설정을 적용하여 서버를 다시 시작했습니다.')
    return 0


def stop():
    import psutil
    from .bootstrap import PROJECT
    root = load_settings().root
    for name, module in [('agent', 'badminton.service'), ('webui', 'badminton.webui')]:
        pid_file = root / (name + '.pid')
        if not pid_file.exists():
            continue
        try:
            process = psutil.Process(int(pid_file.read_text().strip()))
            if module not in ' '.join(process.cmdline()) or Path(process.cwd()).resolve() != PROJECT.resolve():
                print(name + ': PID belongs to another process; left unchanged.')
                continue
            children = process.children(recursive=True)
            for child in reversed(children):
                child.terminate()
            process.terminate()
            psutil.wait_procs([process] + children, timeout=5)
        except (psutil.NoSuchProcess, ValueError):
            pass
        pid_file.unlink(missing_ok=True)
    print('서비스를 중지했습니다. 실행 중이던 분석은 재시작 시 중단 상태로 표시됩니다.')


def credentials():
    import tkinter as tk
    from tkinter import messagebox
    import keyring
    app = tk.Tk()
    app.title('배드민턴 코치 API 인증')
    tk.Label(app, text='새 OpenAI API 키 (기존 키 값은 표시하지 않습니다)').pack(padx=20, pady=10)
    entry = tk.Entry(app, show='*', width=55)
    entry.pack(padx=20, pady=8)
    def save():
        value = entry.get().strip()
        if not value:
            messagebox.showerror('등록', 'API 키를 입력해 주세요.')
            return
        keyring.set_password('BadmintonAgent', 'openai_api_key', value)
        entry.delete(0, 'end')
        messagebox.showinfo('등록', 'Windows 자격 증명에 저장했습니다. 진행 중인 작업은 종료 후 새 질문부터 사용합니다.')
    tk.Button(app, text='등록', command=save).pack(pady=12)
    app.mainloop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'launch', 'shortcut', 'setup', 'status', 'stop', 'remote', 'credentials', 'autostart', 'no-autostart'])
    action = parser.parse_args().action
    sys.exit(globals()[action.replace('-', '_')]() or 0)
