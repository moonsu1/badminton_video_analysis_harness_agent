"""Local motion candidates, deliberately not presented as a trained shuttle detector."""
import math

import numpy as np
from PIL import Image
from scipy.ndimage import label, find_objects


def trajectory_candidates(frames, seed=None):
    tracks, finished, previous = [], [], None
    for index, frame in enumerate(frames):
        with Image.open(frame['path']) as image:
            current = np.asarray(image.convert('RGB')).astype(np.int16)
        if previous is None:
            previous = current
            continue
        delta = np.max(np.abs(current-previous), axis=2)
        mask = (current.min(axis=2) > 125) & ((current.max(axis=2)-current.min(axis=2)) < 110) & (delta > 25)
        components, _ = label(mask)
        candidates = []
        for number, slices in enumerate(find_objects(components), 1):
            if slices is None:
                continue
            ys, xs = slices
            area = int(np.sum(components[slices] == number))
            w, h = xs.stop-xs.start, ys.stop-ys.start
            if 2 <= area <= 500 and max(w,h) <= 80 and max(w,h) / max(1,min(w,h)) <= 8:
                candidates.append(((xs.start+xs.stop)/2, (ys.start+ys.stop)/2, area))
        candidates = sorted(candidates, key=lambda c:c[2], reverse=True)[:80]
        pairs = []
        for ti, track in enumerate(tracks):
            last = track[-1]
            if index-last['index'] > 3:
                continue
            px, py, gate = last['x'], last['y'], 160
            if len(track) > 1:
                old = track[-2]
                dt = max(last['time']-old['time'], 1e-6)
                scale = (frame['time']-last['time']) / dt
                px += (last['x']-old['x'])*scale
                py += (last['y']-old['y'])*scale
                gate = min(160, 25 + math.hypot(last['x']-old['x'], last['y']-old['y'])*.7*scale)
            for ci, (x,y,area) in enumerate(candidates):
                distance = math.hypot(x-px,y-py)
                if distance <= gate:
                    pairs.append((distance,ti,ci))
        used_tracks, used_candidates = set(), set()
        for distance,ti,ci in sorted(pairs):
            if ti in used_tracks or ci in used_candidates:
                continue
            x,y,_ = candidates[ci]
            tracks[ti].append({'index': index, 'time': frame['time'], 'x': x, 'y': y})
            used_tracks.add(ti);used_candidates.add(ci)
        if seed is None or index <= 4:
            for ci,(x,y,area) in enumerate(candidates):
                if ci in used_candidates:
                    continue
                if seed is not None and math.hypot(x-seed[0]*frame['width'],y-seed[1]*frame['height']) > 100:
                    continue
                tracks.append([{'index': index, 'time': frame['time'], 'x': x, 'y': y}])
        alive = []
        for track in tracks:
            if index-track[-1]['index'] > 3:
                finished.append(track)
            else:
                alive.append(track)
        tracks = alive[-100:]
        previous = current
    finished.extend(tracks)
    usable = [t for t in finished if len(t) >= 5 and math.hypot(t[-1]['x']-t[0]['x'],t[-1]['y']-t[0]['y']) >= 18]
    usable.sort(key=lambda t: (len(t), math.hypot(t[-1]['x']-t[0]['x'],t[-1]['y']-t[0]['y'])), reverse=True)
    width,height = frames[0]['width'], frames[0]['height']
    return {'method': 'unverified_bright_motion_candidates', 'coordinate_system': 'normalized within selected ROI',
            'warning': '다른 흰 물체·사람·라켓일 수 있는 미검증 후보입니다. 이미지에서 셔틀과 대조해야 하며 속도·파워 계측이 아닙니다.',
            'tracks': [[{'time':p['time'],'x':round(p['x']/width,6),'y':round(p['y']/height,6)} for p in t] for t in usable[:3]]}
