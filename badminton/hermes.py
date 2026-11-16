"""Optional Hermes adapter. Native harness remains usable if Windows blocks Hermes."""
import json

import httpx
import keyring

from .agent import SYSTEM, ToolSession
from .store import encode


def hermes_run(settings, store, videos, run):
    key = keyring.get_password('BadmintonAgent', 'hermes_api_key')
    if not key:
        raise RuntimeError('Hermes API 인증이 등록되지 않았습니다. native 모드가 기본이며 Hermes 모드는 별도 연결 검증 후 사용합니다.')
    session = ToolSession(settings, store, videos, run, None)
    history = session.history()
    store.event(run['id'], 'progress', {'message': 'Hermes 에이전트로 분석 중'})
    # Per-run session key prevents Hermes memory from crossing chat boundaries.
    headers = {'Authorization': 'Bearer ' + key, 'X-Hermes-Session-Key': 'badminton:' + run['id']}
    with httpx.Client(timeout=3600, trust_env=False) as client:
        response = client.post(settings.hermes_url + '/v1/chat/completions', headers=headers, json={
            'model': 'hermes-agent', 'stream': False,
            'messages': [{'role': 'system', 'content': SYSTEM + '\n영상 도구는 badminton MCP 서버를 사용한다. 이 실행의 허용된 video_id만 사용하라.'},
                         {'role': 'user', 'content': encode({'context': history, 'allowed_video_ids': json.loads(run['video_ids']), 'question': run['question']})}]})
        response.raise_for_status()
        answer = response.json()['choices'][0]['message']['content']
    final = store.cached('hermes-final:' + run['id'])
    if final:
        return final['answer']
    raise RuntimeError('Hermes의 답변 종류와 근거가 확정되지 않았습니다. complete_analysis 연결을 확인해 주세요. 다른 harness에서 자동 재실행하지 않았습니다.')
