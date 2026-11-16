import json

import pytest
from fastapi.testclient import TestClient

from badminton.agent import ModelResponse, ToolSession, native_run
from badminton.service import create_app


class AnswerProvider:
    def __init__(self, kind, answer):
        self.kind, self.answer = kind, answer
        self.requests = []

    def create(self, purpose, **kwargs):
        self.requests.append((purpose,kwargs))
        return ModelResponse({'output':[{'type':'function_call','call_id':'finish','name':'complete_analysis',
            'arguments':json.dumps({'kind':self.kind,'answer':self.answer,'evidence_ids':[]})}]})


@pytest.mark.parametrize('attached',[False,True])
def test_general_grip_question_never_forces_video(system,clip,attached,monkeypatch):
    settings,store,videos=system
    ids=[clip['id']] if attached else []
    run=store.create_run('user1','chat1','general','스매시 스윙할때 이스턴 그립이 맞아? 아니면 주먹그립이 맞아?',ids)
    monkeypatch.setattr(videos,'frames',lambda *a,**k:pytest.fail('일반 질문에서 영상을 읽었습니다.'))
    provider=AnswerProvider('general','일반적으로 스매시의 그립과 손잡이를 쥐는 방법을 구분해서 설명하겠습니다.')
    assert native_run(settings,store,videos,run,provider)==provider.answer
    assert len(provider.requests)==1 and provider.requests[0][0]=='agent'
    assert store.run(run['id'])['response_kind']=='general'
    assert store.events(run['id'])==[]


def test_general_followup_is_not_forced_by_previous_video_analysis(system,clip):
    settings,store,videos=system
    previous=store.create_run('user1','chat1','previous','그립과 스매시를 비교해줘',[clip['id']])
    store.update_run(previous['id'],'completed',answer='확대해도 손가락은 불명확했습니다.')
    current=store.create_run('user1','chat1','current','회내를 더 쉽게 설명해줘',[clip['id']])
    provider=AnswerProvider('general','회내는 전완을 안쪽으로 돌리는 움직임입니다.')
    assert native_run(settings,store,videos,current,provider)==provider.answer
    assert len(provider.requests)==1


def test_general_chat_remembers_previous_answer_without_upload(system):
    settings,store,videos=system
    first=store.create_run('u','c','a','연습 계획을 알려줘',[])
    answer=native_run(settings,store,videos,first,AnswerProvider('general','월요일은 풋워크를 연습하세요.'))
    store.update_run(first['id'],'completed',answer=answer)
    second=store.create_run('u','c','b','그 계획을 짧게 요약해줘',[])
    provider=AnswerProvider('context','월요일: 풋워크.')
    assert native_run(settings,store,videos,second,provider)==provider.answer
    context=json.loads(provider.requests[0][1]['input'][1]['content'])['context']
    assert context['history'][0]['answer']==answer


def test_explanation_uses_saved_observations_without_reading_frames(system,clip,monkeypatch):
    settings,store,videos=system
    previous=store.create_run('user1','chat1','past','풋워크를 확인해줘',[clip['id']])
    session=ToolSession(settings,store,videos,previous,None)
    session.execute('inspect_segment',{'video_id':clip['id'],'start':0,'end':1,'fps':2})
    answer=session.finalize('첫 구간에서 보인 움직임을 설명했습니다.',evidence_ids=['E1'])
    store.update_run(previous['id'],'completed',answer=answer)
    current=store.create_run('user1','chat1','explain','아까 말한 걸 쉽게 설명해줘',[clip['id']])
    monkeypatch.setattr(videos,'frames',lambda *a,**k:pytest.fail('이전 설명에서 영상을 다시 읽었습니다.'))
    provider=AnswerProvider('context','아까 관찰한 내용만 다시 설명하겠습니다.')
    assert native_run(settings,store,videos,current,provider)==provider.answer
    context=json.loads(provider.requests[0][1]['input'][1]['content'])['context']
    assert context['saved_observations'][0]['filename']==clip['filename']
    assert context['saved_observations'][0]['start']==0
    assert not store.cached('evidence:'+current['id'])


