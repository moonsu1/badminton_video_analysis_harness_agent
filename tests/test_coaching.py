import json

import pytest

from badminton.coaching import context_frames,normalize,saved_points
from badminton.review import MatchReviewer,render_report
from badminton.review_store import BudgetExceeded
from badminton.store import encode
from tests.test_match_review import FakeReviewProvider,setup_review


def scene(assessment='observed_issue'):
    return {'target_verified':True,'activity':'play_or_practice',
        'phases':[{'phase':'preparation','frame_ids':['F0'],'observation':'라켓이 몸 뒤에 있다.'},
                  {'phase':'recovery','frame_ids':['F1'],'observation':'발을 다시 모은다.'}],
        'points':[{'topic':'recovery','assessment':assessment,'basis_frame_ids':['F0','F1'],
            'consequence_observed':False,'consequence_frame_ids':[],
            'observation':'큰 스윙 뒤 라켓이 몸 뒤에 남아 있다.','possible_effect':'다음 공에 라켓을 다시 가져오는 동작이 필요할 수 있다.',
            'action':'스윙 뒤 라켓을 몸 앞으로 돌려놓아 보세요.','drill':'스윙과 라켓 회수를 묶어8회씩2세트.',
            'success_check':'다음 준비 동작 전에 라켓이 몸 앞으로 돌아오는지 확인.','uncertainty':'타격 순간은 불명확하다.'}],
        'unreadable_fields':['정확한 타점']}


def frames():
    return [{'frame_id':'F0','time':1.25},{'frame_id':'F1','time':1.75}]


def test_single_scene_advice_does_not_require_two_issue_labels():
    result=normalize(scene(),frames(),'neutral-source','coach')
    assert len(result['points'])==1
    point=result['points'][0]
    assert point['start']==1.25 and point['end']==1.75 and point['scope']=='scene'
    assert point['assessment']=='training_experiment'
    assert point['action'] and point['drill'] and point['success_check']


def test_a_visible_consequence_can_support_a_scene_correction():
    data=scene()
    data['points'][0].update(consequence_observed=True,consequence_frame_ids=['F1'])
    assert normalize(data,frames(),'source','coach')['points'][0]['assessment']=='observed_issue'


@pytest.mark.parametrize('field,value',[('target_verified',False),('activity','rest_or_collection')])
def test_wrong_player_or_only_collection_does_not_create_advice(field,value):
    data=scene();data[field]=value
    assert normalize(data,frames(),'source','coach')['points']==[]


def test_uncertain_rally_context_still_allows_a_grounded_training_experiment():
    data=scene('training_experiment');data['activity']='uncertain'
    assert normalize(data,frames(),'source','coach')['points']
    data['points'][0]['assessment']='observed_issue'
    assert normalize(data,frames(),'source','coach')['points']==[]


def test_made_up_frame_and_empty_exercises_do_not_pass():
    data=scene();data['points'][0]['basis_frame_ids']=['F0','invented']
    assert normalize(data,frames(),'source','coach')['points']==[]
    data=scene();data['points'][0]['success_check']=''
    assert normalize(data,frames(),'source','coach')['points']==[]


def test_context_includes_preparation_and_recovery_not_just_half_a_second():
    timeline=[{'time':i*.5,'frame_id':str(i)} for i in range(40)]
    selected=context_frames(timeline,{'focus':10},20)
    assert selected[0]['time']==6 and selected[-1]['time']==13.5 and len(selected)==16


def test_actionable_scene_is_rendered_when_no_habit_is_proven():
    point=normalize(scene(),frames(),'source','coach')['points'][0]
    report={'scene_coaching':[point],'tendencies':[],'strengths':[],'improvements':[],'scene_ids':[],'limitations':[]}
    status={'sampling_coverage':1,'completed_units':1,'total_units':1,'spent_usd':.02,'reserved_usd':0,'cap_usd':3.5,
            'missing_ranges':[],'player_confirmed_ranges':[[0,2]]}
    answer=render_report({'filename':'game.mp4'},status,report,[])
    assert '라켓을 몸 앞으로' in answer and '8회씩2세트' in answer and '다음 영상에서 확인할 기준' in answer
    assert '반복 근거가 부족해 확정하지 않았습니다' not in answer
    assert '연습은 제안하지 않습니다' not in answer
    assert answer.index('바꿀 행동')<answer.index('판독 범위와 한계')


