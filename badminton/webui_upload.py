"""Bounded-memory video ingestion for the dedicated Open WebUI instance."""
import asyncio
import hashlib
import errno
import json
import os
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .config import load_settings
from .video import probe


def install_video_upload(app):
    from open_webui.models.files import Files, FileForm
    from open_webui.utils.auth import get_verified_user

    router = APIRouter()

    @router.post('/api/v1/files/')
    async def upload(file: UploadFile = File(...), metadata: str | None = Form(None), user=Depends(get_verified_user)):
        settings = load_settings().initialize()
        filename = Path(file.filename or 'video').name
        extension = Path(filename).suffix.lower()
        if extension not in {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}:
            raise HTTPException(400, '이 채팅에는 MP4 또는 MOV 경기 영상을 첨부해 주세요.')
        if file.size is not None and file.size > settings.max_bytes:
            raise HTTPException(413, '영상이 업로드 크기 한도를 초과했습니다. 해상도나 길이를 줄여 주세요.')
        if shutil.disk_usage(settings.root).free < (file.size or 0) + 1024**3:
            raise HTTPException(507, '영상을 저장할 디스크 공간이 부족합니다.')
        try:
            extra = json.loads(metadata) if metadata else {}
            if not isinstance(extra, dict):
                raise ValueError()
        except ValueError:
            raise HTTPException(400, '첨부 정보가 올바르지 않습니다.')
        file_id = str(uuid.uuid4())
        path = settings.webui / 'uploads' / (file_id + extension)
        partial = path.with_suffix('.partial')
        stop_copy = threading.Event()

        def save():
            digest, size = hashlib.sha256(), 0
            file.file.seek(0)
            with partial.open('wb') as output:
                while block := file.file.read(1024**2):
                    if stop_copy.is_set():
                        raise RuntimeError('upload interrupted')
                    size += len(block)
                    if size > settings.max_bytes:
                        raise HTTPException(413, '영상이 업로드 크기 한도를 초과했습니다.')
                    digest.update(block)
                    output.write(block)
            if not size:
                raise HTTPException(400, '빈 영상 파일입니다.')
            info = probe(partial)
            if info['duration'] > settings.max_seconds:
                raise HTTPException(400, '60분을 초과한 영상입니다. 나누어 올려 주세요.')
            os.replace(partial, path)
            return size, digest.hexdigest()

        try:
            copy_task = asyncio.create_task(asyncio.to_thread(save))
            try:
                size, digest = await asyncio.shield(copy_task)
            except asyncio.CancelledError:
                stop_copy.set()
                try:
                    await asyncio.shield(copy_task)
                except Exception:
                    pass
                raise
            record = await Files.insert_new_file(user.id, FileForm(id=file_id, filename=filename, path=str(path),
                data={'status': 'completed'}, meta={'name': filename, 'content_type': file.content_type or 'video/mp4', 'size': size, 'file_hash': digest, 'data': extra}))
            if not record:
                raise RuntimeError('file registration failed')
            return record.model_dump()
        except ValueError as exc:
            partial.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise HTTPException(400, str(exc))
        except OSError as exc:
            partial.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            if exc.errno in {errno.ENOSPC, 112}:
                raise HTTPException(507, '영상 저장 공간이 부족합니다. 여유 공간을 확보해 주세요.') from exc
            raise HTTPException(500, '영상을 저장하지 못했습니다. 저장 폴더의 상태를 확인해 주세요.') from exc
        except BaseException:
            partial.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            raise

    # FastAPI selects the first matching route. The rest of OWUI's file API remains intact.
    app.router.routes[0:0] = router.routes
    from .uploads import install_resumable_upload
    async def get_file(file_id,user_id):
        row=await Files.get_file_by_id_and_user_id(file_id,user_id)
        return row.model_dump() if row else None
    async def register_file(user_id,row,path,digest):
        record=await Files.insert_new_file(user_id,FileForm(id=row['file_id'],filename=row['filename'],path=str(path),
            data={'status':'completed'},meta={'name':row['filename'],'content_type':row['content_type'],
                'size':row['size'],'file_hash':digest,'data':json.loads(row['metadata'])}))
        return record.model_dump() if record else None
    install_resumable_upload(app,load_settings().initialize(),get_verified_user,get_file,register_file)
