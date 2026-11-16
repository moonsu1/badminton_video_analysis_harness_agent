"""CLI for the same PTS-preserving frame extraction used by the agent."""
import argparse
import hashlib
import json
from pathlib import Path

from badminton.config import Settings
from badminton.video import VideoService, probe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('video', type=Path)
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--duration', type=float, default=30)
    parser.add_argument('--fps', type=float, default=1)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    path = args.video.resolve(strict=True)
    digest = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(8 * 1024**2), b''):
            digest.update(block)
    video = {'path': str(path), 'sha256': digest.hexdigest(), 'info': probe(path)}
    settings = Settings(args.output.resolve()).initialize()
    frames = VideoService(settings, None).frames(video, args.start, min(args.start + args.duration, video['info']['duration']), args.fps)
    print(json.dumps({'frames': frames}, ensure_ascii=True))


if __name__ == '__main__':
    main()
