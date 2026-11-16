import concurrent.futures
import json
import time

import pytest
from fastapi.testclient import TestClient

from badminton.agent import ToolSession,native_run
from badminton.memory import Memory
from badminton.review import MatchReviewer
from badminton.service import create_app
from badminton.store import Store,encode
from tests.test_conversation import AnswerProvider
from tests.test_match_review import setup_review,FakeReviewProvider


def saved_clip(store,clip,request='clip-memory',reused=False):
    run=store.create_run('user1','chat1',request,'내 영상에서 스매시 준비를 확인해줘',[clip['id']])
    store.cache('evidence:'+run['id'],'','evidence',[{'video_id':clip['id'],'reused':reused,
        'frames':[{'time':1.0},{'time':1.5}],
        'sequence':{'batches':[{'summary':'라켓을 몸 앞에 두고 준비한다.','limitations':['접촉은 불명확하다.']}]}}])
    store.execute("UPDATE runs SET response_kind='analysis' WHERE id=?",(run['id'],))
    store.update_run(run['id'],'completed',answer='타구 전에 라켓을 준비하세요.')
    return run


def test_profile_survives_restart_and_is_retrieved_in_new_chat_only_for_its_user(system,monkeypatch):
    settings,store,videos=system;memory=Memory(store)
    memory.command('u','old','save','기억저장 나는 왼손잡이고 복식 위주로 쳐.')
    restarted=Store(settings.root/'agent.db')
    run=restarted.create_run('u','new','ask','내 특성에 맞는 준비 운동을 알려줘',[])
    monkeypatch.setattr(videos,'frames',lambda *a,**k:pytest.fail('Memory retrieval must not decode video'))
    provider=AnswerProvider('general','왼손잡이 복식에 맞춰 준비하세요.')
    native_run(settings,restarted,videos,run,provider)
    context=json.loads(provider.requests[0][1]['input'][1]['content'])['context']
    assert context['history']==[] and '왼손잡이' in context['personal_memory']['profiles'][0]['content']
    assert 'get_user_memory' in {t['name'] for t in provider.requests[0][1]['tools']}
    assert not Memory(restarted).context('other','왼손잡이')['profiles']


def test_auto_facts_require_current_literal_user_statement_and_newer_fact_supersedes(system):
    _,store,_=system;memory=Memory(store)
    first=store.create_run('u','old','first','나는 오른손잡이야.',[])
    memory.capture_facts(first,[{'field':'handedness','quote':'나는 오른손잡이야.'},
        {'field':'goal','quote':'풋워크 개선이 목표다.'},{'field':'player','quote':'나는 오른손잡이야.'}])
    assert len(memory.rows('u'))==1
    second=store.create_run('u','new','second','정정할게. 나는 왼손잡이야.',[])
    memory.capture_facts(second,[{'field':'handedness','quote':'나는 왼손잡이야.'}])
    memory.capture_facts(first,[{'field':'handedness','quote':'나는 오른손잡이야.'}])
    assert [r['content'] for r in memory.rows('u')]==['나는 왼손잡이야.']
    conditional=store.create_run('conditional','c','hypo','만약 내가 왼손잡이라면 어떻게 해?',[])
    memory.capture_facts(conditional,[{'field':'handedness','quote':'왼손잡이'}])
    assert memory.rows('conditional')==[]


def test_completed_review_becomes_one_source_linked_episode_with_hypotheses_not_profile(system,clip):
    settings,store,videos=system
    reviews,review,run,_=setup_review(system,clip)
    answer=MatchReviewer(settings,store,videos,run,review['id'],FakeReviewProvider()).execute()
    store.update_run(run['id'],'completed',answer=answer)
    memory=Memory(store);memory.backfill('user1');memory.backfill('user1')
    rows=memory.rows('user1');assert len(rows)==1 and rows[0]['kind']=='episode'
    detail=json.loads(rows[0]['detail'])
    assert detail['coaching'][0]['assessment']=='training_experiment'
    assert detail['target_relation']=='self_reported' and detail['filename']==clip['filename']
    assert rows[0]['source_id']==review['id']
    assert not memory.context('user1')['profiles']


