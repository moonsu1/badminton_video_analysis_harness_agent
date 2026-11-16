import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI,Header,HTTPException
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from badminton.uploads import CHUNK_SIZE,Uploads,install_resumable_upload


@pytest.fixture
def upload_app(system):
    settings,_,_=system;files={};registrations=[]
    async def auth(authorization:str=Header(default='')):
        if authorization not in {'Bearer user1','Bearer user2'}:raise HTTPException(401,'인증이 필요합니다.')
        return SimpleNamespace(id=authorization.split()[1])
    async def get_file(key,user):
        value=files.get(key)
        return value if value and value['user_id']==user else None
    async def register(user,row,path,digest):
        registrations.append(row['file_id'])
        files[row['file_id']]={'id':row['file_id'],'filename':row['filename'],'path':str(path),
            'user_id':user,'data':{'status':'completed'},'meta':{'size':row['size'],'file_hash':digest}}
        return files[row['file_id']]
    app=FastAPI();install_resumable_upload(app,settings,auth,get_file,register)
    return app,files,registrations


def begin(client,payload,filename='test.mp4',fingerprint=None):
    response=client.post('/api/v1/badminton/uploads',json={'filename':filename,'size':len(payload),
        'fingerprint':fingerprint or hashlib.sha256(payload).hexdigest()})
    return response


def send(client,key,offset,payload,checksum=None):
    return client.put('/api/v1/badminton/uploads/'+key,content=payload,
        headers={'Content-Type':'application/octet-stream','Upload-Offset':str(offset),
                 'X-Chunk-SHA256':checksum or hashlib.sha256(payload).hexdigest()})


def test_upload_auth_ownership_and_no_registration_until_complete(upload_app,clip):
    app,files,_=upload_app;payload=Path(clip['path']).read_bytes()
    with TestClient(app) as client:
        assert begin(client,payload).status_code==401
        client.headers['Authorization']='Bearer user1';row=begin(client,payload).json();key=row['id']
        assert not files and send(client,key,0,payload[:100]).status_code==200
        assert client.post('/api/v1/badminton/uploads/'+key+'/complete').status_code==409
        client.headers['Authorization']='Bearer user2'
        assert client.get('/api/v1/badminton/uploads/'+key).status_code==404
        assert send(client,key,100,payload[100:200]).status_code==404
        assert client.post('/api/v1/badminton/uploads/'+key+'/complete').status_code==404


def test_resume_after_restart_response_loss_and_idempotent_completion(system,upload_app,clip):
    settings,_,_=system;app,files,registrations=upload_app;payload=Path(clip['path']).read_bytes();split=len(payload)//2
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer user1';row=begin(client,payload).json();key=row['id']
        assert send(client,key,0,payload[:split]).json()['offset']==split
        assert Uploads(settings).get(key,'user1')['offset']==split
        # Retry after an accepted response is lost must not append duplicate bytes.
        assert send(client,key,0,payload[:split]).status_code==409
        assert begin(client,payload).json()['id']==key
        assert send(client,key,split,payload[split:]).json()['offset']==len(payload)
        first=client.post('/api/v1/badminton/uploads/'+key+'/complete').json()
        second=client.post('/api/v1/badminton/uploads/'+key+'/complete').json()
        assert first['file']['id']==second['file']['id'] and len(registrations)==1
        assert Path(first['file']['path']).read_bytes()==payload
        assert first['file']['meta']['file_hash']==hashlib.sha256(payload).hexdigest()


def test_checksum_and_oversize_chunks_never_advance_durable_offset(upload_app):
    app,_,_=upload_app
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer user1';row=begin(client,b'x'*(CHUNK_SIZE+1)).json();key=row['id']
        assert send(client,key,0,b'abc',checksum='0'*64).status_code==400
        assert send(client,key,0,b'x'*(CHUNK_SIZE+1)).status_code==413
        assert client.get('/api/v1/badminton/uploads/'+key).json()['offset']==0


def test_corrupt_video_is_not_registered_and_missing_partial_can_restart(system,upload_app):
    settings,_,_=system;app,files,_=upload_app
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer user1';payload=b'not-a-video';row=begin(client,payload).json();key=row['id']
        send(client,key,0,payload)
        assert client.post('/api/v1/badminton/uploads/'+key+'/complete').status_code==400
        assert files=={}
        upload=Uploads(settings);upload.part(upload.get(key,'user1')).unlink()
        restarted=begin(client,payload).json()
        assert restarted['id']==key and restarted['offset']==0


def test_file_limits_and_loader_preserves_theme_script(system,upload_app):
    settings,_,_=system;app,_,_=upload_app
    directory=settings.webui/'static';directory.mkdir();(directory/'loader.js').write_text('window.originalTheme=true;',encoding='utf-8')
    with TestClient(app) as client:
        client.headers['Authorization']='Bearer user1'
        assert begin(client,b'x',filename='bad.exe').status_code==400
        assert client.post('/api/v1/badminton/uploads',json={'filename':'big.mp4','size':settings.max_bytes+1,'fingerprint':'a'*64}).status_code==413
        result=client.get('/static/loader.js')
        assert result.headers['x-badminton-upload-version']=='2'
        assert 'window.originalTheme=true;' in result.text and '__badmintonUploadVersion' in result.text
        assert result.headers['cache-control']=='no-store'


def test_partial_network_chunk_does_not_commit_offset(upload_app):
    app,files,_=upload_app
    async def interrupted():
        yield b'only-part-of-the-request'
        raise ClientDisconnect()
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',
                                     headers={'Authorization':'Bearer user1'}) as client:
            row=(await client.post('/api/v1/badminton/uploads',json={'filename':'partial.mp4','size':100,
                'fingerprint':'c'*64})).json()
            with pytest.raises(ClientDisconnect):
                await client.put('/api/v1/badminton/uploads/'+row['id'],content=interrupted(),
                    headers={'Upload-Offset':'0','X-Chunk-SHA256':'d'*64})
            state=(await client.get('/api/v1/badminton/uploads/'+row['id'])).json()
            assert state['offset']==0 and not files
    asyncio.run(scenario())


def test_upload_diagnostics_are_authenticated_and_bounded(upload_app):
    app,_,_=upload_app
    with TestClient(app) as client:
        url='/api/v1/badminton/upload-events'
        assert client.post(url,json={}).status_code==401
        client.headers['Authorization']='Bearer user1'
        assert client.post(url,json={'phase':'chunk','error':'offline'}).status_code==200
        assert client.post(url,json={'error':'x'*4096}).status_code==413
        assert client.post(url,json=['invalid']).status_code==400
