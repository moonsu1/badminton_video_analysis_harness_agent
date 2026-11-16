"""Replay the user's failed plain two-video question with isolated state and real tools."""
import argparse
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from badminton.agent import native_run
from badminton.config import Settings,load_settings
from badminton.store import Store,encode
from badminton.video import VideoService


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-run',default='9d754e25586f4e59b65d9a5eb424b6fc')
    args=parser.parse_args()
    production=load_settings()
    settings=Settings(Path(tempfile.mkdtemp(prefix='badminton-smash-')),model=production.model).initialize()
    store=Store(settings.root/'agent.db')
    with sqlite3.connect('file:'+str(production.root/'agent.db')+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        source=dict(db.execute('SELECT * FROM runs WHERE id=?',(args.source_run,)).fetchone())
        ids=json.loads(source['video_ids'])
        for video_id in ids:
            v=dict(db.execute('SELECT * FROM videos WHERE id=?',(video_id,)).fetchone())
            store.execute('INSERT INTO videos(id,user_id,chat_id,source_id,filename,path,sha256,info,player,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (v['id'],'test','smash',v['source_id'],v['filename'],v['path'],v['sha256'],v['info'],v['player'],time.time()))
            # Exactly the existing coarse overview may be reused, as in a normal follow-up.
            key=f'overview-v2:{video_id}:{settings.model}'
            cached=db.execute('SELECT data FROM observations WHERE cache_key=?',(key,)).fetchone()
            if cached:store.cache(key,video_id,'overview',json.loads(cached['data']))
    run=store.create_run('test','smash','plain-comparison',source['question'],ids)
    report={'source_run':args.source_run,'model':settings.model,'isolated_data':str(settings.root),
            'question_unchanged':True,'provided_extra_timestamps_or_rois':False}
    started=time.monotonic()
    try:
        answer=native_run(settings,store,VideoService(settings,store),run)
        store.update_run(run['id'],'completed',answer=answer)
        evidence=store.cached('evidence:'+run['id']) or []
        report['response_kind']=store.run(run['id'])['response_kind']
        report['evidence']=[{'filename':e['filename'],'video_id':e['video_id'],'native':e['native'],
            'frames':len(e['frames']),'start':e['frames'][0]['time'],'end':e['frames'][-1]['time'],
            'server_pts':all(b.get('time_source')=='server_frame_pts' for b in e['sequence']['batches']) if e['sequence'] else False} for e in evidence]
        assert report['response_kind']=='analysis','The plain comparison did not reach an analyzed answer'
        assert {e['video_id'] for e in report['evidence'] if e['native']}==set(ids)
        assert all(e['server_pts'] for e in report['evidence'] if e['native'])
        report.update(passed=True,answer=answer)
    except Exception as exc:
        report.update(passed=False,error=str(exc))
        raise
    finally:
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        report['tools']=[json.loads(e['data']) for e in store.all("SELECT data FROM events WHERE kind='tool'")]
        report['calls']=[{'purpose':r['purpose'],'status':r['status'],'usage':json.loads(r['usage'] or '{}')} for r in store.all('SELECT * FROM calls')]
        (production.root/'logs/smash-regression.json').write_text(encode(report),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k not in {'tools','calls','answer'}},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
