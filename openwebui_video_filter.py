"""
title: Badminton video bridge
version: 3.0.0
"""
import os
from pathlib import Path

import httpx
from open_webui.models.files import Files
from open_webui.storage.provider import Storage


class Filter:
    def __init__(self):
        self.file_handler = True

    async def inlet(self, body: dict, __user__: dict = None, __metadata__: dict = None, __event_emitter__=None) -> dict:
        if (body.get('metadata') or {}).get('task'):
            return body
        metadata = body.get('metadata') or __metadata__ or {}
        refs = body.get('files') or metadata.get('files') or []
        if refs:
            if not __user__ or not __user__.get('id') or not metadata.get('chat_id'):
                raise ValueError('사용자와 대화 인증 정보를 확인할 수 없습니다.')
            root = Path(os.environ['BADMINTON_DATA_DIR'])
            headers = {'Authorization': 'Bearer ' + (root / 'service.key').read_text().strip()}
            current_message = metadata.get('user_message')
            current_refs = current_message.get('files', []) if isinstance(current_message, dict) else refs
            current_ids = {r.get('id') for r in current_refs if isinstance(r, dict)}
            if current_ids and __event_emitter__:
                await __event_emitter__({'type': 'status', 'data': {'description': '새 첨부 영상 연결 중', 'done': False}})
            selected = []
            async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
                for ref in refs:
                    file_id = ref.get('id') if isinstance(ref, dict) else None
                    if not file_id:
                        raise ValueError('지원되는 영상 파일을 첨부해 주세요.')
                    file = await Files.get_file_by_id_and_user_id(file_id, __user__['id'])
                    if not file:
                        # Deleted historical attachments may still occur in the chat's file references.
                        if await Files.get_file_by_id(file_id):
                            raise ValueError('본인이 올린 파일만 사용할 수 있습니다.')
                        continue
                    if not file.path:
                        raise ValueError('본인이 올린 파일만 사용할 수 있습니다.')
                    response = await client.post(os.getenv('BADMINTON_SERVICE_URL', 'http://127.0.0.1:8765') + '/internal/videos', headers=headers, json={
                        'source_path': Storage.get_file(file.path), 'source_id': file.id, 'filename': file.filename,
                        'user_id': __user__['id'], 'chat_id': metadata['chat_id']})
                    if response.is_error:
                        raise ValueError(response.json().get('detail', '영상 등록에 실패했습니다.'))
                    if file_id in current_ids:
                        selected.append(response.json()['id'])
                if selected:
                    response = await client.post(os.getenv('BADMINTON_SERVICE_URL', 'http://127.0.0.1:8765') + '/internal/context', headers=headers,
                        json={'user_id': __user__['id'], 'chat_id': metadata['chat_id'], 'video_ids': selected})
                    response.raise_for_status()
        # Registered videos are opened through tools; no unsupported video_url reaches the model.
        for message in body.get('messages', []):
            content = message.get('content')
            if isinstance(content, list):
                message['content'] = '\n'.join(str(p.get('text', '')) for p in content if isinstance(p, dict) and p.get('type') in {'text', 'input_text'})
        return body
