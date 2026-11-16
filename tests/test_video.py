import json
import subprocess
from pathlib import Path

import pytest

from badminton.video import ffmpeg_binary


def test_pts_and_cache(system, clip):
    _, _, videos = system
    frames = videos.frames(clip, .3, 1.8, fps=4)
    assert len(frames) == 6
    assert all(0 <= f['time'] - f['target'] < .1 for f in frames)
    assert [f['time'] for f in frames] == sorted(set(f['time'] for f in frames))
    assert frames == videos.frames(clip, .3, 1.8, fps=4)


def test_registration_ownership_and_bounds(system, clip):
    settings, store, videos = system
    with pytest.raises(PermissionError):
        store.video(clip['id'], 'other', 'chat1')
    with pytest.raises(PermissionError):
        store.video(clip['id'], 'user1', 'otherchat')
    assert videos.register(clip['path'], 'file1', 'test.mp4', 'user1', 'chat1')['id'] == clip['id']
    with pytest.raises(PermissionError):
        videos.register(clip['path'], 'newfile', 'test.mp4', 'user1', 'chat1')
    with pytest.raises(ValueError):
        videos.frames(clip, 0, 3, fps=60)
    with pytest.raises(ValueError):
        videos.frames(clip, -1, 1)
    with pytest.raises(ValueError):
        videos.frames(clip, 0, 1, crop=[0, 0, 2, 1])


@pytest.mark.parametrize('seconds', [600, 2400])
def test_long_video_seeking(system, seconds):
    settings, _, videos = system
    path = settings.webui / 'uploads' / f'{seconds}.mp4'
    subprocess.run([ffmpeg_binary(), '-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=duration={seconds}:size=160x90:rate=1', '-c:v', 'libx264', '-preset', 'ultrafast', '-y', str(path)], check=True)
    video = videos.register(str(path), str(seconds), path.name, 'user1', 'chat1')
    frames = videos.frames(video, 0, seconds, fps=.1, maximum=360)
    assert len(frames) == seconds // 10
    assert abs(frames[-1]['time'] - (seconds - 10)) < 1


def test_hevc_rotation_and_crop(system, clip):
    settings, _, videos = system
    hevc = settings.webui / 'uploads' / 'hevc.mp4'
    subprocess.run([ffmpeg_binary(), '-v', 'error', '-i', clip['path'], '-c:v', 'libx265', '-preset', 'ultrafast', '-x265-params', 'log-level=error', '-y', str(hevc)], check=True)
    rotated = settings.webui / 'uploads' / 'rotate.mov'
    subprocess.run([ffmpeg_binary(), '-v', 'error', '-display_rotation:v:0', '90', '-i', str(hevc), '-c', 'copy', '-y', str(rotated)], check=True)
    video = videos.register(str(rotated), 'rotated', 'rotate.mov', 'user1', 'chat1')
    assert video['info']['codec'] == 'hevc'
    assert abs(video['info']['rotation']) == 90
    frame = videos.frames(video, 0, .5, fps=1)[0]
    assert frame['height'] > frame['width']
    reference = settings.root / 'ffmpeg-reference.png'
    subprocess.run([ffmpeg_binary(), '-v', 'error', '-i', str(rotated), '-frames:v', '1', '-y', str(reference)], check=True)
    from PIL import Image, ImageChops, ImageStat
    with Image.open(frame['path']) as actual, Image.open(reference) as expected:
        assert max(ImageStat.Stat(ImageChops.difference(actual.convert('RGB'), expected.convert('RGB'))).mean) < 12
    cropped = videos.frames(video, 0, .5, fps=1, crop=[0, 0, .5, 1])[0]
    assert cropped['width'] == round(frame['width'] / 2)
    from badminton.review import extract_match_frames
    sampled=extract_match_frames(settings,video)
    assert all(f['height']>f['width'] and f['crop'] is None for f in sampled)
    assert len(sampled)==6


def test_corrupt_upload(system):
    settings, _, videos = system
    path = settings.webui / 'uploads' / 'broken.mp4'
    path.write_bytes(b'not a movie')
    with pytest.raises(ValueError, match='영상을 읽을 수 없습니다'):
        videos.register(str(path), 'broken', path.name, 'user1', 'chat1')


def test_vfr_preserves_original_times(system, clip):
    settings, _, videos = system
    path = settings.webui / 'uploads' / 'vfr.mp4'
    subprocess.run([ffmpeg_binary(), '-v', 'error', '-i', clip['path'], '-vf', "select='if(lt(t,1),not(mod(n,2)),not(mod(n,3)))'", '-fps_mode', 'vfr', '-c:v', 'libx264', '-y', str(path)], check=True)
    video = videos.register(str(path), 'vfr', path.name, 'user1', 'chat1')
    frames = videos.frames(video, 0, 2.5, fps=2)
    assert all(0 <= f['time'] - f['target'] < .3 for f in frames)
    from badminton.review import extract_match_frames
    sampled=extract_match_frames(settings,video)
    assert all(0 <= f['time']-f['target']<.3 for f in sampled)
    assert len({f['time'] for f in sampled})==len(sampled)


def test_upload_size_limit_precedes_decoding(system):
    settings, _, videos = system
    settings.max_bytes = 1024
    path = settings.webui / 'uploads' / 'too-large.mp4'
    with path.open('wb') as f:
        f.seek(settings.max_bytes)
        f.write(b'0')
    with pytest.raises(ValueError, match='초과'):
        videos.register(str(path), 'large', path.name, 'user1', 'chat1')