def test_cross_chat_episode_text_never_grants_video_access(system,clip):
    settings,store,videos=system;saved_clip(store,clip)
    run=store.create_run('user1','new-chat','ask','이전에 본 내 스매시를 설명해줘',[])
    session=ToolSession(settings,store,videos,run,None)
    result,frames=session.execute('get_user_memory',{'query':'스매시'})
    assert result['episodes'] and frames==[] and clip['filename'] in result['episodes'][0]['content']
    with pytest.raises(PermissionError):session.video(clip['id'])
    foreign=store.create_run('other','new-chat','ask','기억을 보여줘',[])
    assert not ToolSession(settings,store,videos,foreign,None).history()['personal_memory']['episodes']


def test_reused_observations_do_not_create_duplicate_episode(system,clip):
    _,store,_=system;saved_clip(store,clip);saved_clip(store,clip,'reuse',reused=True)
    assert len(Memory(store).rows('user1'))==1


def test_delete_and_user_correction_are_not_undone_by_backfill(system,clip):
    _,store,_=system;run=saved_clip(store,clip);memory=Memory(store);key=memory.rows('user1')[0]['id']
    memory.command('user1','new','edit','기억수정 '+key[:12]+' 그 장면은 내 연습 스윙이었어.')
    memory.index_run(run['id'])
    assert memory.item('user1',key)['content']=='그 장면은 내 연습 스윙이었어.'
    assert memory.item('user1',key)['confidence']=='user_corrected'
    memory.command('user1','new','forget','기억삭제 '+key[:12]);memory.backfill('user1')
    assert not memory.rows('user1')
    erased=store.one('SELECT * FROM user_memories WHERE id=?',(key,))
    assert erased['status']=='deleted' and erased['content']=='' and erased['detail']=='{}'


def test_clear_keeps_cutoff_and_duplicate_clear_does_not_erase_new_memory(system,clip):
    _,store,_=system;saved_clip(store,clip);memory=Memory(store)
    memory.command('user1','c','clear','기억전체삭제');memory.backfill('user1')
    assert not memory.rows('user1')
    memory.command('user1','c','new','기억저장 새로운 연습 목표는 복귀야.')
    memory.command('user1','c','clear','기억전체삭제')
    assert len(memory.rows('user1'))==1


def test_disabled_memory_is_neither_retrieved_nor_automatically_saved(system,clip):
    _,store,_=system;memory=Memory(store)
    memory.command('user1','c','save','기억저장 복식 위주로 쳐.')
    memory.command('user1','c','off','기억끄기');saved_clip(store,clip)
    assert not memory.context('user1')['enabled'] and not memory.context('user1')['profiles']
    assert len(memory.rows('user1'))==1
    row=store.create_run('user1','c','fact','나는 왼손잡이야.',[])
    memory.capture_facts(row,[{'field':'handedness','quote':'나는 왼손잡이야.'}])
    assert len(memory.rows('user1'))==1
    memory.command('user1','c','on','기억켜기')
    assert memory.context('user1')['profiles']
    memory.backfill('user1')
    assert not memory.context('user1')['episodes']  # Off-period data stays excluded after enabling.


def test_recurrence_counts_separate_videos_and_retains_experiment_label(system):
    _,store,_=system;memory=Memory(store)
    def add(source,video_hash):
        return memory.put('u','episode','review','복귀 연습',{'video_hash':video_hash,'tags':['recovery'],
            'coaching':[{'topic':'recovery','assessment':'training_experiment','action':'복귀를 비교해 보세요.'}]},'review',source,'c')
    add('first','same');add('again','same')
    assert not memory.context('u','복귀')['recurring_topics']
    add('second','different')
    trend=memory.context('u','복귀')['recurring_topics'][0]
    assert trend['separate_videos']==2 and all(r['assessment']=='training_experiment' for r in trend['records'])


