"""Real-API overview batch and reuse test, using a short low-resolution synthetic clip."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

from badminton.agent import Provider, ToolSession
from badminton.config import Settings
from badminton.store import Store
from badminton.video import VideoService, ffmpeg_binary


def main():
    root = Path(tempfile.mkdtemp(prefix='badminton-overview-'))
    settings = Settings(root).initialize()
    store = Store(root / 'agent.db')
    videos = VideoService(settings, store)
    path = settings.webui / 'uploads' / 'overview.mp4'
    subprocess.run([ffmpeg_binary(), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=duration=180:size=160x90:rate=1', '-c:v', 'libx264', '-preset', 'ultrafast', '-y', str(path)], check=True)
    video = videos.register(str(path), 'overview-test', path.name, 'test', 'test')
    run = store.create_run('test', 'test', 'overview-test', 'overview test', [video['id']])
    session = ToolSession(settings, store, videos, run, Provider(settings, store, run['id']))
    first = session.overview(video)
    before = store.one('SELECT COUNT(*) AS count FROM calls')['count']
    second = session.overview(video)
    after = store.one('SELECT COUNT(*) AS count FROM calls')['count']
    assert first == second and before == after == 1
    report = {'test': 'real_api_overview_cache', 'model': settings.model, 'frames': 18, 'cached_without_new_call': True, 'segments': first['segments'], 'usage': store.all('SELECT usage FROM calls')}
    output = Path(os.environ['LOCALAPPDATA']) / 'BadmintonAgent/logs/overview-smoke.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k != 'segments'}, ensure_ascii=True))


if __name__ == '__main__':
    main()
