import asyncio
import json

import httpx

import openwebui_coach_pipe


def test_result_command_uses_saved_answer_without_new_run(tmp_path, monkeypatch):
    (tmp_path / 'service.key').write_text('secret')
    monkeypatch.setenv('BADMINTON_DATA_DIR', str(tmp_path))
    requests = []
    def handle(request):
        requests.append(request)
        assert request.headers['authorization'] == 'Bearer secret'
        if request.url.path=='/internal/reviews/latest':return httpx.Response(200,content=b'null')
        return httpx.Response(200, json={'id': 'saved', 'status': 'completed', 'answer': '저장된 답변'})
    real_client = httpx.AsyncClient
    monkeypatch.setattr(openwebui_coach_pipe.httpx, 'AsyncClient', lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    async def run():
        return [text async for text in openwebui_coach_pipe.Pipe().pipe({'messages': [{'role': 'user', 'content': '결과'}]}, __user__={'id': 'u'}, __metadata__={'chat_id': 'c'})]
    assert asyncio.run(run()) == ['저장된 답변']
    assert len(requests) == 2
    assert all(r.method=='GET' for r in requests)
    assert requests[0].url.params['chat_id'] == 'c'


def test_background_title_does_not_start_analysis():
    async def run():
        return [text async for text in openwebui_coach_pipe.Pipe().pipe({}, __task__='title_generation')]
    assert '배드민턴' in asyncio.run(run())[0]


def test_match_result_prefers_saved_report_and_never_creates_run(tmp_path,monkeypatch):
    (tmp_path/'service.key').write_text('secret')
    monkeypatch.setenv('BADMINTON_DATA_DIR',str(tmp_path))
    requests=[]
    def handle(request):
        requests.append(request)
        assert request.method=='GET'
        if request.url.path.endswith('reviews/latest'):
            return httpx.Response(200,json={'status':'completed','report':'경기 전체 저장 보고서'})
        return httpx.Response(200,json={'id':'general','status':'completed','answer':'일반 그립 답변'})
    client=httpx.AsyncClient
    monkeypatch.setattr(openwebui_coach_pipe.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handle),**kw))
    async def run():
        return [s async for s in openwebui_coach_pipe.Pipe().pipe({'messages':[{'role':'user','content':'결과'}]},
            __user__={'id':'u'},__metadata__={'chat_id':'c'})]
    assert asyncio.run(run())==['경기 전체 저장 보고서']
    assert len(requests)==2


def test_cancel_targets_active_review_after_general_chat(tmp_path,monkeypatch):
    (tmp_path/'service.key').write_text('secret')
    monkeypatch.setenv('BADMINTON_DATA_DIR',str(tmp_path))
    def handle(request):
        if request.url.path.endswith('/reviews/latest'):
            return httpx.Response(200,json={'status':'running','run_id':'long-review'})
        if request.method=='POST':
            assert request.url.path=='/internal/runs/long-review/cancel'
            return httpx.Response(200,json={'cancel_requested':True})
        return httpx.Response(200,json={'id':'general','status':'completed'})
    client=httpx.AsyncClient
    monkeypatch.setattr(openwebui_coach_pipe.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handle),**kw))
    async def run():
        return [s async for s in openwebui_coach_pipe.Pipe().pipe({'messages':[{'role':'user','content':'취소'}]},
            __user__={'id':'u'},__metadata__={'chat_id':'c'})]
    assert '취소를 요청' in asyncio.run(run())[0]


def test_review_preview_stream_once_then_final(tmp_path,monkeypatch):
    (tmp_path/'service.key').write_text('secret')
    monkeypatch.setenv('BADMINTON_DATA_DIR',str(tmp_path))
    preview={'kind':'review_preview','data':{'text':'중간 요약'}}
    progress={'kind':'progress','data':{'message':'판독50%'}}
    def handle(request):
        if request.url.path.endswith('/events'):
            return httpx.Response(200,text='\n'.join('data: '+json.dumps(e) for e in [preview,preview,progress]))
        if request.url.path=='/internal/runs/latest':return httpx.Response(200,content=b'null')
        if request.method=='POST':return httpx.Response(200,json={'id':'review-run'})
        return httpx.Response(200,json={'status':'completed','answer':'최종 리뷰'})
    client=httpx.AsyncClient
    monkeypatch.setattr(openwebui_coach_pipe.httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(handle),**kw))
    async def run():
        return [s async for s in openwebui_coach_pipe.Pipe().pipe({'messages':[{'role':'user','content':'경기 전체 분석'}]},
            __user__={'id':'u'},__metadata__={'chat_id':'c'})]
    assert asyncio.run(run())==['중간 요약\n\n---\n\n','최종 리뷰']
