import json
import time

import pytest
from fastapi.testclient import TestClient

from badminton.agent import ModelResponse
from badminton.intake import prepare,selection_command,stored_context,normalize_profile
from badminton.reporting import render
from badminton.review import validate_report
from badminton.routing import route_request
from badminton.store import encode
from tests.test_match_review import observation


def run_for(store,clip,request,question):
    return store.create_run('user1','chat1',request,question,[clip['id']])


def route(intent='video',kind='match',focus='unspecified',topics=None,player=''):
    return {'intent':intent,'video_kind':kind,'analysis_focus':focus,'techniques':topics or [],'player_description':player}


def test_ambiguous_game_asks_focus_and_explicit_menu_answer_proceeds(system,clip):
    _,store,_=system
    store.execute('UPDATE videos SET player=? WHERE id=?',('검은 상의',clip['id']))
    first=run_for(store,clip,'first','경기 영상 분석해줘')
    result=prepare(store,first,route())
    assert not result['ready'] and '경기 운영' in result['question'] and '기술·자세' in result['question']
    selected=run_for(store,clip,'choice','1')
    resolved=selection_command(store,selected)
    assert resolved['analysis_focus']=='gameplay'
    result=prepare(store,selected,resolved)
    assert result['ready'] and result['route']['intent']=='match'
    assert '경기 영상 분석해줘' in result['route']['analysis_question']
    assert stored_context(store,'user1','chat1',[clip['id']])[0]['pending'] is None


def test_explicit_report_or_technique_does_not_ask_for_the_purpose_again(system,clip):
    _,store,_=system
    report=run_for(store,clip,'report','검은 상의가 나야. 이 경기 특징과 내 레벨,장단점을 분석해줘.')
    assert prepare(store,report,route('match',focus='gameplay',player='검은 상의'))['ready']
    skill=run_for(store,clip,'skill','내 스매시 타점을 더 봐줘')
    result=prepare(store,skill,route('clip',focus='technique',topics=['스매시','타점']))
    assert result['ready'] and result['route']['analysis_profile']['techniques']==['스매시','타점']
    assert stored_context(store,'user1','chat1',[clip['id']])[0]['profile']['analysis_focus']=='gameplay'


def test_only_missing_skill_or_player_is_asked_and_previous_choice_survives(system,clip):
    _,store,_=system
    first=run_for(store,clip,'ask','경기에서 기술을 보고 싶어')
    answer=prepare(store,first,route('match',focus='technique'))
    assert not answer['ready'] and '어떤 동작' in answer['question'] and '옷 색' in answer['question']
    reply=run_for(store,clip,'reply','스매시,검은 옷이 나야')
    result=prepare(store,reply,route('match',focus='technique',topics=['스매시'],player='검은 옷'))
    assert result['ready']
    assert result['route']['analysis_profile']['video_kind']=='match'


def test_general_chat_does_not_consume_a_pending_analysis_choice(system,clip):
    _,store,_=system
    first=run_for(store,clip,'ask','이 경기 분석해줘');prepare(store,first,route())
    other=run_for(store,clip,'grip','일반적인 스매시 그립은 뭐야?')
    assert prepare(store,other,{'intent':'general'})['ready']
    assert stored_context(store,'user1','chat1',[clip['id']])[0]['pending']
    no_upload=store.create_run('user1','empty','general','이스턴 그립을 알려줘',[])
    assert selection_command(store,no_upload) is None
    assert prepare(store,no_upload,{'intent':'general'})['ready']


@pytest.mark.parametrize('kind',['lesson','rally'])
def test_training_modes_are_ready_without_gameplay_question(system,clip,kind):
    _,store,_=system
    row=run_for(store,clip,kind,'이 훈련 영상 전체를 분석해줘')
    result=prepare(store,row,route('match',kind=kind,focus='technique',player='검은 옷'))
    assert result['ready'] and result['route']['analysis_profile']['video_kind']==kind


def test_new_video_and_other_user_do_not_inherit_saved_choices(system,clip):
    _,store,_=system
    first=run_for(store,clip,'set','경기 운영을 봐줘')
    prepare(store,first,route('match',focus='gameplay',player='검은 옷'))
    with store.connect() as db:
        db.execute('INSERT INTO videos SELECT ?,user_id,chat_id,?,filename,path,?,info,NULL,created FROM videos WHERE id=?',
            ('new-video','new-upload','new-hash',clip['id']))
    another=store.create_run('user1','chat1','new','이 영상 분석해줘',['new-video'])
    assert not prepare(store,another,route(kind='unknown'))['ready']
    foreign=store.create_run('other','chat1','foreign','분석',['new-video'])
    with pytest.raises(PermissionError):prepare(store,foreign,route())


def test_reaffirming_the_same_player_preserves_cache_identity(system,clip):
    _,store,_=system
    original='상하의 검은색, 양팔 보라색 띠'
    store.execute('UPDATE videos SET player=? WHERE id=?',(original,clip['id']))
    row=run_for(store,clip,'same-player','검은 상하의가 나야. 경기 전체 리포트')
    incoming=route('match',focus='gameplay',player='검은 상하의');incoming['player_changed']=False
    assert prepare(store,row,incoming)['ready']
    assert store.video(clip['id'],'user1','chat1')['player']==original


