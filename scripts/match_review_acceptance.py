"""One actual 18m52s acceptance run, isolated service, reusable production-owned cache."""
import json
import argparse
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from badminton.config import Settings,load_settings
from badminton.store import Store,encode
from badminton.review_store import Reviews


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--resume-data')
    args=parser.parse_args()
    production=load_settings()
    root=Path(args.resume_data).resolve() if args.resume_data else Path(tempfile.mkdtemp(prefix='badminton-match-live-'))
    (root/'settings.json').write_text(encode({'port':8767,'model':production.model}),encoding='utf-8')
    settings=Settings(root,port=8767,model=production.model).initialize()
    store=Store(root/'agent.db')
    with sqlite3.connect('file:'+str(production.root/'agent.db')+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        source=dict(db.execute("SELECT * FROM runs WHERE id='f90361894a3b4fa48824ecc895123666'").fetchone())
        video_id=json.loads(source['video_ids'])[0]
        video=dict(db.execute('SELECT * FROM videos WHERE id=?',(video_id,)).fetchone())
        store.execute('INSERT OR IGNORE INTO videos(id,user_id,chat_id,source_id,filename,path,sha256,info,player,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
            tuple(video[k] for k in ['id','user_id','chat_id','source_id','filename','path','sha256','info','player','created']))
        # Preserve the relevant prior single-scene answer: the broadening request must
        # not be routed to a mere paraphrase, even with this context.
        prior=dict(db.execute("SELECT * FROM runs WHERE id='a1b33bc020a7498dbb5b5784dea6c0a5'").fetchone())
        store.execute('INSERT OR IGNORE INTO runs(id,user_id,chat_id,request_id,question,video_ids,status,answer,created,updated,analysis_version,response_kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            tuple(prior[k] for k in ['id','user_id','chat_id','request_id','question','video_ids','status','answer','created','updated','analysis_version','response_kind']))
    env={**os.environ,'BADMINTON_DATA_DIR':str(root),'PYTHONUTF8':'1'}
    report={'test':'actual_match_full_review','isolated_data':str(root),'filename':video['filename'],'budget_usd':3.5,
            'question_unchanged':True,'source_run':source['id'],'resumed_same_review':bool(args.resume_data)}
    started=time.monotonic()
    with (root/'service.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen([sys.executable,'-m','badminton.service'],cwd=Path(__file__).resolve().parent.parent,
            env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        with httpx.Client(base_url='http://127.0.0.1:8767',headers={'Authorization':'Bearer '+settings.token},timeout=20,trust_env=False) as client:
            for _ in range(30):
                try:
                    if client.get('/health').status_code==200:break
                except httpx.HTTPError:time.sleep(1)
            if args.resume_data:
                saved=client.get('/internal/reviews/latest',params={'user_id':source['user_id'],'chat_id':source['chat_id']}).json()
                response=client.post('/internal/reviews/'+saved['id']+'/resume',json={'user_id':source['user_id'],'chat_id':source['chat_id'],
                    'request_id':'acceptance-resume-preparation-v1','additional_usd':0})
            else:
                response=client.post('/internal/runs',json={'user_id':source['user_id'],'chat_id':source['chat_id'],
                    'request_id':'match-acceptance-v1','question':source['question'],'video_ids':[video_id],'scope':'auto'})
            response.raise_for_status();run_id=response.json()['id'];report['run_id']=run_id
            last=None
            for _ in range(1800):
                run=client.get('/internal/runs/'+run_id,params={'user_id':source['user_id'],'chat_id':source['chat_id']}).json()
                review=run.get('review')
                if review:
                    state=(review['phase'],review['completed_units'],review['status'])
                    if state!=last:
                        print(encode({k:review[k] for k in ['id','phase','status','completed_units','total_units','sampling_coverage','spent_usd','reserved_usd']}),flush=True);last=state
                    if review['snapshot'] and 'preview_seconds' not in report:report['preview_seconds']=round(time.monotonic()-started,2)
                if run['status'] in {'completed','failed','cancelled'}:
                    report.update(run_status=run['status'],review=review,answer=run.get('answer'),error=run.get('error'))
                    break
                time.sleep(2)
            else:raise RuntimeError('Acceptance run exceeded 60 minutes')
            assert report['run_status']=='completed',report.get('error')
            assert review and review['status']=='completed' and review['sampling_coverage']==1
            assert review['spent_usd']+review['reserved_usd']<=3.5
            assert len(review['report_json']['scene_ids'])>=6
            assert review['report_json']['strengths'] or review['report_json']['improvements']
            report['passed']=True
    finally:
        import psutil
        try:
            parent=psutil.Process(process.pid);children=parent.children(recursive=True)
            for child in reversed(children):child.terminate()
            parent.terminate();psutil.wait_procs(children+[parent],timeout=5)
        except psutil.NoSuchProcess:pass
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        (production.root/'logs/match-review-acceptance.json').write_text(encode(report),encoding='utf-8')
        print(encode({k:v for k,v in report.items() if k not in {'answer','review'}}),flush=True)


if __name__=='__main__':main()
