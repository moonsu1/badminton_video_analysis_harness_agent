"""Small real text-only routing check; never decodes or uploads video frames."""
import json
import tempfile
from pathlib import Path

from badminton.config import Settings,load_settings
from badminton.intake import prepare
from badminton.routing import route_request
from badminton.store import Store,encode


def main():
    production=load_settings();original=Store(production.root/'agent.db')
    source=original.one('SELECT * FROM videos ORDER BY created DESC LIMIT 1')
    settings=Settings(Path(tempfile.mkdtemp(prefix='badminton-intake-smoke-')),model=production.model).initialize()
    store=Store(settings.root/'agent.db')
    columns=list(source);source.update(user_id='test-user',chat_id='test-chat',player='검은 상하의,보라색 팔 무늬')
    store.execute('INSERT INTO videos('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(source[k] for k in columns))
    scenarios=[
        ('경기 영상 분석해줘','ask'),
        ('1','gameplay'),
        ('스매시 타점을 비교해서 봐줘','clip'),
        ('일반적으로 스매시는 이스턴 그립이 맞아?','general'),
        ('이 영상은 개인 강습이야. 배운 동작 전체의 수행과 고칠 점을 분석해줘.','lesson'),
        ('드라이브 난타 영상이야. 반복 동작의 일관성과 실수 패턴을 분석해줘.','rally'),
        ('이번 경기의 전반적인 특징,내 수준과 장단점을 전체 리포트로 분석해줘.','gameplay')]
    results=[]
    for n,(question,expected) in enumerate(scenarios):
        spent=store.one('SELECT COALESCE(SUM(actual_micros),0) AS n FROM calls')['n']/1e6
        assert spent+.008<=.05,'Text-only test budget exhausted'
        run=store.create_run('test-user','test-chat',str(n),question,[source['id']])
        route=route_request(settings,store,run);decision=prepare(store,run,route)
        if expected=='ask':assert not decision['ready'] and '경기 운영' in decision['question']
        elif expected=='general':assert decision['ready'] and route['intent']=='general'
        elif expected=='clip':assert decision['ready'] and route['intent']=='clip'
        elif expected in {'lesson','rally'}:assert decision['ready'] and route['analysis_profile']['video_kind']==expected
        else:assert decision['ready'] and route['analysis_profile']['analysis_focus']=='gameplay'
        answer=decision.get('question') or '분석 목적을 확인했습니다.'
        store.update_run(run['id'],'completed',answer=answer)
        results.append({'question':question,'route':route,'ready':decision['ready']})
        print(encode(results[-1]),flush=True)
    report={'test':'real_text_only_analysis_intake','passed':True,'results':results,
        'cost_usd':store.one('SELECT COALESCE(SUM(actual_micros),0) AS n FROM calls')['n']/1e6,'video_frames_sent':0,'data':str(settings.root)}
    (production.root/'logs/analysis-intake-smoke.json').write_text(encode(report),encoding='utf-8')
    print(encode({k:v for k,v in report.items() if k!='results'}))


if __name__=='__main__':main()
