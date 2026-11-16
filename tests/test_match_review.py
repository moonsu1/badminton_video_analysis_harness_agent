import concurrent.futures
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from badminton.agent import ModelResponse
from badminton.review import MatchReviewer,extract_match_frames,normalize_scan,validate_report
from badminton.review_store import BudgetExceeded,Reviews,fair_order,usage_cost,windows
from badminton.routing import route_request
from badminton.service import create_app
from badminton.store import encode


def setup_review(system,clip,cap=None):
    settings,store,videos=system
    if cap is not None:settings.review_max_usd=cap
    store.execute('UPDATE videos SET player=? WHERE id=?',('시험 선수',clip['id']))
    video=store.video(clip['id'],'user1','chat1')
    run=store.create_run('user1','chat1','review','내 플레이 전체 분석해줘',[clip['id']])
    reviews=Reviews(settings,store);review=reviews.ensure(run,video)
    return reviews,review,store.run(run['id']),video


def test_match_sampling_is_complete_original_pts_and_cache(system,clip):
    settings,store,videos=system
    frames=extract_match_frames(settings,clip)
    assert len(frames)==6
    assert [f['time'] for f in frames]==[0,.5,1,1.5,2,2.5]
    assert all(f['path'].endswith('.jpg') and f['crop'] is None for f in frames)
    again=extract_match_frames(settings,{**clip,'filename':'new.mp4','id':'alias'})
    assert frames[0]['path']==again[0]['path'] and again[0]['video_id']=='alias'


def test_windows_cover_18_minutes_and_fair_order():
    units=[{'start':a,'end':b} for a,b in windows(1132.2064)]
    assert len(units)==63 and units[-1]['end']==1132.2064
    ordered=fair_order(units,1132.2064)
    assert [int((v['start']+v['end'])/2/1132.2064*3) for v in ordered[:3]]==[0,1,2]
    assert all(b['start']<=a['end'] for a,b in zip(units,units[1:]))


@pytest.mark.parametrize('model,cap',[('gpt-6-luna',.345),('gpt-5.4-mini',.43)])
def test_budget_reservations_are_atomic_and_keep_unknown(system,clip,model,cap):
    settings,store,_=system
    settings.model=model
    reviews,review,run,_=setup_review(system,clip,cap=cap)
    def reserve():
        try:return reviews.reserve(review['id'],run['id'],'scan',100000,1000)
        except BudgetExceeded:return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:ids=list(pool.map(lambda _:reserve(),range(4)))
    assert sum(v is not None for v in ids)==1  # Safety, synthesis and scene-coaching funds remain.
    call_id=next(v for v in ids if v)
    store.execute("UPDATE calls SET status='unknown' WHERE id=?",(call_id,))
    reviews.settle(call_id)
    assert reviews.money(review['id'])[1]>0
    with pytest.raises(BudgetExceeded):reserve_again=reviews.reserve(review['id'],run['id'],'scan',100000,1000)


def test_actual_usage_applies_cache_discount_and_releases_reservation(system,clip):
    _,store,_=system
    reviews,review,run,_=setup_review(system,clip)
    cid=reviews.reserve(review['id'],run['id'],'scan',10000,1000)
    usage={'input_tokens':10000,'output_tokens':100,'input_tokens_details':{'cached_tokens':5000}}
    store.execute('UPDATE calls SET status=?,usage=? WHERE id=?',('completed',encode(usage),cid))
    reviews.settle(cid)
    assert reviews.money(review['id'])==(usage_cost(usage),0)


def observation(index,start,unit_start,visible=True):
    return {'id':f'o{index}','start':start,'end':start+1,'focus':start+.5,'unit_start':unit_start,'unit_end':unit_start+20,'unit_id':f'u{unit_start}',
        'player_visible':visible,'category':'recovery','kind':'issue','observation':'타구 후 중앙으로 이동하는 첫 발이 늦게 보임','interpretation':'다음 대응 공간이 줄 수 있음','limitation':''}


