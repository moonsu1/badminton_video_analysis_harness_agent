"""Authenticated, bounded-chunk ingestion, durable offsets, idempotent completion."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter,Depends,Header,HTTPException,Request
from fastapi.responses import Response
from pydantic import BaseModel,Field

from .video import probe

CHUNK_SIZE=4*1024**2
EXTENSIONS={'.mp4','.mov','.m4v','.webm','.avi','.mkv'}


class LegacyUploadTrace:
    """Observe legacy multipart requests before FastAPI finishes parsing the body."""
    def __init__(self,app,uploads):self.app,self.uploads=app,uploads
    async def __call__(self,scope,receive,send):
        if scope['type']!='http' or scope.get('method')!='POST' or scope.get('path','').rstrip('/')!='/api/v1/files':
            return await self.app(scope,receive,send)
        key=str(uuid.uuid4());received=0;status=None;started=time.time()
        self.uploads.active_legacy+=1
        self.uploads.log('legacy_request_start',request_id=key)
        async def read():
            nonlocal received
            message=await receive()
            if message['type']=='http.request':received+=len(message.get('body',b''))
            elif message['type']=='http.disconnect':self.uploads.log('legacy_disconnect',request_id=key,received=received)
            return message
        async def write(message):
            nonlocal status
            if message['type']=='http.response.start':status=message['status']
            await send(message)
        try:await self.app(scope,read,write)
        finally:
            self.uploads.active_legacy-=1
            self.uploads.log('legacy_request_end',request_id=key,received=received,status=status,elapsed=round(time.time()-started,2))


class StartUpload(BaseModel):
    filename:str=Field(min_length=1,max_length=500)
    size:int=Field(gt=0)
    fingerprint:str=Field(pattern=r'^[a-f0-9]{64}$')
    content_type:str=Field(default='video/mp4',max_length=200)
    metadata:dict=Field(default_factory=dict)


class Uploads:
    def __init__(self,settings):
        self.settings=settings
        self.directory=settings.webui/'uploads'/'.incoming';self.directory.mkdir(parents=True,exist_ok=True)
        self.database=settings.root/'uploads.db';self.locks={}
        self.active_legacy=0
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS uploads(
                id TEXT PRIMARY KEY,user_id TEXT NOT NULL,fingerprint TEXT NOT NULL,filename TEXT NOT NULL,
                size INTEGER NOT NULL,offset INTEGER NOT NULL DEFAULT 0,content_type TEXT NOT NULL,metadata TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'receiving',file_id TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
                UNIQUE(user_id,fingerprint));''')
        self.cleanup()

    def cleanup(self):
        with self.connect() as db:
            for row in db.execute("SELECT * FROM uploads WHERE status!='completed' AND updated<?",(time.time()-86400,)).fetchall():
                path=self.part(row).resolve()
                if path.is_relative_to(self.directory.resolve()):path.unlink(missing_ok=True)
                db.execute('DELETE FROM uploads WHERE id=?',(row['id'],))

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.database,timeout=30);db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        finally:db.close()

    def log(self,kind,**data):
        with (self.settings.root/'logs/upload-events.jsonl').open('a',encoding='utf-8') as output:
            output.write(json.dumps({'time':time.time(),'kind':kind,**data},ensure_ascii=False)+'\n')

    def lock(self,key):return self.locks.setdefault(key,asyncio.Lock())

    def get(self,key,user):
        with self.connect() as db:row=db.execute('SELECT * FROM uploads WHERE id=? AND user_id=?',(key,user)).fetchone()
        if not row:raise HTTPException(404,'접근할 수 없는 업로드입니다.')
        return dict(row)

    def part(self,row):return self.directory/(row['id']+'.part')
    def destination(self,row):return self.settings.webui/'uploads'/(row['file_id']+Path(row['filename']).suffix.lower())

    def start(self,user,body):
        filename=Path(body.filename.replace('\\','/')).name
        if Path(filename).suffix.lower() not in EXTENSIONS:raise HTTPException(400,'MP4 또는 MOV 영상 파일을 선택해 주세요.')
        if body.size>self.settings.max_bytes:raise HTTPException(413,'파일당 최대4GiB입니다. 더 짧은 영상이나 작은 원본을 선택해 주세요.')
        if len(json.dumps(body.metadata))>16000:raise HTTPException(400,'첨부 정보가 너무 큽니다.')
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM uploads WHERE user_id=? AND fingerprint=?',(user,body.fingerprint)).fetchone()
            if old:
                if old['size']!=body.size or old['filename']!=filename:raise HTTPException(409,'다른 파일의 업로드 정보입니다. 파일을 다시 선택해 주세요.')
                part=self.part(old)
                if old['offset'] and not self.destination(old).exists() and (not part.exists() or part.stat().st_size<old['offset']):
                    db.execute("UPDATE uploads SET offset=0,status='receiving',updated=? WHERE id=?",(now,old['id']))
                    return dict(db.execute('SELECT * FROM uploads WHERE id=?',(old['id'],)).fetchone())
                return dict(old)
            if shutil.disk_usage(self.settings.root).free<body.size+1024**3:raise HTTPException(507,'영상을 저장할 디스크 공간이 부족합니다.')
            key=str(uuid.uuid4());file_id=str(uuid.uuid4())
            db.execute('INSERT INTO uploads(id,user_id,fingerprint,filename,size,content_type,metadata,file_id,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (key,user,body.fingerprint,filename,body.size,body.content_type,json.dumps(body.metadata,ensure_ascii=False),file_id,now,now))
            row=dict(db.execute('SELECT * FROM uploads WHERE id=?',(key,)).fetchone())
        self.log('start',upload_id=key,filename=filename,size=body.size)
        return row

    def append(self,key,user,offset,body,digest):
        row=self.get(key,user)
        if row['status']=='completed':return row
        if row['status'] not in {'receiving','failed'}:raise HTTPException(409,'완료 여부를 확인하는 중입니다. 잠시 후 상태를 다시 확인해 주세요.')
        if row['offset']!=offset:raise HTTPException(409,{'message':'저장된 위치에서 이어서 전송해 주세요.','offset':row['offset']})
        if not body or len(body)>CHUNK_SIZE or offset+len(body)>row['size']:raise HTTPException(400,'업로드 조각의 크기 또는 위치가 올바르지 않습니다.')
        if not re.fullmatch(r'[a-f0-9]{64}',digest) or hashlib.sha256(body).hexdigest()!=digest:raise HTTPException(400,'전송 조각의 검증값이 맞지 않습니다. 같은 부분을 다시 전송해 주세요.')
        path=self.part(row)
        if offset and (not path.exists() or path.stat().st_size<offset):raise HTTPException(409,'이어서 보낼 임시 파일이 없습니다. 업로드를 다시 시작해 주세요.')
        try:
            with path.open('r+b' if path.exists() else 'w+b') as output:
                output.seek(offset);output.truncate(offset);output.write(body);output.flush();os.fsync(output.fileno())
        except OSError as exc:
            self.log('write_error',upload_id=key,error_type=type(exc).__name__)
            raise HTTPException(507,'영상 조각을 저장하지 못했습니다. PC의 저장 공간을 확인해 주세요.') from exc
        with self.connect() as db:
            changed=db.execute("UPDATE uploads SET offset=?,status='receiving',updated=? WHERE id=? AND user_id=? AND offset=?",
                (offset+len(body),time.time(),key,user,offset)).rowcount
            if changed!=1:raise HTTPException(409,'다른 전송이 먼저 저장됐습니다. 저장된 위치를 다시 확인해 주세요.')
        if offset==0 or (offset//(64*1024**2))!=((offset+len(body))//(64*1024**2)) or offset+len(body)==row['size']:
            self.log('progress',upload_id=key,received=offset+len(body),size=row['size'])
        return self.get(key,user)

    def verify(self,row):
        path=self.part(row) if self.part(row).exists() else self.destination(row)
        if not path.exists() or path.stat().st_size!=row['size']:raise HTTPException(409,'아직 영상 전체가 저장되지 않았습니다. 이어서 전송해 주세요.')
        try:info=probe(path)
        except ValueError as exc:raise HTTPException(400,str(exc)) from exc
        if info['duration']>self.settings.max_seconds:raise HTTPException(400,'60분을 초과한 영상입니다. 나누어 올려 주세요.')
        digest=hashlib.sha256()
        with path.open('rb') as source:
            while block:=source.read(1024**2):digest.update(block)
        destination=self.destination(row)
        if path!=destination:os.replace(path,destination)
        return destination,digest.hexdigest()


def install_resumable_upload(app,settings,authenticate,get_file,register_file):
    uploads=Uploads(settings);router=APIRouter()
    app.state.badminton_uploads=uploads

    async def state(row,user):
        result={k:row[k] for k in ('id','size','offset','status')};result['chunk_size']=CHUNK_SIZE
        if row['status']=='completed':
            record=await get_file(row['file_id'],user.id)
            if not record:raise HTTPException(410,'이전 업로드 파일이 삭제됐습니다. 파일을 다시 선택해 주세요.')
            result['file']=record
        return result

    @router.get('/api/v1/badminton/upload-health')
    def health():return {'version':2,'chunk_size':CHUNK_SIZE,'max_bytes':settings.max_bytes,'resumable':True,'active_legacy_uploads':uploads.active_legacy}

    @router.get('/static/loader.js')
    def loader():
        original=settings.webui/'static/loader.js'
        text=original.read_text(encoding='utf-8') if original.exists() else ''
        text+='\n'+Path(__file__).with_name('upload_client.js').read_text(encoding='utf-8')
        return Response(text,media_type='application/javascript',headers={'Cache-Control':'no-store','X-Badminton-Upload-Version':'2'})

    @router.post('/api/v1/badminton/uploads')
    async def start(body:StartUpload,user=Depends(authenticate)):
        row=await asyncio.to_thread(uploads.start,user.id,body)
        if row['status']=='completed' and not await get_file(row['file_id'],user.id):
            # A deliberately deleted completed file is a new upload, not a resume.
            with uploads.connect() as db:db.execute('DELETE FROM uploads WHERE id=? AND user_id=?',(row['id'],user.id))
            row=await asyncio.to_thread(uploads.start,user.id,body)
        return await state(row,user)

    @router.get('/api/v1/badminton/uploads/{key}')
    async def status(key:str,user=Depends(authenticate)):
        return await state(uploads.get(key,user.id),user)

    @router.put('/api/v1/badminton/uploads/{key}')
    async def chunk(key:str,request:Request,upload_offset:int=Header(alias='Upload-Offset'),
                    checksum:str=Header(alias='X-Chunk-SHA256'),user=Depends(authenticate)):
        uploads.get(key,user.id)
        payload=bytearray()
        try:
            async for block in request.stream():
                if len(payload)+len(block)>CHUNK_SIZE:raise HTTPException(413,'전송 조각이 너무 큽니다.')
                payload.extend(block)
        except Exception as exc:
            uploads.log('receive_error',upload_id=key,error_type=type(exc).__name__);raise
        async with uploads.lock(key):
            row=await asyncio.to_thread(uploads.append,key,user.id,upload_offset,bytes(payload),checksum)
        return await state(row,user)

    @router.post('/api/v1/badminton/uploads/{key}/complete')
    async def complete(key:str,user=Depends(authenticate)):
        async with uploads.lock(key):
            row=uploads.get(key,user.id)
            if row['status']=='completed':return await state(row,user)
            if row['offset']!=row['size']:raise HTTPException(409,'아직 영상 전체가 전송되지 않았습니다.')
            # The file ID is stable across reconnects and crashes. Check an already
            # registered result before doing expensive verification again.
            record=await get_file(row['file_id'],user.id)
            if not record:
                with uploads.connect() as db:db.execute("UPDATE uploads SET status='verifying',updated=? WHERE id=?",(time.time(),key))
                try:
                    path,digest=await asyncio.to_thread(uploads.verify,row)
                    record=await register_file(user.id,row,path,digest)
                    if not record:raise RuntimeError('file_registration_failed')
                except Exception as exc:
                    with uploads.connect() as db:db.execute("UPDATE uploads SET status='failed',updated=? WHERE id=?",(time.time(),key))
                    uploads.log('completion_error',upload_id=key,error_type=type(exc).__name__)
                    if isinstance(exc,HTTPException):raise
                    raise HTTPException(500,'파일은 전송됐지만 등록을 완료하지 못했습니다. 같은 파일을 다시 선택해 주세요.') from exc
            with uploads.connect() as db:db.execute("UPDATE uploads SET status='completed',updated=? WHERE id=?",(time.time(),key))
            uploads.log('completed',upload_id=key,size=row['size'])
            return await state(uploads.get(key,user.id),user)

    @router.post('/api/v1/badminton/upload-events')
    async def client_event(request:Request,user=Depends(authenticate)):
        payload=bytearray()
        async for block in request.stream():
            if len(payload)+len(block)>4096:raise HTTPException(413,'진단 정보가 너무 큽니다.')
            payload.extend(block)
        try:body=json.loads(payload)
        except (ValueError,UnicodeDecodeError):raise HTTPException(400,'올바르지 않은 진단 정보입니다.')
        if not isinstance(body,dict):raise HTTPException(400,'올바르지 않은 진단 정보입니다.')
        uploads.log('client_error',phase=str(body.get('phase',''))[:40],upload_id=str(body.get('upload_id',''))[:36],
                    error=str(body.get('error',''))[:200],size=str(body.get('size',''))[:30])
        return {'saved':True}

    app.router.routes[0:0]=router.routes
    app.add_middleware(LegacyUploadTrace,uploads=uploads)
