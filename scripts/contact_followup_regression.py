"""Replay the failed contact-point follow-up, including its real preceding chat context."""
import json
import sqlite3
import tempfile
import time
from pathlib import Path

from badminton.agent import ToolSession,native_run
from badminton.config import Settings,load_settings
from badminton.store import Store,encode
from badminton.video import VideoService


def main():
    production=load_settings()
    settings=Settings(Path(tempfile.mkdtemp(prefix='badminton-contact-')),model=production.model).initialize()
    store=Store(settings.root/'agent.db')
    source_id='352ef70d36ef404d917168985d21fbd6'
    with sqlite3.connect('file:'+str(production.root/'agent.db')+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        original=dict(db.execute('SELECT * FROM runs WHERE id=?',(source_id,)).fetchone())
        ids=json.loads(original['video_ids'])
        for video_id in ids:
            v=dict(db.execute('SELECT * FROM videos WHERE id=?',(video_id,)).fetchone())
            store.execute('INSERT INTO videos(id,user_id,chat_id,source_id,filename,path,sha256,info,player,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (v['id'],'test','contact',v['source_id'],v['filename'],v['path'],v['sha256'],v['info'],v['player'],time.time()))
            for row in db.execute("SELECT * FROM observations WHERE video_id=? AND kind IN ('precision','overview')",(video_id,)):
                store.cache(row['cache_key'],video_id,row['kind'],json.loads(row['data']))
        previous=db.execute("SELECT * FROM runs WHERE user_id=? AND chat_id=? AND status='completed' AND created<? ORDER BY created DESC LIMIT 12",
                            (original['user_id'],original['chat_id'],original['created'])).fetchall()
        for row in previous:
            store.execute('INSERT INTO runs(id,user_id,chat_id,request_id,question,video_ids,status,answer,created,updated,analysis_version,response_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (row['id'],'test','contact','history-'+row['id'],row['question'],row['video_ids'],'completed',row['answer'],row['created'],row['updated'],row['analysis_version'],row['response_kind']))
            e=db.execute('SELECT data FROM observations WHERE cache_key=?',('evidence:'+row['id'],)).fetchone()
            if e:store.cache('evidence:'+row['id'],'','evidence',json.loads(e['data']))
    run=store.create_run('test','contact','contact-followup',original['question'],ids)
    videos=VideoService(settings,store)
    policy=ToolSession(settings,store,videos,run,None).policy
    assert not policy['grip'] and policy['precise']
    report={'source_run':source_id,'isolated_data':str(settings.root),'question_unchanged':True,
            'preceding_chat_copied':len(previous),'model':settings.model,'policy':policy}
    started=time.monotonic()
    try:
        answer=native_run(settings,store,videos,run)
        store.update_run(run['id'],'completed',answer=answer)
        evidence=store.cached('evidence:'+run['id']) or []
        report['response_kind']=store.run(run['id'])['response_kind']
        report['evidence']=[{'filename':e['filename'],'video_id':e['video_id'],'reused':bool(e.get('reused')),
            'native':e['native'],'frames':len(e['frames']),'start':e['frames'][0]['time'],'end':e['frames'][-1]['time']} for e in evidence]
        assert report['response_kind']=='analysis','Expected a grounded contact-point comparison, not an unverified or partial response'
        assert {e['video_id'] for e in evidence if e['native']}==set(ids)
        assert '정밀 근거 검증을 완료하지 못했습니다' not in answer
        report.update(passed=True,answer=answer,completed_run=run['id'])
    except Exception as exc:
        report.update(passed=False,error=str(exc))
        raise
    finally:
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        report['tools']=[json.loads(e['data']) for e in store.all("SELECT data FROM events WHERE kind='tool' AND run_id=?",(run['id'],))]
        report['calls']=[{'purpose':r['purpose'],'status':r['status'],'usage':json.loads(r['usage'] or '{}')} for r in store.all('SELECT * FROM calls WHERE run_id=?',(run['id'],))]
        (production.root/'logs/contact-followup-regression.json').write_text(encode(report),encoding='utf-8')
        print(json.dumps({k:v for k,v in report.items() if k not in {'answer','tools','calls'}},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