def test_one_scene_and_uncertain_player_cannot_become_global_claim():
    observations=[observation(1,1,0),observation(2,3,0),observation(3,40,36,False),observation(4,80,72)]
    claim=lambda ids:{'text':'반복되는 복귀 동작','evidence_ids':ids,'counterexample_ids':[],'drill':'타구 뒤 첫 발을 연습'}
    draft={'tendencies':[claim(['o1','o2']),claim(['o1','o3']),claim(['o1','o4'])]}
    result=validate_report(draft,observations)
    assert len(result['tendencies'])==1 and len(result['rejected_claims'])==2


class FakeReviewProvider:
    def __init__(self):self.calls=[];self.started=None;self.release=None
    def count_input(self,**kwargs):return 1000
    def create(self,purpose,**kwargs):
        self.calls.append(purpose)
        if self.started:
            self.started.set();self.release.wait(5)
        schema=kwargs['text']['format']['schema']
        if purpose=='review_scan':
            ids=schema['properties']['observations']['items']['properties']['start_frame_id']['enum']
            result={'observations':[{'start_frame_id':ids[0],'end_frame_id':ids[-1],'focus_frame_id':ids[0],
                'player_visible':True,'category':'footwork','rally_context':'play','kind':'neutral','observation':'시험 선수 이동','interpretation':'','limitation':'합성 영상 시험',
                'needs_refinement':False,'box':[0,0,1,1]}],'limitations':[]}
        elif purpose=='review_synthesis':
            result={'tendencies':[],'strengths':[],'improvements':[],'scene_ids':[],'limitations':['짧은 합성 영상']}
        elif purpose=='review_coaching':
            if 'actions' in schema['properties']:
                points=json.loads(kwargs['input'][1]['content'])['points']
                result={'actions':[{k:p[k] for k in ('id','action','drill','success_check')} for p in points]}
                return ModelResponse({'output':[{'type':'message','content':[{'type':'output_text','text':encode(result)}]}],
                    'usage':{'input_tokens':1000,'output_tokens':100}})
            ids=schema['properties']['phases']['items']['properties']['frame_ids']['items']['enum']
            result={'target_verified':True,'activity':'play_or_practice','primary_action_frame_id':ids[0],'next_action_frame_id':'none','phases':[
                {'phase':'preparation','frame_ids':[ids[0]],'observation':'시험 준비 자세'},
                {'phase':'recovery','frame_ids':[ids[-1]],'observation':'시험 복귀 자세'}],
                'points':[{'topic':'recovery','assessment':'training_experiment','basis_frame_ids':[ids[0],ids[-1]],
                    'observation':'시험 영상에서 발 위치가 바뀐다.','possible_effect':'다음 준비 동작을 연결하는 데 도움될 수 있다.',
                    'action':'다음 준비 자세로 이어가 보세요.','drill':'복귀 후 준비 자세를5회 연습하세요.',
                    'success_check':'복귀 뒤 준비 자세가 이어지는지 확인하세요.','uncertainty':'합성 시험이다.'}],
                'unreadable_fields':[]}
        elif purpose=='review_verification':
            ids=schema['properties']['checks']['items']['properties']['id']['enum']
            result={'checks':[{'id':i,'verdict':'supported','reason':'시험 선수 확인'} for i in ids]}
        else:raise AssertionError(purpose)
        return ModelResponse({'output':[{'type':'message','content':[{'type':'output_text','text':encode(result)}]}],
                              'usage':{'input_tokens':1000,'output_tokens':100}})


def test_review_completes_caches_and_does_not_claim_one_scene_is_style(system,clip):
    settings,store,videos=system
    reviews,review,run,_=setup_review(system,clip)
    fake=FakeReviewProvider()
    answer=MatchReviewer(settings,store,videos,run,review['id'],fake).execute()
    status=reviews.status(review['id'],'user1','chat1')
    assert status['sampling_coverage']==1 and status['missing_ranges']==[]
    assert '이번 경기에서 관찰된 경향' not in answer and '다음 준비 자세로 이어가' in answer
    assert fake.calls==['review_scan','review_coaching','review_coaching','review_synthesis','review_verification']
    another=store.create_run('user1','chat1','repeat','경기 전체 분석해줘',[clip['id']])
    cached=reviews.ensure(another,store.video(clip['id'],'user1','chat1'))
    assert cached['id']==review['id'] and cached['status']=='completed'


