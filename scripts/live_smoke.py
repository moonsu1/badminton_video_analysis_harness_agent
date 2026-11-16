"""Small, explicitly synthetic real-API smoke test. Saves evidence, never prints keys."""
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from badminton.agent import native_run
from badminton.config import Settings
from badminton.store import Store
from badminton.video import VideoService, ffmpeg_binary


def main():
    root = Path(tempfile.mkdtemp(prefix='badminton-live-'))
    settings = Settings(root).initialize()
    store = Store(root / 'agent.db')
    videos = VideoService(settings, store)
    path = settings.webui / 'uploads/synthetic.mp4'
    subprocess.run([ffmpeg_binary(), '-v', 'error', '-f', 'lavfi', '-i', 'color=red:duration=2:size=320x180:rate=10', '-f', 'lavfi', '-i', 'color=blue:duration=2:size=320x180:rate=10', '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0', '-c:v', 'libx264', '-y', str(path)], check=True)
    video = videos.register(str(path), 'synthetic', path.name, 'test-user', 'test-chat')
    run = store.create_run('test-user', 'test-chat', uuid.uuid4().hex, '이것은 실제 선수가 없는 합성 시험 영상이다. 선수 지정은 필요 없다. 도구로 0~4초를 초당 1장 확인하여 영상의 두 색이 시간에 따라 어떻게 바뀌는지 원본 시각과 함께 설명해줘. 운동 코칭은 하지 말아줘.', [video['id']])
    store.update_run(run['id'], 'analyzing')
    answer = native_run(settings, store, videos, run)
    store.update_run(run['id'], 'completed', answer=answer)
    report = {'test': 'synthetic_real_openai_api', 'model': settings.model, 'answer': answer,
              'calls': store.all('SELECT purpose,status,usage FROM calls'),
              'tool_events': [e for e in store.events(run['id']) if e['kind'] == 'tool'], 'data_directory': str(root)}
    output = Path(os.environ['LOCALAPPDATA']) / 'BadmintonAgent/logs/live-smoke.json'
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
