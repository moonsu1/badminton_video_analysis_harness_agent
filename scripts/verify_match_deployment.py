"""Read-only deployment checks and a free saved-result request through the Pipe."""
import asyncio
import json
import os
import sqlite3
import time

import httpx

from badminton.config import load_settings
from badminton.store import Store,encode


async def main():
    settings=load_settings().initialize();store=Store(settings.root/'agent.db')
    review=store.one('SELECT * FROM reviews ORDER BY updated DESC LIMIT 1')
    assert review and review['status']=='completed'
    report=json.loads(review['report_json'])
    assert report.get('report_version')=='five-part-v1'
    assert report['level_assessment']['status']=='estimated'
    assert all('### '+str(n)+'.' in review['report'] for n in range(1,6))
    assert report['analysis_profile']['analysis_focus']=='gameplay'
    before=store.one('SELECT COUNT(*) AS n FROM calls')['n']
    os.environ['BADMINTON_DATA_DIR']=str(settings.root)
    os.environ['BADMINTON_SERVICE_URL']=f'http://127.0.0.1:{settings.port}'
    from openwebui_coach_pipe import Pipe
    answer=''.join([part async for part in Pipe().pipe({'messages':[{'role':'user','content':'결과'}]},
        __user__={'id':review['user_id']},__metadata__={'chat_id':review['chat_id']})])
    assert answer==review['report'] and store.one('SELECT COUNT(*) AS n FROM calls')['n']==before
    url=(settings.root/'remote-url.txt').read_text().strip()
    with httpx.Client(timeout=30,trust_env=False) as client:
        health=client.get(f'http://127.0.0.1:{settings.port}/health').json()
        assert health['model']=='gpt-6-luna' and health['pricing_version']==2
        assert health['reasoning_effort']=='none' and health['service_tier']=='default'
        assert health['match_review_version']==1 and health['scene_coaching_version']==4 and health['review_max_usd']==3.5
        assert health['analysis_intake_version']==1 and health['report_format']=='five-part-v1'
        assert health['memory_version']==1 and health['memory_scope']=='user'
        assert client.get('http://127.0.0.1:8080/health').status_code==200
        upload=client.get(url+'/api/v1/badminton/upload-health').json()
        assert upload['version']==2 and upload['resumable'] and upload['chunk_size']==4*1024**2
        loader=client.get(url+'/static/loader.js')
        assert loader.status_code==200 and loader.headers['x-badminton-upload-version']=='2'
        assert '__badmintonUploadVersion = 2' in loader.text and loader.headers['cache-control']=='no-store'
        assert client.post(url+'/api/v1/badminton/uploads',json={}).status_code==401
        assert client.get(url).status_code==200
        assert client.get(url+'/api/models').status_code==401
    import websockets
    async with websockets.connect(url.replace('https://','wss://')+'/ws/socket.io/?EIO=4&transport=websocket',
        origin=url,open_timeout=20) as ws:
        packet=await asyncio.wait_for(ws.recv(),10)
        assert packet.startswith('0') and 'sid' in packet
    with sqlite3.connect(settings.webui/'webui.db') as db:
        row=db.execute("SELECT content FROM function WHERE id='badminton_coach'").fetchone()
        assert row and 'version: 4.3.0' in row[0] and '리뷰계속' in row[0]
        metadata=json.loads(db.execute("SELECT meta FROM model WHERE id='badminton_coach'").fetchone()[0])
        assert 'GPT-6 Luna' in metadata['description']
    result={'passed':True,'time':time.time(),'health':health,'saved_result_no_new_api_calls':True,
        'remote_https':200,'anonymous_models':401,'websocket':True,'pipe_version':'4.3.0','review_id':review['id'],
        'five_part_report':True,'level_estimate':True,'purpose_remembered':True,'upload':upload}
    (settings.root/'logs/match-review-deployment.json').write_text(encode(result),encoding='utf-8')
    print(encode(result))


if __name__=='__main__':asyncio.run(main())