def test_insufficient_budget_returns_partial_and_records_missing_ranges(system,clip):
    settings,store,videos=system
    reviews,review,run,_=setup_review(system,clip,cap=.255)
    answer=MatchReviewer(settings,store,videos,run,review['id'],FakeReviewProvider()).execute()
    status=reviews.status(review['id'])
    assert status['sampling_coverage']==0 and status['status']=='paused_budget'
    assert '부분 경기 리뷰' in answer and '전체 시간대 판독 완료' not in answer


def test_resume_budget_is_idempotent_scoped_and_preserves_completed_work(system,clip):
    settings,store,_=system
    reviews,review,run,_=setup_review(system,clip)
    store.execute("UPDATE reviews SET status='paused_budget' WHERE id=?",(review['id'],))
    with pytest.raises(PermissionError):reviews.resume(review['id'],'other','chat1',1,'approval')
    first=reviews.resume(review['id'],'user1','chat1',1,'approval')
    second=reviews.resume(review['id'],'user1','chat1',1,'approval')
    assert first['cap_micros']==second['cap_micros']==4500000
    assert first['run_id']==second['run_id']
    assert json.loads(store.run(first['run_id'])['route'])['intent']=='match'


def test_review_scope_api_rejects_foreign_access(system,clip):
    settings,store,_=system
    reviews,review,run,_=setup_review(system,clip)
    with TestClient(create_app(settings,runner=lambda *a:'mock')) as client:
        assert client.get('/internal/reviews/'+review['id'],params={'user_id':'user1','chat_id':'chat1'}).status_code==401
        client.headers['Authorization']='Bearer '+settings.token
        assert client.get('/internal/reviews/'+review['id'],params={'user_id':'other','chat_id':'chat1'}).status_code==404
        assert client.get('/internal/reviews/'+review['id'],params={'user_id':'user1','chat_id':'chat1'}).status_code==200


def test_router_uses_semantic_scope_not_previous_general_topic(system,clip):
    settings,store,_=system
    run=store.create_run('user1','chat1','route','한장면 말고 경기 전체 장단점을 알려줘',[clip['id']])
    class RouteProvider:
        def count_input(self,**kwargs):return 100
        def create(self,*a,**kwargs):
            return ModelResponse({'output':[{'type':'message','content':[{'type':'output_text','text':encode({'intent':'match','player_description':''})}]}],
                                  'usage':{'input_tokens':100,'output_tokens':20}})
    assert route_request(settings,store,run,RouteProvider())['intent']=='match'
    assert json.loads(store.run(run['id'])['route'])['intent']=='match'


def test_general_chat_does_not_wait_for_running_match_review(system,clip,monkeypatch):
    from badminton import service,routing
    settings,store,videos=system
    store.execute('UPDATE videos SET player=? WHERE id=?',('시험 선수',clip['id']))
    started,release=threading.Event(),threading.Event()
    def fake_review(self):
        self.update(status='running');started.set();release.wait(8)
        self.update(status='completed',report='저장된 전체 리뷰')
        return '저장된 전체 리뷰'
    def fake_route(settings,store,row):
        route={'intent':'match' if row.get('scope')=='match' else 'general','player_description':''}
        store.execute('UPDATE runs SET route=? WHERE id=?',(encode(route),row['id']))
        return route
    monkeypatch.setattr(MatchReviewer,'execute',fake_review)
    monkeypatch.setattr(routing,'route_request',fake_route)
    monkeypatch.setattr(service,'native_run',lambda *a:'영상 없이 바로 답했습니다.')
    with TestClient(service.create_app(settings)) as client:
        client.headers['Authorization']='Bearer '+settings.token
        identity={'user_id':'user1','chat_id':'chat1','video_ids':[clip['id']]}
        heavy=client.post('/internal/runs',json={**identity,'request_id':'heavy','scope':'match','question':'내 플레이 전체 분석'}).json()
        assert started.wait(3)
        light=client.post('/internal/runs',json={**identity,'request_id':'light','question':'일반 그립 설명'}).json()
        for _ in range(60):
            row=store.run(light['id'])
            if row['status']=='completed':break
            time.sleep(.03)
        assert row['answer']=='영상 없이 바로 답했습니다.'
        assert store.run(heavy['id'])['status']=='analyzing'
        release.set()