def test_no_coaching_points_is_not_a_completed_coaching_review(system,clip):
    settings,store,videos=system
    reviews,review,run,_=setup_review(system,clip)
    class RestProvider(FakeReviewProvider):
        def create(self,purpose,**kwargs):
            result=super().create(purpose,**kwargs)
            if purpose=='review_coaching':
                raw=scene();raw.update(activity='rest_or_collection',phases=[],points=[])
                result.raw['output'][0]['content'][0]['text']=encode(raw)
            return result
    provider=RestProvider()
    MatchReviewer(settings,store,videos,run,review['id'],provider).execute()
    assert reviews.status(review['id'])['status']=='partial'
    assert store.run(run['id'])['response_kind']=='partial'


def test_saved_report_refresh_uses_same_cap_and_does_not_hold_another_summary(system,clip):
    settings,store,_=system
    reviews,review,run,_=setup_review(system,clip,cap=.25)
    with pytest.raises(BudgetExceeded):reviews.reserve(review['id'],run['id'],'coaching',1000,1000)
    store.execute("UPDATE reviews SET status='completed',report='Saved result' WHERE id=?",(review['id'],))
    store.update_run(run['id'],'completed',answer='Saved result')
    reviews.reserve(review['id'],run['id'],'coaching',1000,1000)
    assert reviews.status(review['id'])['cap_usd']==.25


def test_new_stroke_is_not_used_as_slow_recovery_of_previous_stroke():
    timeline=frames()+[{'frame_id':'F2','time':2.25}]
    data=scene('training_experiment')
    data.update(primary_action_frame_id='F1',next_action_frame_id='F2')
    data['points'][0]['basis_frame_ids']=['F1','F2']
    assert normalize(data,timeline,'source','coach',focus=1.75)['points']==[]
    data['points'][0]['basis_frame_ids']=['F0','F1']
    assert normalize(data,timeline,'source','coach',focus=1.75)['points']


def test_focus_and_fall_claims_need_actual_evidence():
    data=scene()
    data.update(primary_action_frame_id='F0',next_action_frame_id='none')
    with pytest.raises(ValueError,match='지정 구간'):
        normalize(data,frames(),'source','coach',focus=20)
    data['points'][0]['observation']='선수가 쓰러지고 균형을 잃었다.'
    assert normalize(data,frames(),'source','coach',focus=1.25)['points']==[]


def test_new_coaching_version_is_persisted_and_reused_without_another_generation(system,clip):
    from badminton.coaching import analyze,calibrate_plan,vision_points
    from badminton.review import extract_match_frames
    settings,store,videos=system
    reviews,review,run,_=setup_review(system,clip)
    # The old version used the timestamp as its unique position. A new version
    # at that same timestamp must not silently lose a paid response.
    store.execute('INSERT INTO review_units(id,review_id,kind,start,end,position,status,result) VALUES(?,?,?,?,?,?,?,?)',
        ('old',review['id'],'coaching',0,2.5,500000,'completed',
         encode({'version':'scene-coaching-v1','source_observation_id':'source','points':[]})))
    provider=FakeReviewProvider()
    reviewer=MatchReviewer(settings,store,videos,run,review['id'],provider)
    timeline=extract_match_frames(settings,clip);source={'id':'source','focus':.5}
    result=analyze(reviewer,source,timeline)
    assert result['points'] and len(store.all("SELECT id FROM review_units WHERE kind='coaching'"))==2
    before=reviews.money(review['id'])
    assert analyze(reviewer,source,timeline)==result
    assert provider.calls==['review_coaching'] and reviews.money(review['id'])==before
    first=calibrate_plan(reviewer,vision_points(store,review['id']))
    before=reviews.money(review['id'])
    assert first==calibrate_plan(reviewer,vision_points(store,review['id']))
    assert provider.calls==['review_coaching','review_coaching'] and reviews.money(review['id'])==before
    assert saved_points(store,review['id'])==first
