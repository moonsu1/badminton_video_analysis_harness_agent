"""GPT-6 Luna functional test, isolated data, one 72s actual-video excerpt.

No baseline comparison or full-match replay. Total model API test bound: $0.35.
"""
import argparse
import json
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from badminton.agent import Provider,native_run
from badminton.config import Settings,load_settings
from badminton.intake import prepare
from badminton.pricing import DEFAULT_MODEL,reservation_cost
from badminton.review import MatchReviewer
from badminton.review_store import Reviews
from badminton.routing import route_request
from badminton.store import Store,encode
from badminton.video import VideoService,ffmpeg_binary


class TestBudgetProvider(Provider):
    def create(self,purpose,**kwargs):
        tokens=self.count_input(**kwargs)
        bound=reservation_cost(tokens,kwargs.get('max_output_tokens',4000),self.settings.model)
        own=kwargs.get('_call_id','')
        used=self.store.one("SELECT COALESCE(SUM(actual_micros),0)+COALESCE(SUM(CASE WHEN status IN ('reserved','running','unknown') THEN reserved_micros ELSE 0 END),0) n FROM calls WHERE id<>?",(own,))['n']
        if used+bound>350000:raise RuntimeError('Luna migration test reached its $0.35 total bound; no call sent.')
        return super().create(purpose,**kwargs)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--resume-data');args=parser.parse_args()
    production=load_settings();root=Path(args.resume_data).resolve() if args.resume_data else Path(tempfile.mkdtemp(prefix='badminton-luna-migration-'))
    settings=Settings(root,model=DEFAULT_MODEL,max_tools=6,review_max_usd=.30,
        review_summary_reserve=.05,review_safety_reserve=.01,review_coaching_reserve=.02).initialize()
    store=Store(root/'agent.db');videos=VideoService(settings,store)
    report={'model':settings.model,'data':str(root),'budget_usd':.35,'comparison_run':False,
        'full_original_replayed':False,'passed':False,'steps':[]}
    if args.resume_data:
        previous=json.loads((production.root/'logs/luna-migration-smoke.json').read_text(encoding='utf-8'))
        assert previous['data']==str(root) and previous['model']==DEFAULT_MODEL
        report=previous;report['passed']=False;report['resumed_without_replaying_video']=True
    prior_seconds=report.get('elapsed_seconds',0)
    start=time.monotonic()
    try:
        def task(question,ids):
            row=store.create_run('migration-test','luna-chat',uuid.uuid4().hex,question,ids)
            provider=TestBudgetProvider(settings,store,row['id'])
            route=route_request(settings,store,row,provider)
            decision=prepare(store,row,route)
            return store.run(row['id']),provider,route,decision
        if args.resume_data:
            review=store.one('SELECT * FROM reviews ORDER BY created DESC LIMIT 1')
            assert review and review['status'] in {'completed','partial'}
            video=store.video(review['video_id'],'migration-test','luna-chat');answer=review['report']
        else:
            review,video,answer=initial_review(production,settings,store,videos,task,report)
        saved=Reviews(settings,store).status(review['id'])
        report['review']=saved
        assert saved['sampling_coverage']==1 and saved['status'] in {'completed','partial'}
        assert saved['report_json']['report_version']=='five-part-v1'
        level=saved['report_json']['level_assessment']['status']
        if saved['status']=='partial':
            assert saved['report_json']['report_status']=='assessment_pending' and level=='limited'
        report['level_estimate_verified']=level=='estimated'
        assert saved['report_json']['scene_coaching']
        assert all('### '+str(n)+'.' in answer for n in range(1,6))
        report['steps'].append({'step':'actual_video_five_part_report','coverage':saved['sampling_coverage'],
            'units':saved['completed_units'],'scene_coaching':len(saved['report_json']['scene_coaching'])})
        print('actual_video_five_part_report passed',flush=True)
        before=store.one('SELECT COUNT(*) n FROM calls WHERE purpose IN (\'precision\',\'overview\')')['n']
        row,provider,route,decision=task('방금 리포트에서 내 수준을 그렇게 본 이유를 설명해줘.',[video['id']])
        assert decision['ready'] and route['intent']=='explanation'
        answer=native_run(settings,store,videos,row,provider)
        store.update_run(row['id'],'completed',answer=answer)
        assert store.one('SELECT COUNT(*) n FROM calls WHERE purpose IN (\'precision\',\'overview\')')['n']==before
        report['steps'].append({'step':'followup_saved_report','answer':answer,'new_video_calls':0})
        row,provider,route,decision=task('이 영상 9초부터 9.4초까지 스매시 준비 자세와 발 움직임을 원본 연속 프레임으로 확인해줘. 실제로 보이는 움직임만 설명해줘.',[video['id']])
        assert route['intent']=='clip' and decision['ready']
        answer=native_run(settings,store,videos,row,provider)
        store.update_run(row['id'],'completed',answer=answer)
        evidence=store.cached('evidence:'+row['id']) or []
        assert any(e['native'] and e['sequence']['native_frames_analyzed']>0 for e in evidence)
        report['steps'].append({'step':'harness_native_video_tool','answer':answer,
            'native_frames':sum(e['sequence']['native_frames_analyzed'] for e in evidence if e['native'])})
        report['passed']=True
    finally:
        report['elapsed_seconds']=round(prior_seconds+time.monotonic()-start,2)
        report['calls']=store.all('SELECT purpose,status,model,response_model,service_tier,usage,actual_micros,reserved_micros FROM calls ORDER BY created')
        report['cost_usd']=sum(r['actual_micros'] or 0 for r in report['calls'])/1e6
        report['reserved_usd']=sum(r['reserved_micros'] or 0 for r in report['calls'] if r['status'] in {'reserved','running','unknown'})/1e6
        if report['calls']:assert all(r['model']==r['response_model']==DEFAULT_MODEL and r['status']=='completed' for r in report['calls'])
        (production.root/'logs/luna-migration-smoke.json').write_text(encode(report),encoding='utf-8')
        print(encode({k:v for k,v in report.items() if k not in {'steps','review','calls'}}),flush=True)