def test_restart_preserves_completed_units_and_uncertain_money(system,clip):
    settings,store,_=system
    reviews,review,run,_=setup_review(system,clip)
    unit=store.one('SELECT id FROM review_units WHERE review_id=?',(review['id'],))
    store.execute("UPDATE review_units SET status='completed',result=? WHERE id=?",(encode({'observations':[]}),unit['id']))
    store.execute("UPDATE reviews SET status='running' WHERE id=?",(review['id'],))
    cid=reviews.reserve(review['id'],run['id'],'scan',10000,1000)
    store.execute("UPDATE calls SET status='running' WHERE id=?",(cid,))
    store.recover()
    assert reviews.get(review['id'])['status']=='paused_interrupted'
    assert store.one('SELECT status FROM review_units WHERE id=?',(unit['id'],))['status']=='completed'
    assert store.one('SELECT status FROM calls WHERE id=?',(cid,))['status']=='unknown'
    assert reviews.money(review['id'])[1]>0


def test_decode_gap_is_not_counted_as_complete_window(system,clip):
    settings,store,videos=system
    reviews,review,run,_=setup_review(system,clip)
    frames=extract_match_frames(settings,clip)
    fake=FakeReviewProvider()
    reviewer=MatchReviewer(settings,store,videos,run,review['id'],fake)
    unit=store.one('SELECT * FROM review_units WHERE review_id=?',(review['id'],))
    with pytest.raises(ValueError,match='디코딩 공백'):
        reviewer.scan(unit,[frames[0],frames[-1]])
    assert fake.calls==[]
    assert reviews.status(review['id'])['sampling_coverage']==0


def test_unknown_synthesis_is_not_replayed_on_resume(system,clip):
    from badminton.review import BudgetProvider
    settings,store,_=system
    reviews,review,run,_=setup_review(system,clip)
    cid=reviews.reserve(review['id'],run['id'],'synthesis',1000,1000)
    store.execute("UPDATE calls SET status='unknown' WHERE id=?",(cid,))
    fake=FakeReviewProvider()
    with pytest.raises(RuntimeError,match='자동으로 다시 보내지 않았습니다'):
        BudgetProvider(settings,store,run['id'],review['id'],fake).create('synthesis',max_output_tokens=1000,input=[])
    assert not fake.calls and reviews.money(review['id'])[1]>0


def test_multiphase_video_covers_early_middle_late_without_duplicate_ids(system,tmp_path):
    import subprocess
    from PIL import Image
    from badminton.video import ffmpeg_binary,probe
    settings,store,_=system
    path=tmp_path/'phases.mp4'
    subprocess.run([ffmpeg_binary(),'-v','error','-f','lavfi','-i','color=red:s=96x64:r=4:d=20',
        '-f','lavfi','-i','color=green:s=96x64:r=4:d=20','-f','lavfi','-i','color=blue:s=96x64:r=4:d=20',
        '-filter_complex','[0:v][1:v][2:v]concat=n=3:v=1:a=0','-c:v','libx264','-y',str(path)],check=True)
    video={'id':'phases','filename':path.name,'sha256':'three-colors','path':str(path),'info':probe(path)}
    frames=extract_match_frames(settings,video)
    assert len(frames)==120 and len({f['frame_id'] for f in frames})==120
    assert max(abs(f['time']-index*.5) for index,f in enumerate(frames))<.01
    for index,channel in [(10,0),(50,1),(100,2)]:
        with Image.open(frames[index]['path']) as image:
            rgb=image.getpixel((40,40))
        assert rgb[channel]==max(rgb) and rgb[channel]>100


