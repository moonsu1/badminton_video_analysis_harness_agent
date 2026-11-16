import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

import av

from .store import encode


def ffmpeg_binary(name='ffmpeg'):
    override = os.getenv(name.upper() + '_PATH')
    found = Path(override) if override else None
    if found and found.is_file():
        return str(found)
    root = Path(os.environ['LOCALAPPDATA']) / 'hermes/tools'
    candidates = sorted(root.glob(f'ffmpeg-*/bin/{name}.exe'))
    if candidates:
        return str(candidates[-1])
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f'{name}를 찾을 수 없습니다. 상태확인.cmd를 실행해 주세요.')
    return found


def probe(path):
    try:
        result = subprocess.run([ffmpeg_binary('ffprobe'), '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)],
                                capture_output=True, timeout=90, check=True)
        data = json.loads(result.stdout)
        stream = next(s for s in data['streams'] if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic'))
        duration = float(stream.get('duration') or data['format'].get('duration') or 0)
        rotation = float(stream.get('tags', {}).get('rotate', 0))
        for item in stream.get('side_data_list', []):
            if 'rotation' in item:
                rotation = float(item['rotation'])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('invalid duration')
        rate = stream.get('avg_frame_rate', '0/1').split('/')
        fps = float(rate[0]) / max(float(rate[1]), 1)
        return {'duration': duration, 'width': stream['width'], 'height': stream['height'],
                'codec': stream['codec_name'], 'rotation': rotation, 'fps': fps,
                'stream_index': stream['index'], 'start_time': float(stream.get('start_time', 0)),
                'bytes': Path(path).stat().st_size}
    except (subprocess.SubprocessError, StopIteration, KeyError, ValueError) as exc:
        raise ValueError('영상을 읽을 수 없습니다. 손상되지 않은 MP4 또는 MOV를 다시 올려 주세요.') from exc


class VideoService:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def register(self, source, source_id, filename, user, chat):
        existing = self.store.one('SELECT id FROM videos WHERE user_id=? AND chat_id=? AND source_id=?', (user, chat, source_id))
        if existing:
            return self.store.video(existing['id'], user, chat)
        path = Path(source).resolve(strict=True)
        allowed = (self.settings.webui / 'uploads').resolve()
        if not path.is_relative_to(allowed) or not path.is_file():
            raise PermissionError('허용된 업로드 폴더의 영상만 등록할 수 있습니다.')
        if Path(filename).suffix.lower() not in {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}:
            raise ValueError('MP4 또는 MOV 영상을 첨부해 주세요.')
        size = path.stat().st_size
        if not 0 < size <= self.settings.max_bytes:
            raise ValueError(f'빈 파일 또는 크기 한도({self.settings.max_bytes / 1024**3:g}GiB)를 초과한 영상입니다. 촬영 해상도나 영상 길이를 줄여 주세요.')
        if shutil.disk_usage(self.settings.root).free < size + 1024**3:
            raise ValueError('디스크 공간이 부족합니다. 영상 크기 외에 최소 1GiB의 여유 공간이 필요합니다.')
        info = probe(path)
        if info['duration'] > self.settings.max_seconds:
            raise ValueError('60분을 초과한 영상입니다. 나누어 올려 주세요.')
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(8 * 1024**2), b''):
                digest.update(chunk)
        # Keep a separate immutable original; deleting an OWUI attachment cannot break a saved run.
        video_id = uuid.uuid4().hex
        destination = self.settings.root / 'originals' / (video_id + Path(filename).suffix.lower())
        staging = destination.with_suffix('.partial')
        try:
            shutil.copyfile(path, staging)
            os.replace(staging, destination)
            self.store.execute('INSERT INTO videos(id,user_id,chat_id,source_id,filename,path,sha256,info,created) VALUES(?,?,?,?,?,?,?,?,?)',
                               (video_id, user, chat, source_id, Path(filename).name, str(destination), digest.hexdigest(), encode(info), time.time()))
        except BaseException as exc:
            staging.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            if isinstance(exc, sqlite3.IntegrityError):
                existing = self.store.one('SELECT id FROM videos WHERE user_id=? AND chat_id=? AND source_id=?', (user, chat, source_id))
                if existing:
                    return self.store.video(existing['id'], user, chat)
            raise
        return self.store.video(video_id, user, chat)

    def frames(self, video, start, end, fps=4, maximum=120, crop=None, check=None, resolution='original'):
        info = video['info']
        if not all(math.isfinite(float(v)) for v in [start, end, fps]):
            raise ValueError('시간과 프레임 간격은 유한한 숫자여야 합니다.')
        native = fps == 0
        if resolution not in {'original', 'preview'}:
            raise ValueError('지원되지 않는 해상도 모드입니다.')
        if not 0 <= start < end <= info['duration'] + .05 or not (native or .01 <= fps <= min(60, max(info['fps'], 1))):
            raise ValueError('영상 범위 또는 원본 프레임레이트를 벗어났습니다.')
        if native and end - start > 3:
            raise ValueError('원본 연속 장면은 한 번에 3초 이내로 지정해 주세요. 원본 프레임을 생략하지 않습니다.')
        count = math.ceil((end - start) * fps - 1e-7) if not native else 0
        if count > maximum:
            raise ValueError(f'한 번에 {maximum}장까지 확인할 수 있습니다. 구간이나 추출 밀도를 줄여 주세요.')
        if crop is not None and (len(crop) != 4 or not all(math.isfinite(float(x)) for x in crop) or not 0 <= crop[0] < crop[2] <= 1 or not 0 <= crop[1] < crop[3] <= 1):
            raise ValueError('확대 범위는 0~1 사이의 [왼쪽, 위, 오른쪽, 아래]여야 합니다.')
        params = [video['sha256'], start, end, fps, crop, resolution, 'pts-native-v2']
        key = hashlib.sha256(encode(params).encode()).hexdigest()
        directory = self.settings.root / 'cache' / key
        manifest = directory / 'manifest.json'
        if manifest.exists():
            data = json.loads(manifest.read_text(encoding='utf-8'))
            if all(Path(f['path']).exists() for f in data):
                if len(data) > maximum:
                    raise ValueError(f'원본 프레임이 {maximum}장을 초과합니다. 구간을 더 짧게 요청해 주세요.')
                os.utime(manifest, None)
                for f in data:
                    f.update(video_id=video.get('id'), filename=video.get('filename', Path(video['path']).name))
                return data
        directory.mkdir(parents=True, exist_ok=True)
        frames = []
        previous_pts = None

        def save(frame, timestamp, target):
            if len(frames) >= maximum:
                raise ValueError(f'원본 프레임이 {maximum}장을 초과합니다. 생략하지 않고 구간을 더 짧게 요청해 주세요.')
            image = frame.to_image()
            if info['rotation']:
                image = image.rotate(info['rotation'], expand=True)
            if crop is not None:
                w, h = image.size
                bounds = tuple(round(v * (w if n % 2 == 0 else h)) for n, v in enumerate(crop))
                if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                    raise ValueError('확대 범위가 1픽셀보다 작습니다.')
                image = image.crop(bounds)
            if resolution == 'preview':
                image.thumbnail((1280, 1280))
                frame_path = directory / f'{len(frames):04d}.jpg'
                image.save(frame_path, quality=80)
            else:
                frame_path = directory / f'{len(frames):04d}.png'
                image.save(frame_path)
            frames.append({'path': str(frame_path), 'time': round(timestamp, 6), 'target': round(target, 6),
                           'width': image.width, 'height': image.height, 'sampling': 'native' if native else 'sampled',
                           'resolution': resolution, 'crop': crop, 'video_id': video.get('id'),
                           'filename': video.get('filename', Path(video['path']).name)})

        with av.open(video['path']) as container:
            stream = next(s for s in container.streams.video if s.index == info['stream_index'])
            origin = float((stream.start_time or 0) * stream.time_base)
            if native:
                # Decode once in presentation order; no resampling, synthetic frames or average-fps rounding.
                container.seek(int((origin + start) / stream.time_base), stream=stream, backward=True)
                for frame in container.decode(stream):
                    if check:
                        check()
                    if frame.pts is None:
                        continue
                    timestamp = float(frame.pts * frame.time_base) - origin
                    if timestamp + 1e-6 < start:
                        continue
                    if timestamp >= end:
                        break
                    if frame.pts != previous_pts:
                        save(frame, timestamp, timestamp)
                        previous_pts = frame.pts
            for i in range(count):
                if check:
                    check()
                target = start + i / fps
                container.seek(int((origin + target) / stream.time_base), stream=stream, backward=True)
                for frame in container.decode(stream):
                    if frame.pts is None:
                        continue
                    timestamp = float(frame.pts * frame.time_base) - origin
                    if timestamp + 1e-6 < target:
                        continue
                    if timestamp >= end or frame.pts == previous_pts:
                        break
                    save(frame, timestamp, target)
                    previous_pts = frame.pts
                    break
        if not frames:
            raise ValueError('이 구간에서 장면을 읽지 못했습니다. 다른 시간 구간을 선택해 주세요.')
        temp = manifest.with_suffix('.tmp')
        temp.write_text(encode(frames), encoding='utf-8')
        os.replace(temp, manifest)
        return frames

    def cleanup(self):
        # Only regenerated images; never delete originals or analysis records automatically.
        cache = (self.settings.root / 'cache').resolve()
        for item in cache.iterdir():
            manifest = item / 'manifest.json'
            stamp = manifest.stat().st_mtime if manifest.exists() else item.stat().st_mtime
            if item.is_dir() and item.resolve().is_relative_to(cache) and time.time() - stamp > 7 * 86400:
                shutil.rmtree(item)
