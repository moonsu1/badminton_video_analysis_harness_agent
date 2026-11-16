import base64
import math
from pathlib import Path


def image_parts(frames):
    content = []
    for frame in frames:
        label = f"파일 {frame.get('filename', '영상')} | video_id={frame.get('video_id', '')} | 원본 시각 {frame['time']:.6f}초"
        if frame.get('frame_id'):
            label = f"frame_id={frame['frame_id']} | " + label
        if frame.get('crop') is not None:
            label += f" | 원본 영역 {frame['crop']}"
        content.append({'type': 'input_text', 'text': label})
        mime = 'image/png' if Path(frame['path']).suffix.lower() == '.png' else 'image/jpeg'
        data = base64.b64encode(Path(frame['path']).read_bytes()).decode('ascii')
        content.append({'type': 'input_image', 'image_url': f'data:{mime};base64,' + data,
                        'detail': 'original' if frame.get('resolution') == 'original' else 'auto'})
    return content


def image_tokens(frame):
    # Conservative sizing estimate used only to bound context, not an invoice calculator.
    return math.ceil(frame['width'] / 32) * math.ceil(frame['height'] / 32) * 1.2
