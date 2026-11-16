"""Offline, no-API publication of the existing review's saved five-part draft."""
import json
import sqlite3
import time
from datetime import datetime

from badminton.config import load_settings
from badminton.store import Store,encode
from badminton.review import all_observations,validate_report,render_report
from badminton.review_store import Reviews
from badminton.coaching import saved_points,VERSION


def main():
    settings=load_settings().initialize()
    import psutil
    for process in psutil.process_iter(['cmdline']):
        args=process.info['cmdline'] or []
        if any(arg in {'badminton.service','badminton.webui'} for arg in args):
            raise RuntimeError('Stop both application services before publishing.')
    backup=settings.root/'backups'/('five-part-intake-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    for source,name in [(settings.root/'agent.db','agent.db'),(settings.webui/'webui.db','webui.db')]:
        with sqlite3.connect(source) as origin,sqlite3.connect(backup/name) as destination:origin.backup(destination)
    store=Store(settings.root/'agent.db');rid='8d83172d9a254c3db554539ec6e839c0'
    assert not store.one("SELECT id FROM runs WHERE status IN ('queued','preparing','analyzing')")
    review=store.one('SELECT * FROM reviews WHERE id=?',(rid,));old=json.loads(review['report_json'])
    calls_before=store.one('SELECT COUNT(*) n FROM calls')['n']
    profile=json.loads(review['profile'])
    profile['analysis_profile']={'video_kind':'match','analysis_focus':'gameplay','techniques':[]}
    positions={r['position'] for r in store.all("SELECT position FROM review_units WHERE review_id=? AND kind='recheck' AND status='completed'",(rid,))}
    observations=[o for o in all_observations(store,rid) if round(o['unit_start']/18) in positions]
    draft=json.loads((settings.root/'logs/five-part-report-draft.json').read_text(encoding='utf-8'))
    refs=lambda *codes:[rid+':'+code for code in codes]
    # The raw draft overstated causal effects and used post-rally walking as recovery
    # evidence. Editorial corrections below use only the saved observations/coaching.
    draft['overview']={'summary':'이번 경기에서는 몸 앞쪽의 짧은 처리와 큰 스텝을 동반한 스윙이 함께 나타납니다. 06:25–06:29와 15:02–15:03에는 앞으로 라켓을 내보낸 뒤 자세를 정리하는 모습이 있고, 04:19–04:23과 10:12–10:15에는 큰 이동과 몸통 회전이 연결됩니다. 전위 처리, 큰 이동 뒤의 균형, 다음 타구를 위한 준비가 이번 영상의 핵심입니다. 아래에서는 유지할 행동과 다음 경기에서 시험할 조정을 나눠 제안합니다.',
        'evidence_ids':refs('S0021:O2','S0050:O0','S0014:O2','S0034:O0')}
    draft['tendencies']=[{'text':'서로 다른 두 전방 처리 장면에서 라켓을 앞으로 내보낸 뒤 자세를 다시 정리하는 연결이 보입니다.',
        'evidence_ids':refs('S0021:O2','S0050:O0'),'counterexample_ids':[],
        'drill':'짧은 공 처리 뒤 다음 공을 받을 준비까지 한 묶음으로 연습하세요.'}]
    draft['strengths'][0]['text']='06:25–06:29와 15:02–15:03의 짧은 처리에서 라켓을 몸 앞쪽으로 내보내고 자세를 정리하는 연결을 유지했습니다.'
    draft['strengths'][1]['text']='큰 스텝과 몸통 회전, 위쪽 또는 앞쪽으로 이어지는 스윙이 여러 장면에 나타납니다. 작은 처리에만 머물지 않고 동작 범위를 넓혀 대응하는 모습입니다.'
    level=draft['level_assessment']
    level['summary']='보이는 준비 자세, 전방 처리와 스윙 연결을 기준으로 초중급~중급 범위의 잠정 평가입니다. 기본 동작을 이어가는 사례는 확인됩니다. 큰 이동 뒤의 중심과 재준비는 아래 두 장면에서 조정 효과를 비교할 우선 항목입니다. 이를 이미 입증된 약점이나 경기 결과의 원인으로 단정하지는 않습니다.'
    level['criteria']=[
        {'name':'스윙 연결','assessment':'두 전방 처리에서 라켓을 앞으로 보내고 자세를 다시 정리합니다. 셔틀 성공률이나 코스 정확도를 측정한 평가는 아닙니다.','evidence_ids':refs('S0021:O2','S0050:O0')},
        {'name':'이동과 중심','assessment':'큰 스텝과 몸통 방향 전환이 연결됩니다. 장면 코칭에서 제안한 짧은 첫발이 다음 준비에 유리한지는 다음 영상으로 비교할 항목입니다.','evidence_ids':refs('S0014:O2','S0034:O0')},
        {'name':'재준비','assessment':'몸 앞에서 스윙을 마치거나 라켓과 무릎을 준비하는 장면이 있습니다. 랠리 종료 뒤 걷는 구간은 복귀 지연의 근거로 사용하지 않았습니다.','evidence_ids':refs('S0014:O2','S0034:O0')},
        {'name':'위치 전환','assessment':'전방 처리와 후방 스윙 뒤 위치를 정리하는 모습이 있습니다. 상대 타구와 점수 흐름을 충분히 확인하지 못해 전술 선택의 성공률까지 평가하지는 않습니다.','evidence_ids':refs('S0021:O2','S0050:O2')}]
    level['evidence_ids']=list(dict.fromkeys(i for c in level['criteria'] for i in c['evidence_ids']))
    report=validate_report(draft,observations,profile['duration'])
    assert report['level_assessment']['status']=='estimated'
    for key in ['identity_checks','quality_recheck_units','source_audit']:
        if key in old:report[key]=old[key]
    report.update(scene_coaching=saved_points(store,rid),coaching_status='actionable',coaching_version=VERSION,report_status='ready')
    report['report_edit']={'version':'five-part-v1','based_on':'saved observations and scene coaching',
        'new_video_calls':0,'source_language_tightened':True,'formal_level_validation':False}
    status=Reviews(settings,store).status(rid)
    fine_count=sum(json.loads(r['result']).get('frames',0) for r in store.all(
        "SELECT result FROM review_units WHERE review_id=? AND kind='refine' AND result IS NOT NULL",(rid,)))
    video=store.video(review['video_id'],review['user_id'],review['chat_id'])
    text=render_report(video,status,report,observations,fine_count)
    assert all('### '+str(n)+'.' in text for n in range(1,6))
    assert '초중급~중급' in text and '잠정 평가' in text and len(report['scene_coaching'])>=5
    with store.connect() as db:
        db.execute('UPDATE reviews SET profile=?,report=?,report_json=?,updated=? WHERE id=?',(encode(profile),text,encode(report),time.time(),rid))
        db.execute("UPDATE runs SET answer=?,response_kind='analysis',updated=? WHERE id=?",(text,time.time(),review['run_id']))
        db.execute('INSERT INTO analysis_preferences(user_id,chat_id,video_id,profile,pending,updated) VALUES(?,?,?,?,NULL,?) '
            'ON CONFLICT(user_id,chat_id,video_id) DO UPDATE SET profile=excluded.profile,pending=NULL,updated=excluded.updated',
            (review['user_id'],review['chat_id'],review['video_id'],encode(profile['analysis_profile']),time.time()))
    assert store.one('SELECT COUNT(*) n FROM calls')['n']==calls_before
    (settings.root/'logs/five-part-report.md').write_text(text,encoding='utf-8')
    (settings.root/'logs/five-part-report.json').write_text(encode(report),encoding='utf-8')
    result={'backup':str(backup),'review_id':rid,'report_version':'five-part-v1','scene_coaching':len(report['scene_coaching']),
        'level_status':report['level_assessment']['status'],'level_confidence':report['level_assessment']['confidence'],
        'spent_usd':status['spent_usd'],'reserved_usd':status['reserved_usd'],'cap_usd':status['cap_usd'],
        'publish_new_api_calls':0,'tests':103,'automatic_coaching_accuracy_passed':False}
    (settings.root/'logs/five-part-intake-deployment.json').write_text(encode(result),encoding='utf-8')
    print(encode(result))


if __name__=='__main__':main()