def test_saved_report_context_is_for_the_current_video_and_requires_a_report(system,clip):
    from badminton.review_store import Reviews
    settings,store,_=system
    row=run_for(store,clip,'report-context','경기 전체 분석')
    review=Reviews(settings,store).ensure(row,clip)
    assert not stored_context(store,'user1','chat1',[clip['id']])[0]['has_saved_review']
    store.execute('UPDATE reviews SET report=? WHERE id=?',('저장된 리포트',review['id']))
    assert stored_context(store,'user1','chat1',[clip['id']])[0]['has_saved_review']
    with store.connect() as db:
        db.execute('INSERT INTO videos SELECT ?,user_id,chat_id,?,filename,path,?,info,NULL,created FROM videos WHERE id=?',
                   ('second-video','second-upload','second-hash',clip['id']))
    assert not stored_context(store,'user1','chat1',['second-video'])[0]['has_saved_review']


def test_explicit_gameplay_drops_stale_training_kind_and_techniques():
    assert normalize_profile({'video_kind':'rally','analysis_focus':'gameplay','techniques':['드라이브']})=={
        'video_kind':'match','analysis_focus':'gameplay','techniques':[]}


def test_service_waits_for_goal_then_starts_once_and_general_chat_does_not_reset_it(system,clip,monkeypatch):
    from badminton import service,routing
    from badminton.review import MatchReviewer
    settings,store,_=system
    store.execute('UPDATE videos SET player=? WHERE id=?',('검은 상의',clip['id']))
    analyses=[]
    def classify(settings,store,row):
        if row['question']=='1':result=selection_command(store,row)
        elif row['question']=='일반 그립 설명':result={'intent':'general','player_description':''}
        else:result=route()
        store.execute('UPDATE runs SET route=? WHERE id=?',(encode(result),row['id']))
        return result
    def analyze(self):
        analyses.append(self.analysis_profile)
        self.update(status='completed',phase='done',report='목적에 맞는 경기 리포트')
        return '목적에 맞는 경기 리포트'
    monkeypatch.setattr(routing,'route_request',classify)
    monkeypatch.setattr(MatchReviewer,'execute',analyze)
    monkeypatch.setattr(service,'native_run',lambda *args:'영상 없이 일반 답변')
    def submit(client,request,question):
        row=client.post('/internal/runs',json={'user_id':'user1','chat_id':'chat1','video_ids':[clip['id']],
            'request_id':request,'question':question}).json()
        for _ in range(100):
            result=store.run(row['id'])
            if result['status'] in {'completed','failed'}:return result
            time.sleep(.03)
        pytest.fail('Fixture worker did not finish')
    with TestClient(service.create_app(settings)) as client:
        client.headers['Authorization']='Bearer '+settings.token
        answer=submit(client,'ask','경기 영상 분석')
        assert answer['response_kind']=='clarification' and not analyses and not store.all('SELECT id FROM reviews')
        assert submit(client,'general','일반 그립 설명')['answer']=='영상 없이 일반 답변'
        assert stored_context(store,'user1','chat1',[clip['id']])[0]['pending']
        selected=submit(client,'select','1')
        assert selected['answer']=='목적에 맞는 경기 리포트' and len(analyses)==1
        assert analyses[0]['analysis_focus']=='gameplay'


def test_level_requires_independent_owned_evidence_and_reports_five_parts():
    obs=[observation(1,1,0),observation(2,401,396),observation(3,801,792)]
    raw={'analysis_profile':{'video_kind':'match','analysis_focus':'gameplay','techniques':[]},
         'overview':{'summary':'공격 뒤 위치를 재정비하는 흐름이다.','evidence_ids':['o1','o2']},
         'level_assessment':{'lower_tier':'developing','upper_tier':'intermediate','confidence':'high',
             'summary':'기본 연결이 보이고 압박에서 안정성을 점검할 단계다.','evidence_ids':['o1','o2','o3'],
             'criteria':[{'name':'이동·복귀','assessment':'기본 연결 확인','evidence_ids':['o1','o2']},
                         {'name':'준비','assessment':'자세 재정비 확인','evidence_ids':['o2','o3']},
                         {'name':'샷 연결','assessment':'반복 수행 확인','evidence_ids':['o1','o3']}],'limitations':'영상에 드러난 수행 기준이다.'}}
    report=validate_report(raw,obs,900)
    assert report['level_assessment']['status']=='estimated'
    assert report['level_assessment']['confidence']=='low'  # Three scenes do not establish half a match.
    status={'sampling_coverage':1,'completed_units':50,'total_units':50,'missing_ranges':[],
            'spent_usd':1,'reserved_usd':0,'cap_usd':3.5}
    answer=render({'filename':'game.mp4'},status,report,obs)
    assert all('### '+str(n)+'.' in answer for n in range(1,6))
    assert '초중급~중급' in answer and '공식 등급' in answer
    raw['level_assessment']['evidence_ids']=['other-user-evidence']
    assert validate_report(raw,obs,900)['level_assessment']['status']=='limited'
    raw['level_assessment']['evidence_ids']=['o1']
    for c in raw['level_assessment']['criteria']:c['evidence_ids']=['o1']
    assert validate_report(raw,obs,900)['level_assessment']['status']=='limited'


@pytest.mark.parametrize('kind,label',[('lesson','개인 강습 분석 리포트'),('rally','난타·반복 훈련 리포트')])
def test_non_game_report_does_not_show_game_rank_or_tactics(kind,label):
    report={'analysis_profile':{'video_kind':kind,'analysis_focus':'technique','techniques':['드라이브']},
            'level_assessment':{'status':'estimated','lower_tier':'beginner','upper_tier':'intermediate',
                'confidence':'low','summary':'반복 동작의 준비를 점검한다.','criteria':[],'limitations':''}}
    status={'sampling_coverage':1,'completed_units':1,'total_units':1,'missing_ranges':[],
            'spent_usd':.1,'reserved_usd':0,'cap_usd':3.5}
    answer=render({'filename':'training.mp4'},status,report,[])
    assert label in answer and '초급~중급' not in answer and '대회 급수' not in answer
