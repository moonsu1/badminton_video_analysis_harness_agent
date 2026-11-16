import subprocess

import pytest

from badminton.config import Settings
from badminton.store import Store
from badminton.video import VideoService, ffmpeg_binary


@pytest.fixture
def system(tmp_path):
    settings = Settings(tmp_path).initialize()
    store = Store(tmp_path / 'agent.db')
    return settings, store, VideoService(settings, store)


@pytest.fixture
def clip(system):
    settings, store, videos = system
    path = settings.webui / 'uploads' / 'test.mp4'
    subprocess.run([ffmpeg_binary(), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=duration=3:size=320x180:rate=12', '-c:v', 'libx264', '-preset', 'ultrafast', '-y', str(path)], check=True)
    return videos.register(str(path), 'file1', 'test.mp4', 'user1', 'chat1')
