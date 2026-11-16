"""Real-model intent checks in isolated chats; never edits user conversations."""
import json
import subprocess
import tempfile
import time
from pathlib import Path

from badminton.agent import native_run
from badminton.config import Settings, load_settings
from badminton.store import Store, encode
from badminton.video import VideoService, ffmpeg_binary


def main():
    production=load_settings()
    root=Path(tempfile.mkdtemp(prefix='badminton-conversation-'))
    settings=Settings(root,model=production.model).initialize()
    store=Store(root/'agent.db')
    videos=VideoService(settings,store)
    path=settings.webui/'uploads'/'synthetic.mp4'
    subprocess.run([ffmpeg_binary(),'-v','error','-f','lavfi','-i','testsrc2=duration=2:size=320x180:rate=12','-c:v','libx264','-y',str(path)],check=True)
    video=videos.register(str(path),'test-file','synthetic.mp4','test','with-video')
    precise=store.create_run('test','with-video','past','영상의 스매시 그립을 봐줘',[video['id']])
    store.execute('UPDATE runs SET response_kind=? WHERE id=?',('analysis',precise['id']))
    store.update_run(precise['id'],'completed',answer='손가락이 불명확해서 그립을 확정하지 못했습니다. 일반적인 연습으로 손잡이를 느슨하게 쥐고 타격 순간에만 조이는 연습을 제안했습니다.')
    cases=[
        ('empty','스매시 스윙할때 이스턴 그립이 맞아? 아니면 주먹그립이 맞아?',{'general'},False),
        ('with-video','스매시 스윙할때 이스턴 그립이 맞아? 아니면 주먹그립이 맞아?',{'general'},False),
        ('with-video','셔틀 궤적은 왜 휘어지는 거야? 일반적인 원리가 궁금해.',{'general'},False),
        ('with-video','아까 손가락이 불명확하다고 한 말과 추천한 연습을 쉽게 설명해줘.',{'context'},False),
        ('with-video','그 연습을 일주일 훈련 계획으로 만들어줘.',{'general','context'},False),
        ('missing','내 영상에서 실제로 어떤 그립을 쓰는지 확인해줘.',{'clarification'},False),
        ('with-video','첨부된 것은 선수가 없는 합성 시험 영상이야. 실제 도구로 0~0.3초를 초당 4장 확인해서 색과 도형만 설명해줘. 선수 지정은 필요 없어.',{'analysis'},True),
        ('with-video','회내가 뭔지 더 쉽게 설명해줘. 일반적인 개념이 궁금해.',{'general'},False),
    ]
    report={'test':'adaptive_conversation_real_api','model':settings.model,'isolated_data':str(root),'cases':[]}
    try:
        for index,(chat,question,kinds,requires_images) in enumerate(cases):
            ids=[video['id']] if chat=='with-video' else []
            run=store.create_run('test',chat,'case-'+str(index),question,ids)
            started=time.monotonic()
            answer=native_run(settings,store,videos,run)
            store.update_run(run['id'],'completed',answer=answer)
            kind=store.run(run['id'])['response_kind']
            tools=[json.loads(e['data'])['name'] for e in store.all("SELECT data FROM events WHERE run_id=? AND kind='tool'",(run['id'],))]
            reads=[t for t in tools if t in {'inspect_segment','get_overview','find_segments','crop_region','inspect_trajectory'}]
            calls=store.all('SELECT purpose,usage FROM calls WHERE run_id=?',(run['id'],))
            item={'question':question,'attached':bool(ids),'kind':kind,'tools':tools,'elapsed':round(time.monotonic()-started,2),
                'answer':answer,'usage':[json.loads(c['usage'] or '{}') for c in calls]}
            report['cases'].append(item)
            assert kind in kinds,(question,kind,answer)
            assert bool(reads)==requires_images,(question,tools)
            if not requires_images:
                assert all(c['purpose']=='agent' for c in calls),(question,calls)
                if kind=='general':
                    assert not any(w in answer for w in ['영상을 첨부해','영상을 올려','선수를 알려']),(question,answer)
            print(json.dumps({'case':index,'kind':kind,'video_reads':len(reads),'passed':True},ensure_ascii=False),flush=True)
        report['passed']=True
    finally:
        (production.root/'logs/conversation-upgrade.json').write_text(encode(report),encoding='utf-8')
    print('All adaptive conversation cases passed.',flush=True)


if __name__=='__main__':
    main()
