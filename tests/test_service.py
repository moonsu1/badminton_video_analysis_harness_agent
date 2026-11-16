import threading
import time

from fastapi.testclient import TestClient

from badminton.service import create_app


def test_api_auth_scope_dedup_and_results(system, clip):
    settings, store, videos = system
    def runner(*args):
        return '관찰 [00:01] — 실제 시험용 결과'
    app = create_app(settings, runner=runner)
    with TestClient(app) as client:
        identity = {'user_id': 'user1', 'chat_id': 'chat1'}
        body = {**identity, 'request_id': 'request1', 'question': '풋워크', 'video_ids': [clip['id']]}
        assert client.post('/internal/runs', json=body).status_code == 401
        client.headers['Authorization'] = 'Bearer ' + settings.token
        run = client.post('/internal/runs', json=body).json()
        assert client.post('/internal/runs', json=body).json()['id'] == run['id']
        assert client.get('/internal/runs/' + run['id'], params={**identity, 'chat_id': 'wrong'}).status_code == 404
        assert client.post('/internal/runs', json={**body, 'request_id': 'bad', 'user_id': 'wrong'}).status_code == 404
        for _ in range(60):
            result = client.get('/internal/runs/' + run['id'], params=identity).json()
            if result['status'] == 'completed':
                break
            time.sleep(.05)
        assert result['answer'].startswith('관찰')
        events = client.get('/internal/runs/' + run['id'] + '/events', params=identity)
        assert 'completed' in events.text
        # A fresh UI subscription reads persisted results without executing another run.
        assert client.get('/internal/runs/latest', params=identity).json()['id'] == run['id']
        assert len(store.all('SELECT * FROM runs')) == 1
        assert client.delete('/internal/videos/' + clip['id'], params={**identity, 'user_id': 'wrong'}).status_code == 404
        assert client.delete('/internal/videos/' + clip['id'], params=identity).status_code == 200
        from pathlib import Path
        assert not Path(clip['path']).exists()
        assert not store.all('SELECT * FROM runs')


def test_cancel_waiting_job_and_disconnect(system, clip):
    settings, store, videos = system
    started, release = threading.Event(), threading.Event()
    def runner(*args):
        started.set()
        release.wait(5)
        return 'done'
    app = create_app(settings, runner=runner)
    identity = {'user_id': 'user1', 'chat_id': 'chat1'}
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer ' + settings.token
        first = client.post('/internal/runs', json={**identity, 'request_id': 'a', 'question': 'a', 'video_ids': [clip['id']]}).json()
        assert started.wait(3)
        second = client.post('/internal/runs', json={**identity, 'request_id': 'b', 'question': 'b', 'video_ids': [clip['id']]}).json()
        client.post('/internal/runs/' + second['id'] + '/cancel', json=identity)
        # No browser stream is needed for execution to complete.
        release.set()
        for _ in range(50):
            if store.run(second['id'])['status'] == 'cancelled':
                break
            time.sleep(.05)
        assert store.run(first['id'])['status'] == 'completed'
        assert store.run(second['id'])['status'] == 'cancelled'


def test_restart_does_not_replay_uncertain_call(system):
    settings, store, _ = system
    run = store.create_run('u', 'c', 'r', 'q', [])
    store.update_run(run['id'], 'analyzing')
    store.execute("INSERT INTO calls(id,run_id,purpose,status,usage,error,created) VALUES('call',?,'agent','running',NULL,NULL,0)", (run['id'],))
    store.recover()
    assert store.run(run['id'])['status'] == 'failed'
    assert store.one("SELECT status FROM calls WHERE id='call'")['status'] == 'unknown'


def test_worker_lease_prevents_two_workers(system):
    import pytest
    from badminton.service import WorkerLease
    settings, store, _ = system
    first, second = WorkerLease(settings.root / 'test.lock'), WorkerLease(settings.root / 'test.lock')
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match='이미 실행'):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_active_attachment_replacement_and_followup_are_scoped(system,clip):
    import json
    import shutil
    settings,store,videos=system
    path=settings.webui/'uploads'/'corrected.mp4'
    shutil.copyfile(clip['path'],path)
    corrected=videos.register(str(path),'corrected','corrected.mp4','user1','chat1')
    identity={'user_id':'user1','chat_id':'chat1'}
    with TestClient(create_app(settings,runner=lambda *args:'saved')) as client:
        assert client.post('/internal/context',json={**identity,'video_ids':[clip['id']]}).status_code==401
        client.headers['Authorization']='Bearer '+settings.token
        assert client.post('/internal/context',json={**identity,'user_id':'other','video_ids':[clip['id']]}).status_code==404
        assert client.post('/internal/context',json={**identity,'video_ids':[clip['id']]}).status_code==200
        assert client.post('/internal/context',json={**identity,'video_ids':[corrected['id']]}).json()['video_ids']==[corrected['id']]
        result=client.post('/internal/runs',json={**identity,'request_id':'followup','question':'같은 영상을 더 봐줘'}).json()
        assert json.loads(store.run(result['id'])['video_ids'])==[corrected['id']]
