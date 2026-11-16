"""1.55 GiB real Chromium + Open WebUI upload, dropped response and page reload.

Isolated app/account, no model generation. Uses the signed installed Chrome.
"""
import argparse
import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import websockets

from badminton.bootstrap import PROJECT
from badminton.config import load_settings
from badminton.store import encode
from badminton.video import ffmpeg_binary,probe


def wait_ready(url,process):
    for _ in range(120):
        if process.poll() is not None:raise RuntimeError('Isolated test server stopped; inspect its test log.')
        try:
            if httpx.get(url,timeout=2,trust_env=False).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(1)
    raise RuntimeError('Isolated test server did not become ready.')


class CDP:
    def __init__(self,ws):self.ws,self.counter=ws,0
    async def command(self,method,params=None):
        self.counter+=1;key=self.counter
        await self.ws.send(encode({'id':key,'method':method,'params':params or {}}))
        while True:
            message=json.loads(await self.ws.recv())
            if message.get('id')==key:
                if 'error' in message:raise RuntimeError('Browser command failed: '+str(message['error']))
                return message.get('result',{})
    async def evaluate(self,expression):
        value=await self.command('Runtime.evaluate',{'expression':expression,'returnByValue':True})
        if value.get('exceptionDetails'):raise RuntimeError('Browser script failed: '+value['exceptionDetails'].get('text',''))
        return value.get('result',{}).get('value')


