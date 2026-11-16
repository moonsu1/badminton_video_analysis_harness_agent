"""Real Luna cross-chat memory test, text only, isolated DB, $0.05 bound."""
import json
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path

from badminton.agent import Provider,native_run
from badminton.config import Settings,load_settings
from badminton.intake import prepare
from badminton.memory import Memory
from badminton.pricing import reservation_cost
from badminton.routing import route_request
from badminton.store import Store,encode
from badminton.video import VideoService


class TextBudgetProvider(Provider):
    def create(self,purpose,**kwargs):
        tokens=self.count_input(**kwargs);own=kwargs.get('_call_id','')
        bound=reservation_cost(tokens,kwargs.get('max_output_tokens',4000),self.settings.model)
        used=self.store.one("SELECT COALESCE(SUM(actual_micros),0)+COALESCE(SUM(CASE WHEN status IN ('reserved','running','unknown') THEN reserved_micros ELSE 0 END),0) n FROM calls WHERE id<>?",(own,))['n']
        if used+bound>50000:raise RuntimeError('Text memory smoke test reached its $0.05 bound.')
        return super().create(purpose,**kwargs)


def main():
    production=load_settings();settings=Settings(Path(tempfile.mkdtemp(prefix='badminton-memory-')),model=production.model,max_tools=5).initialize()
    store=Store(settings.root/'agent.db');memory=Memory(store);videos=VideoService(settings,store)
    videos.frames=lambda *a,**k:(_ for _ in ()).throw(AssertionError('Memory test must not extract video frames'))
    report={'model':settings.model,'data':str(settings.root),'passed':False,'budget_usd':.05,'new_video_frames':0,'steps':[]}
    started=time.monotonic()
    try:
        # Copy existing structured text and source metadata, never source pixels.
        with sqlite3.connect('file:'+str(production.root/'agent.db')+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            review=dict(db.execute("SELECT * FROM reviews WHERE report_json IS NOT NULL AND status IN ('completed','partial') ORDER BY updated DESC LIMIT 1").fetchone())
            video=dict(db.execute('SELECT * FROM videos WHERE id=?',(review['video_id'],)).fetchone())
            run=dict(db.execute('SELECT * FROM runs WHERE id=?',(review['run_id'],)).fetchone())
        for table,row in [('videos',video),('runs',run),('reviews',review)]:
            row.update(user_id='memory-test',chat_id='old-match')
            columns=list(row);store.execute('INSERT INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(row[k] for k in columns))
        memory.backfill('memory-test');assert memory.context('memory-test')['episodes']
        def ask(chat,question):
            row=store.create_run('memory-test',chat,uuid.uuid4().hex,question,[])
            provider=TextBudgetProvider(settings,store,row['id'])
            route=route_request(settings,store,row,provider)
            assert route['intent'] in {'general','explanation'},route
            assert prepare(store,row,route)['ready']
            answer=native_run(settings,store,videos,store.run(row['id']),provider)
            store.update_run(row['id'],'completed',answer=answer)
            result={'chat':chat,'question':question,'route':route,'answer':answer}
            report['steps'].append(result);print(encode(result),flush=True)
            return answer
        ask('intro','나는 왼손잡이고 복식 위주로 쳐. 앞으로 풋워크와 복귀를 개선하는 게 목표야. 내 특성에 맞게 답변해줘.')
        fields={row['field'] for row in memory.rows('memory-test') if row['kind']=='profile'}
        assert {'handedness','play_format','goal'}.issubset(fields),fields
        answer=ask('fresh','새 채팅이야. 내 주손과 주로 하는 종목,연습 목표를 기억해? 이전 경기의 조언도 장면 근거와 연결해서 오늘 10분 연습을 제안해줘.')
        assert '왼손' in answer and '복식' in answer and ('풋워크' in answer or '복귀' in answer)
        assert video['filename'] in answer and any(c.isdigit() for c in answer)
        memory.command('memory-test','training','progress','성장기록 지난 2주 동안 복귀 연습을 주 3회 했고 편해졌다고 느꼈어.')
        answer=ask('another','내가 최근 어떤 연습을 했다고 말했지? 실제 실력이 좋아진 것도 측정된 건가?')
        assert ('2주' in answer or '두 주' in answer) and ('3회' in answer or '세 번' in answer)
        ask('correction','정정할게. 사실 나는 오른손잡이야. 앞으로 이걸 기준으로 설명해줘.')
        hand=next(row for row in memory.rows('memory-test') if row['field']=='handedness')
        assert '오른손' in hand['content']
        memory.command('memory-test','controls','off','기억끄기')
        assert not memory.context('memory-test')['profiles']
        memory.command('memory-test','controls','on','기억켜기')
        memory.command('memory-test','controls','delete','기억삭제 '+hand['id'][:12])
        memory.backfill('memory-test')
        assert not any(r['id']==hand['id'] for r in memory.rows('memory-test'))
        assert not memory.context('different-user')['episodes']
        report['passed']=True
    finally:
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        report['calls']=store.all('SELECT purpose,status,model,response_model,actual_micros FROM calls')
        report['cost_usd']=sum(row['actual_micros'] or 0 for row in report['calls'])/1e6
        report['remaining_memories']=len(memory.rows('memory-test'))
        assert all(row['purpose'] in {'agent','routing'} and row['response_model']=='gpt-6-luna' for row in report['calls'])
        (production.root/'logs/personal-memory-smoke.json').write_text(encode(report),encoding='utf-8')
        print(encode({k:v for k,v in report.items() if k not in {'steps','calls'}}),flush=True)


if __name__=='__main__':main()