def test_missing_video_does_not_skip_harness_for_general_questions(system):
    import time
    settings,store,_=system
    seen=[]
    def runner(settings,store,videos,run):
        seen.append(run['question'])
        return native_run(settings,store,videos,run,AnswerProvider('general','영상 없이 일반 원리를 설명합니다.'))
    with TestClient(create_app(settings,runner=runner)) as client:
        client.headers['Authorization']='Bearer '+settings.token
        run=client.post('/internal/runs',json={'user_id':'u','chat_id':'c','request_id':'q','question':'스매시 그립이 궁금해'}).json()
        for _ in range(80):
            result=store.run(run['id'])
            if result['status']=='completed':break
            time.sleep(.03)
        assert result['answer']=='영상 없이 일반 원리를 설명합니다.'
        assert seen==['스매시 그립이 궁금해']


def test_new_video_claim_still_requires_evidence(system):
    settings,store,videos=system
    run=store.create_run('u','c','q','내 영상의 그립을 판단해줘',[])
    session=ToolSession(settings,store,videos,run,None)
    with pytest.raises(ValueError,match='실제 판독 근거'):
        session.finalize('당신은 주먹그립을 씁니다.',kind='analysis',evidence_ids=[])
    assert session.finalize('개인의 실제 그립을 보려면 영상을 첨부해 주세요.',kind='clarification',evidence_ids=[])


def test_new_frame_inspection_cannot_bypass_evidence_as_context(system,clip):
    settings,store,videos=system
    run=store.create_run('user1','chat1','q','영상의 움직임을 봐줘',[clip['id']])
    session=ToolSession(settings,store,videos,run,None)
    session.execute('inspect_segment',{'video_id':clip['id'],'start':0,'end':1,'fps':2})
    assert '직접 확인한 근거' in session.finalize('방금 본 장면입니다.',kind='context',evidence_ids=[])
    assert store.run(run['id'])['response_kind']=='analysis'
    assert store.all("SELECT id FROM events WHERE run_id=? AND kind='completion_normalized'",(run['id'],))


def test_explicit_contact_point_does_not_inherit_previous_general_grip(system,clip):
    settings,store,videos=system
    previous=store.create_run('user1','chat1','grip','망치그립이 스매시에 불리한 이유를 설명해봐',[clip['id']])
    store.execute('UPDATE runs SET response_kind=? WHERE id=?',('general',previous['id']))
    store.update_run(previous['id'],'completed',answer='일반적인 원리 설명')
    question='오케이 알겠어 그럼 그 다음으로는 타점이 맞는지가 궁금한데 1109랑 1026 비교했을때 타점은 어떄?? 둘중에 어떤 타점이 더 맞아보여/'
    current=store.create_run('user1','chat1','contact',question,[clip['id']])
    policy=ToolSession(settings,store,videos,current,None).policy
    assert policy['precise'] and policy['comparison'] and not policy['grip']