def test_wrong_player_scene_is_excluded_and_verification_is_reused(system,clip):
    settings,store,videos=system
    reviews,review,run,_=setup_review(system,clip)
    frames=extract_match_frames(settings,clip)
    o=observation(1,0,0);o['focus']=.5
    draft=validate_report({'scene_ids':[o['id']]},[o])
    class IdentityProvider(FakeReviewProvider):
        def create(self,purpose,**kwargs):
            self.calls.append(purpose)
            return ModelResponse({'output':[{'type':'message','content':[{'type':'output_text','text':encode({
                'checks':[{'id':o['id'],'verdict':'wrong_player','reason':'다른 선수의 행동'}]})}]}],
                'usage':{'input_tokens':1000,'output_tokens':100}})
    provider=IdentityProvider()
    reviewer=MatchReviewer(settings,store,videos,run,review['id'],provider)
    result=reviewer.verify_report(draft,[o],frames)
    assert result['scene_ids']==[] and result['identity_checks']['accepted']==0
    before=reviews.money(review['id'])
    assert reviewer.verify_report(draft,[o],frames)['scene_ids']==[]
    assert provider.calls==['review_verification'] and reviews.money(review['id'])==before


def test_blind_event_check_rejects_opponents_fall_even_when_draft_critic_agrees(system,clip):
    settings,store,videos=system
    reviews,review,run,_=setup_review(system,clip)
    frames=extract_match_frames(settings,clip)
    o=observation(1,0,0);o['focus']=.5;o['observation']='지정 선수가 균형을 잃고 바닥 쪽으로 내려갔다.'
    draft=validate_report({'scene_ids':[o['id']]},[o])
    class EventProvider(FakeReviewProvider):
        def create(self,purpose,**kwargs):
            self.calls.append(purpose)
            if kwargs['text']['format']['name']=='blind_player_event':
                result={'target_fallen':False,'other_fallen':True,'target_helping_other':True,'description':'상대가 넘어졌고 대상 선수는 서서 도왔다.'}
            else:result={'checks':[{'id':o['id'],'verdict':'supported','reason':'초안에 동의'}]}
            return ModelResponse({'output':[{'type':'message','content':[{'type':'output_text','text':encode(result)}]}],
                'usage':{'input_tokens':1000,'output_tokens':100}})
    provider=EventProvider()
    result=MatchReviewer(settings,store,videos,run,review['id'],provider).verify_report(draft,[o],frames)
    assert result['scene_ids']==[]
    assert result['identity_checks']['rejected'][0]['verdict']=='wrong_player'
    assert len(provider.calls)==2


def test_between_points_actions_cannot_support_play_habits():
    pickup=observation(1,1,0);pickup['observation']='셔틀을 집어 든 뒤 원위치 쪽으로 걸었다.'
    playing=observation(2,50,36)
    between=observation(3,90,72);between['rally_context']='break'
    draft={'strengths':[{'text':'복귀가 좋다','evidence_ids':['o1','o2'],'counterexample_ids':[],'drill':''},
                        {'text':'대응이 빠르다','evidence_ids':['o2','o3'],'counterexample_ids':[],'drill':''}]}
    result=validate_report(draft,[pickup,playing,between])
    assert result['strengths']==[] and result['scene_ids']==['o2']


def test_camera_uncertainty_cannot_become_a_players_fault():
    a,b=observation(1,1,0),observation(2,50,36)
    for o in (a,b):o['kind']='neutral';o['observation']='타격 순간이 흐려 보이지 않는다.'
    claim={'text':'임팩트 전 준비를 고쳐야 한다','evidence_ids':['o1','o2'],'counterexample_ids':[],'drill':'스윙 연습'}
    assert validate_report({'improvements':[claim]},[a,b])['improvements']==[]
    for o in (a,b):o['kind']='issue';o['observation']='타구 후 정지하여 다음 공에 한 발 늦게 움직였다.'
    assert len(validate_report({'improvements':[claim]},[a,b])['improvements'])==1