def initial_review(production,settings,store,videos,task,report):
    with sqlite3.connect('file:'+str(production.root/'agent.db')+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        source=dict(db.execute('SELECT * FROM videos WHERE id=?',('127ce529e4ba4788b419a1cfc78b71a9',)).fetchone())
    excerpt=settings.webui/'uploads/luna-test-72s.mp4'
    subprocess.run([ffmpeg_binary(),'-v','error','-ss','252','-i',source['path'],'-t','72',
        '-an','-vf','setpts=PTS-STARTPTS','-c:v','libx264','-preset','ultrafast','-crf','22','-y',str(excerpt)],
        check=True,timeout=180)
    video=videos.register(str(excerpt),'luna-72s',excerpt.name,'migration-test','luna-chat')
    store.execute('UPDATE videos SET player=? WHERE id=?',(source['player'],video['id']))
    report['excerpt']={'source_file':source['filename'],'source_start_seconds':252,'duration':video['info']['duration']}
    row,provider,route,decision=task('일반적으로 스매시 스윙할 때 이스턴 그립과 주먹그립을 어떻게 구분해야 해?',[])
    assert route['intent']=='general' and decision['ready']
    answer=native_run(settings,store,videos,row,provider)
    store.update_run(row['id'],'completed',answer=answer)
    assert store.run(row['id'])['response_kind']=='general'
    report['steps'].append({'step':'general_no_video','answer':answer})
    print('general_no_video passed',flush=True)
    row,provider,route,decision=task('이 경기 영상 분석해줘',[video['id']])
    assert not decision['ready'] and '경기 운영' in decision['question']
    store.update_run(row['id'],'completed',answer=decision['question'])
    report['steps'].append({'step':'purpose_question','answer':decision['question']})
    row,provider,route,decision=task('1',[video['id']])
    assert decision['ready'] and route['intent']=='match'
    review=Reviews(settings,store).ensure(row,store.video(video['id'],'migration-test','luna-chat'))
    store.update_run(row['id'],'analyzing')
    answer=MatchReviewer(settings,store,videos,store.run(row['id']),review['id'],provider).execute()
    store.update_run(row['id'],'completed',answer=answer)
    return review,video,answer


if __name__=='__main__':main()