def test_commands_are_free_immediate_and_user_scoped_over_api(system):
    settings,store,_=system
    def forbidden(*args):pytest.fail('Memory commands must not call the model runner')
    with TestClient(create_app(settings,runner=forbidden)) as client:
        base={'user_id':'u','chat_id':'old','request_id':'save','question':'기억저장 복식 위주로 쳐.'}
        assert client.post('/internal/runs',json=base).status_code==401
        client.headers['Authorization']='Bearer '+settings.token
        run=client.post('/internal/runs',json=base).json()
        assert run['status']=='completed' and run['response_kind']=='memory'
        assert client.post('/internal/runs',json=base).json()['id']==run['id']
        listing=client.get('/internal/memory',params={'user_id':'u','chat_id':'new'}).json()
        key=listing['items'][0]['id']
        assert client.get('/internal/memory',params={'user_id':'other','chat_id':'new'}).json()['items']==[]
        assert client.post('/internal/memory/command',json={**base,'user_id':'other','question':'기억삭제 '+key}).status_code==404
        assert client.post('/internal/memory/command',json={**base,'request_id':'edit','question':'기억수정 '+key+' 이제 단식 위주야.'}).status_code==200
        assert not store.all('SELECT * FROM calls')


def test_concurrent_duplicate_command_stores_one_memory(system):
    _,store,_=system;memory=Memory(store)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        answers=list(pool.map(lambda _:memory.command('u','c','same','기억저장 복귀가 목표야.'),range(4)))
    assert len(memory.rows('u'))==1 and len(set(answers))==1


def test_memory_commands_are_not_replayed_in_model_chat_history(system):
    settings,store,videos=system
    memory=Memory(store);memory.command('u','c','save','기억저장 복식 위주야.')
    run=store.create_run('u','c','control','기억목록',[],initial_status='completed')
    store.execute("UPDATE runs SET response_kind='memory' WHERE id=?",(run['id'],))
    store.update_run(run['id'],'completed',answer=memory.listing('u'))
    memory.command('u','c','clear','기억전체삭제')
    current=store.create_run('u','c','new','내 기억이 있니?',[])
    context=ToolSession(settings,store,videos,current,None).history()
    assert context['history']==[] and not context['personal_memory']['profiles']


def test_video_deletion_removes_derived_memory_across_chats(system,clip):
    settings,store,_=system;saved_clip(store,clip);assert Memory(store).rows('user1')
    with TestClient(create_app(settings,runner=lambda *a:'unused')) as client:
        client.headers['Authorization']='Bearer '+settings.token
        assert client.delete('/internal/videos/'+clip['id'],params={'user_id':'user1','chat_id':'chat1'}).status_code==200
    assert not Memory(store).context('user1')['episodes']


def test_retrieval_is_relevant_and_context_is_bounded(system):
    _,store,_=system;memory=Memory(store)
    for n in range(12):
        text='풋워크 복귀 연습' if n==0 else '스매시 연습'
        memory.put('u','episode','review',text*100,{'tags':tags_for_test(text)},'review',str(n),'c')
    result=memory.context('u','풋워크 복귀')
    assert '풋워크' in result['episodes'][0]['content'] and len(encode(result))<=12000


def tags_for_test(text):
    from badminton.memory import tags
    return tags(text)


def test_large_profile_and_recurring_history_still_respect_memory_context_budget(system):
    _,store,_=system;memory=Memory(store)
    for n in range(12):
        memory.put('u','profile','note','사용자 진술'*400,{'tags':['recovery']},'manual',str(n)+'x'*350,'c')
    for n in range(4):
        memory.put('u','episode','review','복귀 연습'*400,{'video_hash':str(n),'filename':'a'*400+'.mp4','tags':['recovery'],
            'coaching':[{'topic':topic,'assessment':'training_experiment','action':'연습 방법'*400} for topic in ['recovery','movement','preparation','swing']]},'review','r'+str(n),'c')
    assert len(encode(memory.context('u','복귀 연습')))<=12000


def test_many_notes_do_not_displace_core_profile_facts(system):
    _,store,_=system;memory=Memory(store)
    memory.put('u','profile','handedness','왼손잡이',{},'statement','hand','c')
    for n in range(15):memory.put('u','profile','note','추가 메모 '+str(n),{},'manual',str(n),'c')
    assert any('왼손잡이' in r['content'] for r in memory.context('u')['profiles'])