def test_reuse_observations_is_scoped_and_does_not_read_frames(system,clip,monkeypatch):
    settings,store,videos=system
    previous=store.create_run('user1','chat1','past','풋워크를 봐줘',[clip['id']])
    old=ToolSession(settings,store,videos,previous,None)
    old.execute('inspect_segment',{'video_id':clip['id'],'start':0,'end':1,'fps':2})
    answer=old.finalize('확인한 움직임입니다.',evidence_ids=['E1'])
    store.update_run(previous['id'],'completed',answer=answer)
    current=store.create_run('user1','chat1','reuse','풋워크의 차이를 설명해줘',[clip['id']])
    session=ToolSession(settings,store,videos,current,None)
    monkeypatch.setattr(videos,'frames',lambda *a,**k:pytest.fail('저장 관찰을 재사용하며 프레임을 읽었습니다.'))
    for _ in range(2):session.execute('reuse_observations',{'run_id':previous['id'],'evidence_ids':['E1']})
    assert len(session.evidence)==1 and session.images==0 and session.precision_calls==0
    assert '기존 판독 재사용' in session.finalize('기존 관찰로 설명합니다.',kind='context',evidence_ids=[])
    foreign=store.create_run('other','chat1','foreign','다른 사용자',[])
    with pytest.raises(PermissionError):
        ToolSession(settings,store,videos,foreign,None).execute('reuse_observations',{'run_id':previous['id'],'evidence_ids':['E1']})
    disconnected=store.create_run('user1','chat1','none','현재 영상 없음',[])
    with pytest.raises(PermissionError):
        ToolSession(settings,store,videos,disconnected,None).execute('reuse_observations',{'run_id':previous['id'],'evidence_ids':['E1']})


def test_missing_comparison_evidence_returns_safe_partial_after_repeated_finish(system,clip):
    import shutil
    from tests.test_precision import FrameIdProvider
    settings,store,videos=system
    path=settings.webui/'uploads'/'second.mp4';shutil.copyfile(clip['path'],path)
    second=videos.register(str(path),'second','second.mp4','user1','chat1')
    run=store.create_run('user1','chat1','partial','1초 타점을 비교해줘',[clip['id'],second['id']])
    class Provider(FrameIdProvider):
        agent_calls=0
        def create(self,purpose,**kwargs):
            if purpose=='precision':return super().create(purpose,**kwargs)
            self.agent_calls+=1
            if self.agent_calls==1:
                name,args='inspect_segment',{'video_id':clip['id'],'start':0,'end':.8,'fps':0}
            else:
                name,args='complete_analysis',{'kind':'context','answer':'첫 영상이 더 낫습니다.','evidence_ids':[]}
            return ModelResponse({'output':[{'type':'function_call','call_id':str(self.agent_calls),'name':name,'arguments':json.dumps(args)}]})
    provider=Provider();answer=native_run(settings,store,videos,run,provider)
    assert store.run(run['id'])['response_kind']=='partial' and provider.agent_calls==4
    assert '비교를 확정하기 어렵습니다' in answer and 'second.mp4' in answer
    assert '첫 영상이 더 낫습니다' not in answer and '정밀 근거 검증을 완료하지 못했습니다' not in answer


def test_precise_comparison_normalizes_context_after_reading_both_videos(system,clip):
    import shutil
    from tests.test_precision import FrameIdProvider
    settings,store,videos=system
    path=settings.webui/'uploads'/'other.mp4';shutil.copyfile(clip['path'],path)
    other=videos.register(str(path),'other','other.mp4','user1','chat1')
    run=store.create_run('user1','chat1','normalize','두 영상의 타점을 비교해줘',[clip['id'],other['id']])
    steps=[(clip['id'],0),(other['id'],0),(clip['id'],2),(other['id'],2)]
    class Provider(FrameIdProvider):
        agent_calls=0
        def create(self,purpose,**kwargs):
            if purpose=='precision':return super().create(purpose,**kwargs)
            index=self.agent_calls;self.agent_calls+=1
            if index<len(steps):
                video_id,start=steps[index]
                name,args='inspect_segment',{'video_id':video_id,'start':start,'end':start+.8,'fps':0}
            else:
                name,args='complete_analysis',{'kind':'context','answer':'두 영상 모두 실제 접촉 위치를 확정하기에는 불명확합니다.','evidence_ids':[]}
            return ModelResponse({'output':[{'type':'function_call','call_id':str(index),'name':name,'arguments':json.dumps(args)}]})
    provider=Provider();answer=native_run(settings,store,videos,run,provider)
    assert store.run(run['id'])['response_kind']=='analysis' and provider.agent_calls==5
    assert '직접 확인한 근거' in answer and 'other.mp4' in answer
    assert len(store.cached('evidence:'+run['id']))==4
