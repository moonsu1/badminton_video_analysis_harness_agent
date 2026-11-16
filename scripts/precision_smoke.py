"""Real uploaded match clips, isolated state, real Responses/tool loop. No user chat edits."""
import json
import argparse
import os
import sqlite3
import tempfile
import time
from pathlib import Path

from badminton.agent import native_run
from badminton.config import Settings, load_settings
from badminton.store import Store, encode
from badminton.video import VideoService


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--grip', action='store_true')
    args = parser.parse_args()
    production = load_settings()
    root = Path(tempfile.mkdtemp(prefix='badminton-precision-'))
    settings = Settings(root, model=production.model).initialize()
    store = Store(root/'agent.db')
    videos = VideoService(settings, store)
    selected = []
    with sqlite3.connect('file:'+str(production.root/'agent.db')+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        for filename in ['20261026_201057.mp4', '20261109_195403.mp4']:
            row = dict(db.execute('SELECT * FROM videos WHERE filename=? ORDER BY created DESC LIMIT 1', (filename,)).fetchone())
            store.execute('INSERT INTO videos(id,user_id,chat_id,source_id,filename,path,sha256,info,created) VALUES(?,?,?,?,?,?,?,?,?)',
                (row['id'],'test','precision',row['source_id'],row['filename'],row['path'],row['sha256'],row['info'],time.time()))
            selected.append(row['id'])
    question = ('선수 자세나 그립 분석은 하지 말고 셔틀 궤적만 비교해줘. '
        '20261026_201057.mp4의 30.10초~30.36초, 20261109_195403.mp4의 39.15초~39.40초를 각각 '
        'inspect_trajectory로 원본 연속 판독해줘. 이 두 구간만 확인하면 돼. '
        '첫 영상 box=[0.442708,0.277778,0.78125,0.518519], seed=[0.463,0.315], '
        '둘째 영상 box=[0.546875,0.231481,0.885417,0.555556], seed=[0.12,0.08]를 사용해. '
        '각 파일에 실제 셔틀이 식별되는지, 프레임 사이에서 어느 방향으로 이동하는지 답해줘. '
        '궤적 후보와 시각적으로 확인한 것을 구별하고 속도나 파워 수치를 측정하지 마. '
        '여기서는 선수 지정은 필요 없어. complete_analysis로 사용한 두 파일의 근거를 제출해.')
    # Avoid unrelated negated grip terms in the routing policy of this trajectory-only test.
    question = question.replace('선수 자세나 그립 분석은 하지 말고 ', '')
    if args.grip:
        selected = selected[:1]
        question = ('20261026_201057.mp4의 29.8초~30.2초에서 화면 가까운 쪽 검정 상의에 노란 무늬가 있는 선수가 나야. '
            '이 구간만 원본 연속 프레임으로 보고 손과 손잡이 영역을 확대해서 그립이 실제로 구별되는지 확인해줘. '
            '보이지 않는 손가락/악력은 추측하지 말고 관찰과 한계를 말해줘. 추가 선수 확인 질문은 필요 없어.')
    run = store.create_run('test','precision','real-grip' if args.grip else 'real-pair',question,selected)
    report = {'test':'real_match_native_grip' if args.grip else 'real_match_native_trajectory','model':settings.model,'isolated_data':str(root),'files':[]}
    started = time.monotonic()
    try:
        answer = native_run(settings,store,videos,run)
        evidence = store.cached('evidence:'+run['id']) or []
        for item in evidence:
            sequence = item['sequence'] or {}
            report['files'].append({'filename':item['filename'],'tool':item['tool'],
                'native':item['native'],'frame_count':len(item['frames']),
                'dimensions':[item['frames'][0]['width'],item['frames'][0]['height']],
                'start':item['frames'][0]['time'],'end':item['frames'][-1]['time'],
                'shuttle_visible':sequence.get('shuttle_visible'),
                'grip_visible':sequence.get('grip_visible'),
                'candidate_tracks':len((item.get('tracking') or {}).get('tracks',[])),
                'observations':sequence.get('batches')})
        if args.grip:
            assert any(e['crop'] and e['purpose']=='grip' for e in evidence)
        else:
            assert {e['video_id'] for e in evidence if e['tool']=='inspect_trajectory' and e['native']} == set(selected)
        assert any(e['native'] for e in evidence)
        assert all(e['sequence']['native_frames_analyzed']==len(e['frames']) for e in evidence if e['native'])
        report.update(answer=answer,passed=True)
    except Exception as exc:
        report.update(passed=False,error=str(exc))
        raise
    finally:
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        report['tools']=[json.loads(r['data']) for r in store.all("SELECT data FROM events WHERE kind='tool'")]
        report['calls']=[{'purpose':r['purpose'],'status':r['status'],'usage':json.loads(r['usage'] or '{}')} for r in store.all('SELECT * FROM calls')]
        output=production.root/'logs'/('precision-grip.json' if args.grip else 'precision-upgrade.json')
        output.write_text(encode(report),encoding='utf-8')
        print(json.dumps({'passed':report.get('passed'),'error':report.get('error'),'report':str(output),
            'files':[{k:v for k,v in f.items() if k!='observations'} for f in report['files']],
            'elapsed_seconds':report['elapsed_seconds']},ensure_ascii=False))


if __name__ == '__main__':
    main()
