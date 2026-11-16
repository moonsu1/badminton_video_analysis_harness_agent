"""Publish the accepted cache, never replay its API calls. Services must be stopped."""
import argparse
import hashlib
import json
import shutil
import socket
import sqlite3
import time
import uuid
from pathlib import Path

from badminton.config import load_settings
from badminton.review_store import Reviews,VERSION
from badminton.store import Store,encode


def insert(db,table,row):
    columns=list(row)
    db.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',
        tuple(row[c] for c in columns))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True)
    parser.add_argument('--same-player-verified',action='store_true',help='Source audit confirmed different descriptions identify the same player')
    args=parser.parse_args()
    settings=load_settings().initialize();source=Path(args.source).resolve()
    assert source!=settings.root.resolve()
    for port in (settings.port,8080):
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(('127.0.0.1',port))!=0,'Stop the production services before publishing'
    with sqlite3.connect('file:'+str(settings.root/'agent.db')+'?mode=ro',uri=True) as db:
        assert not db.execute("SELECT id FROM runs WHERE status IN ('queued','preparing','analyzing') LIMIT 1").fetchone()
    backup=settings.root/'backups'/time.strftime('match-review-v1-%Y%m%d-%H%M%S')
    backup.mkdir(parents=True,exist_ok=False)
    for relative in ('agent.db','webui/webui.db'):
        with sqlite3.connect('file:'+str(settings.root/relative)+'?mode=ro',uri=True) as origin:
            with sqlite3.connect(backup/Path(relative).name) as destination:origin.backup(destination)
    production=Store(settings.root/'agent.db');tested=Store(source/'agent.db')
    review=tested.one('SELECT * FROM reviews ORDER BY created DESC LIMIT 1')
    assert review['status']=='completed'
    state=Reviews(settings,tested).status(review['id'])
    assert state['sampling_coverage']==1 and state['reserved_usd']==0 and state['spent_usd']<=3.5
    assert state['report_json'].get('identity_checks') and len(state['report_json']['scene_ids'])>=6
    video=production.video(review['video_id'],review['user_id'],review['chat_id'])
    profile=json.loads(review['profile'])
    assert video['sha256']==profile['video_sha256']
    normalized_player=False
    if video['player']!=profile['player']:
        assert args.same_player_verified,'Player descriptions differ; verify the actual player before publishing'
        profile['player']=video['player'];review['profile']=encode(profile);normalized_player=True
        review['cache_key']=hashlib.sha256(encode([VERSION,review['user_id'],review['chat_id'],video['sha256'],video['player'],settings.model]).encode()).hexdigest()
    canonical=uuid.uuid5(uuid.NAMESPACE_URL,'badminton-match-publish:'+review['id']).hex
    original=tested.run(review['run_id'])
    with production.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT id FROM reviews WHERE id=?',(review['id'],)).fetchone():
            original.update(id=canonical,request_id='match-review-recovery-v1-'+review['id'],
                question='경기 전체 리뷰 — 검증된 전체 시간대 판독 결과',status='completed',answer=review['report'],
                error=None,cancel=0,created=time.time(),updated=time.time(),response_kind='analysis',review_id=review['id'])
            insert(db,'runs',original)
            review['run_id']=canonical;review['updated']=time.time();insert(db,'reviews',review)
            for unit in tested.all('SELECT * FROM review_units WHERE review_id=?',(review['id'],)):insert(db,'review_units',unit)
            for call in tested.all('SELECT * FROM calls WHERE review_id=?',(review['id'],)):
                call['run_id']=canonical;insert(db,'calls',call)
    # Preserve the 2fps extraction cache under the production runtime directory.
    key=hashlib.sha256(encode([video['sha256'],'match-sample-v1',2,1280,90]).encode()).hexdigest()
    origin=(source/'cache'/key).resolve();destination=(settings.root/'cache'/key).resolve()
    assert origin.is_relative_to(source/'cache') and destination.is_relative_to(settings.root/'cache')
    frames=json.loads((origin/'manifest.json').read_text(encoding='utf-8'))
    destination.mkdir(parents=True,exist_ok=True)
    for frame in frames:
        old=Path(frame['path']).resolve();assert old.parent==origin
        new=destination/old.name
        shutil.copy2(old,new);frame['path']=str(new)
    (destination/'manifest.json').write_text(encode(frames),encoding='utf-8')
    production.event(canonical,'state',{'status':'completed','answer':review['report']})
    output={'review_id':review['id'],'run_id':canonical,'backup':str(backup),'frames':len(frames),
        'spent_usd':state['spent_usd'],'cached_without_replay':True,'player_description_normalized_after_source_audit':normalized_player}
    (settings.root/'logs/match-review-publish.json').write_text(encode(output),encoding='utf-8')
    print(encode(output))


if __name__=='__main__':main()
