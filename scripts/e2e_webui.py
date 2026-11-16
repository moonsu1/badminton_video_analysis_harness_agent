"""Isolated real Open WebUI upload -> harness -> follow-up. Test account stays in temp data."""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx

from badminton.bootstrap import PROJECT
from badminton.video import ffmpeg_binary


def wait(url, process):
    for _ in range(180):
        if process.poll() is not None:
            raise RuntimeError('test server exited')
        try:
            if httpx.get(url, timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError('test server did not become ready')


def main():
    root = Path(tempfile.mkdtemp(prefix='badminton-e2e-'))
    root.joinpath('settings.json').write_text(json.dumps({'port': 8766}), encoding='utf-8')
    env = {**os.environ, 'BADMINTON_DATA_DIR': str(root), 'BADMINTON_WEBUI_PORT': '8081', 'PYTHONUTF8': '1'}
    report = {'test': 'isolated_openwebui_real_api', 'data_directory': str(root)}
    processes = []
    try:
        with (root / 'setup.log').open('w', encoding='utf-8') as output:
            subprocess.run([sys.executable, '-m', 'badminton.bootstrap'], cwd=PROJECT, env=env, stdout=output, stderr=output, check=True)
        for module, port in [('badminton.service', 8766), ('badminton.webui', 8081)]:
            log = (root / (module + '.log')).open('w', encoding='utf-8')
            process = subprocess.Popen([sys.executable, '-m', module], cwd=PROJECT, env=env, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process)
            log.close()
            wait(f'http://127.0.0.1:{port}/health', process)
        with httpx.Client(base_url='http://127.0.0.1:8081', timeout=300, trust_env=False) as client:
            r = client.post('/api/v1/auths/signup', json={'name': 'Isolated integration test', 'email': 'test@example.invalid', 'password': secrets.token_urlsafe(30)})
            if r.status_code != 200:
                raise RuntimeError(f'test signup HTTP {r.status_code}: {r.text[:300]}')
            client.headers['Authorization'] = 'Bearer ' + r.json()['token']
            report['signup'] = r.status_code
            r = client.get('/api/models')
            r.raise_for_status()
            report['models'] = [m['id'] for m in r.json()['data']]
            assert 'badminton_coach' in report['models']
            synthetic = root / 'synthetic.mp4'
            subprocess.run([ffmpeg_binary(), '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=duration=3:size=320x180:rate=12', '-c:v', 'libx264', '-y', str(synthetic)], check=True)
            with synthetic.open('rb') as file:
                r = client.post('/api/v1/files/', files={'file': ('synthetic.mp4', file, 'video/mp4')})
            r.raise_for_status()
            file_id = r.json()['id']
            report['upload'] = r.status_code
            r = client.post('/api/v1/chats/new', json={'chat': {'title': 'Isolated upload test', 'models': ['badminton_coach'], 'messages': [], 'history': {'messages': {}, 'currentId': None}}})
            r.raise_for_status()
            chat_id = r.json()['id']
            question = '실제 선수가 없는 합성 시험 영상이다. 선수 지정은 필요 없어. 도구로 처음 0~2초 장면을 초당 1장 확인하고 보이는 도형이나 색만 시각과 함께 설명해줘. 운동 코칭은 하지 말아줘.'
            messages = [{'role': 'user', 'content': question}]
            body = {'model': 'badminton_coach', 'messages': messages, 'chat_id': chat_id, 'id': str(uuid.uuid4()), 'stream': False,
                    'files': [{'id': file_id, 'type': 'file', 'name': 'synthetic.mp4', 'status': 'uploaded'}]}
            r = client.post('/api/chat/completions', json=body)
            if r.status_code != 200:
                raise RuntimeError(f'chat HTTP {r.status_code}: {r.text[:600]}')
            answer = r.json()['choices'][0]['message']['content']
            report['first_answer'] = answer
            assert '연결이 끊겼습니다' not in answer
            assert '첨부해 주세요' not in answer
            messages += [{'role': 'assistant', 'content': answer}, {'role': 'user', 'content': '같은 영상의 1~2초를 더 자세히 도구로 확인해줘. 합성 영상이며 운동 선수는 없어.'}]
            r = client.post('/api/chat/completions', json={'model': 'badminton_coach', 'messages': messages, 'chat_id': chat_id, 'id': str(uuid.uuid4()), 'stream': False})
            r.raise_for_status()
            report['followup_answer'] = r.json()['choices'][0]['message']['content']
            r = client.post('/api/chat/completions', json={'model': 'badminton_coach', 'messages': [{'role': 'user', 'content': '결과'}], 'chat_id': chat_id, 'id': str(uuid.uuid4()), 'stream': False})
            r.raise_for_status()
            assert r.json()['choices'][0]['message']['content'] == report['followup_answer']
            general_question = '스매시 스윙할때 이스턴 그립이 맞아? 아니면 주먹그립이 맞아?'
            r = client.post('/api/chat/completions', json={'model':'badminton_coach',
                'messages':messages+[{'role':'assistant','content':report['followup_answer']},{'role':'user','content':general_question}],
                'chat_id':chat_id,'id':str(uuid.uuid4()),'stream':False})
            r.raise_for_status()
            report['general_with_video'] = r.json()['choices'][0]['message']['content']
            r = client.post('/api/v1/chats/new', json={'chat':{'title':'No upload general question','models':['badminton_coach'],
                'messages':[],'history':{'messages':{},'currentId':None}}})
            r.raise_for_status()
            empty_chat_id=r.json()['id']
            r = client.post('/api/chat/completions', json={'model':'badminton_coach','messages':[{'role':'user','content':general_question}],
                'chat_id':empty_chat_id,'id':str(uuid.uuid4()),'stream':False})
            r.raise_for_status()
            report['general_without_video'] = r.json()['choices'][0]['message']['content']
            from badminton.store import Store
            store = Store(root / 'agent.db')
            report['run_states'] = [r['status'] for r in store.all('SELECT status FROM runs ORDER BY created')]
            assert report['run_states'] == ['completed']*4
            general_runs=store.all('SELECT id,response_kind FROM runs ORDER BY created DESC LIMIT 2')
            assert all(r['response_kind']=='general' for r in general_runs)
            for run in general_runs:
                names=[json.loads(e['data'])['name'] for e in store.all("SELECT data FROM events WHERE run_id=? AND kind='tool'",(run['id'],))]
                assert not set(names).intersection({'get_overview','find_segments','inspect_segment','inspect_trajectory','crop_region','set_player'})
                assert not store.all("SELECT id FROM calls WHERE run_id=? AND purpose NOT IN ('routing','agent')",(run['id'],))
            report['general_questions_no_video_tools']=True
            assert len(store.all('SELECT id FROM videos')) == 1
            report['tool_names'] = [json.loads(r['data'])['name'] for r in store.all("SELECT data FROM events WHERE kind='tool'")]
            assert report['tool_names'].count('inspect_segment') >= 2
            report['signup_after_admin'] = client.get('/api/config').json()['features']['enable_signup']
            assert report['signup_after_admin'] is False
            report['passed'] = True
    finally:
        import psutil
        for process in processes:
            try:
                parent = psutil.Process(process.pid)
                children = parent.children(recursive=True)
                for child in children:
                    child.terminate()
                parent.terminate()
                psutil.wait_procs(children + [parent], timeout=5)
            except psutil.NoSuchProcess:
                pass
        output = Path(os.environ['LOCALAPPDATA']) / 'BadmintonAgent/logs/webui-e2e.json'
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
