"""
title: 배드민턴 코치
version: 4.3.0
"""
import hashlib
import json
import os
import re
from pathlib import Path

import httpx


class Pipe:
    async def pipe(self, body: dict, __user__: dict = None, __metadata__: dict = None, __event_emitter__=None, __task__=None):
        if __task__:
            yield json.dumps({'title': '배드민턴 영상 분석', 'tags': ['배드민턴']}, ensure_ascii=False)
            return
        metadata = __metadata__ or body.get('metadata') or {}
        if not __user__ or not __user__.get('id') or not metadata.get('chat_id'):
            yield '대화 정보를 확인할 수 없습니다. 새 채팅에서 다시 요청해 주세요.'
            return
        identity = {'user_id': __user__['id'], 'chat_id': metadata['chat_id']}
        last = next((m for m in reversed(body.get('messages', [])) if m.get('role') == 'user'), {})
        question = last.get('content') or '이 영상을 분석해 주세요.'
        if not isinstance(question, str):
            question = '\n'.join(p.get('text', '') for p in question if p.get('type') == 'text')
        root = Path(os.environ['BADMINTON_DATA_DIR'])
        headers = {'Authorization': 'Bearer ' + (root / 'service.key').read_text().strip()}
        base = os.getenv('BADMINTON_SERVICE_URL', 'http://127.0.0.1:8765')
        request_id = metadata.get('message_id') or hashlib.sha256(json.dumps(body.get('messages', []), ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        try:
            async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=90), trust_env=False) as client:
                latest = await client.get(base + '/internal/runs/latest', params=identity)
                latest.raise_for_status()
                latest = latest.json()
                command = question.strip().lower().replace(' ', '')
                raw_command = question.strip()
                if raw_command.startswith('리뷰계속'):
                    match=re.fullmatch(r'리뷰계속(?:\s+(\d+(?:\.\d{1,2})?))?',raw_command)
                    if not match:
                        yield '기존 예산으로 재개하려면 리뷰계속, 추가 예산1달러를 승인하려면 리뷰계속 1.00을 입력해 주세요.'
                        return
                    saved=await client.get(base+'/internal/reviews/latest',params=identity);saved.raise_for_status();saved=saved.json()
                    if not saved:
                        yield '이 대화에 저장된 전체 경기 리뷰가 없습니다. 경기 영상을 첨부하고 전체 리뷰를 요청해 주세요.'
                        return
                    resumed=await client.post(base+'/internal/reviews/'+saved['id']+'/resume',
                        json={**identity,'request_id':request_id,'additional_usd':float(match.group(1) or 0)})
                    resumed.raise_for_status();run=resumed.json()
                    if run['status'] in {'completed','cancelled'}:
                        yield run.get('answer') or saved.get('report') or '저장된 리뷰가 완료된 상태입니다.'
                        return
                    latest=run
                if command in {'영상목록', '/videos'} or raw_command.startswith('영상삭제') or raw_command.startswith('/delete'):
                    listing = await client.get(base + '/internal/videos', params=identity)
                    listing.raise_for_status()
                    items = listing.json()
                    if command in {'영상목록', '/videos'}:
                        yield '\n'.join(f"{i+1}. {v['filename']}" for i,v in enumerate(items)) or '이 대화에 등록된 영상이 없습니다.'
                        return
                    tail = raw_command.removeprefix('영상삭제').removeprefix('/delete').strip()
                    selected = items[0] if len(items) == 1 and not tail else None
                    if tail.isdigit() and 1 <= int(tail) <= len(items):
                        selected = items[int(tail)-1]
                    if not selected:
                        yield '먼저 `영상목록`으로 번호를 확인한 뒤 `영상삭제 1`처럼 입력해 주세요. 원본과 분석 기록이 삭제됩니다.'
                        return
                    deleted = await client.delete(base + '/internal/videos/' + selected['id'], params=identity)
                    if deleted.status_code == 409:
                        yield deleted.json()['detail']
                        return
                    deleted.raise_for_status()
                    source_id = deleted.json()['source_id']
                    from open_webui.models.files import Files
                    file = await Files.get_file_by_id_and_user_id(source_id, identity['user_id'])
                    if file and file.path:
                        upload = Path(file.path).resolve()
                        if upload.is_relative_to((root / 'webui/uploads').resolve()):
                            upload.unlink(missing_ok=True)
                            await Files.delete_file_by_id(source_id)
                    yield '영상 원본과 분석 저장 기록을 삭제했습니다. 이 채팅에 이미 표시된 답변까지 지우려면 채팅도 삭제해 주세요. 임시 추출 이미지는 캐시 정리 시 제거됩니다.'
                    return
                status_commands = {'/status', '/result', '상태', '진행상태', '결과', '결과보여줘', '분석결과보여줘', '어디까지됐어?', '어디까지됐어'}
                if command in {'/cancel', '취소', '분석취소'}:
                    saved=await client.get(base+'/internal/reviews/latest',params=identity)
                    saved.raise_for_status();saved=saved.json()
                    if saved and saved['status'] in {'running','queued'}:
                        latest={'id':saved['run_id']}
                    if latest:
                        r = await client.post(base + '/internal/runs/' + latest['id'] + '/cancel', json=identity)
                        r.raise_for_status()
                        yield '진행 중인 분석에 취소를 요청했습니다. 실행 중인 API 호출이 있으면 반환된 뒤 중단합니다.'
                    else:
                        yield '취소할 분석이 없습니다.'
                    return
                if command in status_commands:
                    saved=await client.get(base+'/internal/reviews/latest',params=identity)
                    saved.raise_for_status();saved=saved.json()
                    if saved:
                        if saved['status'] in {'running','queued'}:
                            yield saved.get('snapshot') or '전체 경기 리뷰를 준비하고 있습니다. 작업은 PC에서 계속됩니다. 잠시 후 결과를 다시 요청해 주세요.'
                        else:
                            yield saved.get('report') or '완료된 판독을 보관했습니다. 리뷰계속으로 재개할 수 있습니다.'
                        return
                    if not latest:
                        yield '아직 저장된 답변이 없습니다. 질문을 보내 주세요. 일반 코칭 질문은 영상 없이도 답할 수 있습니다.'
                        return
                    if latest['status'] == 'completed':
                        yield latest['answer']
                        return
                    if latest['status'] in {'failed', 'cancelled'}:
                        yield latest['error'] or '취소된 분석입니다.'
                        return
                    run = latest
                elif not raw_command.startswith('리뷰계속'):
                    r = await client.post(base + '/internal/runs', json={**identity, 'request_id': request_id, 'question': question})
                    r.raise_for_status()
                    run = r.json()
                if __event_emitter__:
                    await __event_emitter__({'type': 'status', 'data': {'description': '답변 준비 중', 'done': False}})
                async with client.stream('GET', base + '/internal/runs/' + run['id'] + '/events', params=identity) as stream:
                    stream.raise_for_status()
                    preview_sent=False
                    async for line in stream.aiter_lines():
                        if not line.startswith('data: '):
                            continue
                        event = json.loads(line[6:])
                        data = event['data']
                        if event['kind']=='review_preview' and not preview_sent:
                            yield data['text']+'\n\n---\n\n'
                            preview_sent=True
                        if event['kind'] == 'progress' and __event_emitter__:
                            await __event_emitter__({'type': 'status', 'data': {'description': data['message'], 'done': False}})
                        if event['kind'] == 'tool' and __event_emitter__:
                            label = {'get_video_info': '영상 정보 확인 중', 'get_overview': '전체 장면 확인 중', 'find_segments': '질문에 맞는 구간 찾는 중', 'inspect_segment': '원본 해상도 장면 확인 중', 'inspect_trajectory': '셔틀의 연속 위치 변화 확인 중', 'crop_region': '손·라켓 영역 확대 중', 'get_analysis_history': '이전 대화와 저장된 관찰 확인 중', 'reuse_observations': '기존 장면의 판독 근거 재사용 중', 'set_player': '분석할 선수 저장 중'}.get(data.get('name'), '질문 확인 중')
                            await __event_emitter__({'type': 'status', 'data': {'description': label, 'done': False}})
                result = await client.get(base + '/internal/runs/' + run['id'], params=identity)
                result.raise_for_status()
                result = result.json()
                if __event_emitter__:
                    review=result.get('review') or {}
                    coaching=(review.get('report_json') or {}).get('coaching_status')
                    label='판독 저장 · 코칭 추가 확인 필요' if coaching in {'incomplete','provisional'} else ('답변 완료' if result['status']=='completed' else '작업 종료')
                    if result.get('response_kind')=='clarification':label='분석 목적·대상 확인'
                    if result.get('response_kind')=='memory':label='개인 기억'
                    await __event_emitter__({'type': 'status', 'data': {'description':label,'done':True}})
                yield result['answer'] or result['error'] or '분석을 취소했습니다.'
        except httpx.HTTPError:
            yield '분석 서비스와 연결이 끊겼습니다. 시작된 작업은 PC에서 계속 진행될 수 있습니다. 연결이 돌아오면 이 채팅에 `결과`라고 입력해 주세요. PC에서는 상태확인.cmd를 사용할 수 있습니다.'