async def browser_upload(websocket,url,path,token,root):
    async with websockets.connect(websocket,max_size=20*1024**2) as ws:
        cdp=CDP(ws);await cdp.command('Page.enable');await cdp.command('Runtime.enable')
        hook="""(() => {const f=window.fetch.bind(window);let n=0;window.fetch=async function(input,options={}) {
          const u=String(input),r=await f(input,options);
          if(u.endsWith('/api/v1/badminton/uploads')&&options.method==='POST') {const d=await r.clone().json();window.testResumeOffset=d.offset;}
          if(u.includes('/api/v1/badminton/uploads/')&&options.method==='PUT'&&++n===5&&!localStorage.testDroppedResponse) {
            localStorage.testDroppedResponse='1';throw new TypeError('simulated lost response after server commit');}
          return r;};})();"""
        await cdp.command('Page.addScriptToEvaluateOnNewDocument',{'source':hook})
        await cdp.command('Page.navigate',{'url':url})
        for _ in range(120):
            if await cdp.evaluate('window.__badmintonUploadVersion===2'):break
            await asyncio.sleep(.5)
        else:raise RuntimeError('Resumable uploader was not loaded by the real browser.')
        await cdp.evaluate('localStorage.setItem("token",'+json.dumps(token)+');true')
        async def begin():
            await cdp.evaluate("document.getElementById('large-upload-test')?.remove();var e=document.createElement('input');e.type='file';e.id='large-upload-test';document.body.appendChild(e);true")
            document=await cdp.command('DOM.getDocument')
            node=await cdp.command('DOM.querySelector',{'nodeId':document['root']['nodeId'],'selector':'#large-upload-test'})
            await cdp.command('DOM.setFileInputFiles',{'nodeId':node['nodeId'],'files':[str(path)]})
            await cdp.evaluate("window.testUpload={status:'running'};(async()=>{try{const b=new FormData();b.append('file',document.getElementById('large-upload-test').files[0]);const r=await fetch('/api/v1/files/?process=true',{method:'POST',headers:{authorization:'Bearer '+localStorage.token},body:b});window.testUpload={status:r.ok?'completed':'error',file:await r.json()};}catch(e){window.testUpload={status:'error',error:e.message}}})();true")
        await begin();reloaded=False;last=-1;resume_offset=0
        for _ in range(1200):
            result=await cdp.evaluate('window.testUpload || null')
            if result and result['status']=='error':raise RuntimeError('Browser upload failed: '+str(result.get('error') or result.get('file')))
            if result and result['status']=='completed':
                dropped=await cdp.evaluate("localStorage.testDroppedResponse==='1'")
                assert dropped and reloaded and resume_offset>0
                return result['file'],{'lost_response_recovered':dropped,'page_reload_resumed':reloaded,'resume_offset':resume_offset}
            if (root/'uploads.db').exists():
                with sqlite3.connect(root/'uploads.db') as db:row=db.execute('SELECT offset,size FROM uploads LIMIT 1').fetchone()
                if row:
                    current,total=row;percent=int(current/total*100)
                    if percent//10!=last:
                        print(encode({'upload_percent':percent,'bytes_saved':current,'browser_reload_done':reloaded}),flush=True);last=percent//10
                    if current>=64*1024**2 and not reloaded:
                        await cdp.command('Page.reload');reloaded=True
                        for _ in range(120):
                            if await cdp.evaluate("window.__badmintonUploadVersion===2 && document.readyState==='complete'"):break
                            await asyncio.sleep(.25)
                        await begin()
                    if reloaded:
                        value=await cdp.evaluate('window.testResumeOffset || 0')
                        resume_offset=max(resume_offset,value or 0)
            await asyncio.sleep(.5)
        raise RuntimeError('Browser upload test exceeded ten minutes.')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--gib',type=float,default=1.55);args=parser.parse_args()
    production=load_settings();root=Path(tempfile.mkdtemp(prefix='badminton-large-upload-'))
    root.joinpath('settings.json').write_text(encode({'port':8766,'model':'gpt-6-luna'}),encoding='utf-8')
    env={**os.environ,'BADMINTON_DATA_DIR':str(root),'BADMINTON_WEBUI_PORT':'8081','PYTHONUTF8':'1'}
    report={'data':str(root),'passed':False,'model_generation_calls':0,'gib':args.gib};processes=[]
    started=time.monotonic()
    try:
        path=root/'large-upload.mp4'
        subprocess.run([ffmpeg_binary(),'-v','error','-f','lavfi','-i','color=blue:s=640x360:r=30:d=2',
            '-c:v','libx264','-movflags','+faststart','-y',str(path)],check=True)
        target=int(args.gib*1024**3);padding=target-path.stat().st_size
        with path.open('ab') as output:output.write(struct.pack('>I4s',padding,b'free'))
        with path.open('r+b') as output:output.seek(target-1);output.write(b'\0')
        assert probe(path)['duration']==2
        with path.open('rb') as source:expected=hashlib.file_digest(source,'sha256').hexdigest()
        with (root/'setup.log').open('w',encoding='utf-8') as log:
            subprocess.run([sys.executable,'-m','badminton.bootstrap'],cwd=PROJECT,env=env,stdout=log,stderr=log,check=True)
        for module,port in [('badminton.service',8766),('badminton.webui',8081)]:
            with (root/(module+'.log')).open('w',encoding='utf-8') as log:
                process=subprocess.Popen([sys.executable,'-m',module],cwd=PROJECT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process);wait_ready(f'http://127.0.0.1:{port}/health',process)
        with httpx.Client(base_url='http://127.0.0.1:8081',timeout=300,trust_env=False) as client:
            signup=client.post('/api/v1/auths/signup',json={'name':'Isolated upload test','email':'upload@example.invalid','password':secrets.token_urlsafe(30)});signup.raise_for_status()
            token=signup.json()['token'];client.headers['Authorization']='Bearer '+token
            chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
            with (root/'browser.log').open('w',encoding='utf-8') as log:
                process=subprocess.Popen([str(chrome),'--headless=new','--no-first-run','--disable-default-apps',
                    '--remote-debugging-port=9229','--user-data-dir='+str(root/'browser'),'about:blank'],
                    stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            processes.append(process);wait_ready('http://127.0.0.1:9229/json/version',process)
            with httpx.Client(trust_env=False) as debug:
                pages=debug.get('http://127.0.0.1:9229/json').json()
            page=next(p for p in pages if p['type']=='page')
            file,browser_result=asyncio.run(browser_upload(page['webSocketDebuggerUrl'],'http://127.0.0.1:8081',path,token,root))
            assert file['meta']['size']==target and file['meta']['file_hash']==expected
            with Path(file['path']).open('rb') as saved:assert hashlib.file_digest(saved,'sha256').hexdigest()==expected
            chat=client.post('/api/v1/chats/new',json={'chat':{'title':'Large upload test','models':['badminton_coach'],
                'messages':[],'history':{'messages':{},'currentId':None}}});chat.raise_for_status();chat_id=chat.json()['id']
            response=client.post('/api/chat/completions',json={'model':'badminton_coach','messages':[{'role':'user','content':'기억목록'}],
                'chat_id':chat_id,'id':str(uuid.uuid4()),'stream':False,
                'files':[{'id':file['id'],'type':'file','name':file['filename'],'status':'uploaded'}]})
            response.raise_for_status()
            with sqlite3.connect(root/'agent.db') as db:
                assert db.execute('SELECT COUNT(*) FROM videos').fetchone()[0]==1
                assert db.execute('SELECT sha256 FROM videos').fetchone()[0]==expected
                assert db.execute('SELECT COUNT(*) FROM calls').fetchone()[0]==0
            report.update(passed=True,bytes=target,sha256=expected,upload_registered=True,chat_attachment_connected=True,
                browser='installed_signed_chrome_headless',**browser_result)
    finally:
        import psutil
        for process in reversed(processes):
            try:
                parent=psutil.Process(process.pid);children=parent.children(recursive=True)
                for child in reversed(children):child.terminate()
                parent.terminate();psutil.wait_procs(children+[parent],timeout=5)
            except psutil.NoSuchProcess:pass
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        (production.root/'logs/large-upload-smoke.json').write_text(encode(report),encoding='utf-8')
        print(encode(report),flush=True)


if __name__=='__main__':main()
