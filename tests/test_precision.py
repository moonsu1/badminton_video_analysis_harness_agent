import json
import shutil
import subprocess

import av
import pytest
from PIL import Image, ImageChops, ImageDraw

from badminton.agent import ModelResponse, ToolSession, native_run
from badminton.motion import trajectory_candidates
from badminton.video import ffmpeg_binary
from badminton.vision import image_parts


def test_native_keeps_every_pts_full_resolution_and_png(system):
    settings, store, videos = system
    path = settings.webui/'uploads'/'native.mp4'
    subprocess.run([ffmpeg_binary(),'-v','error','-f','lavfi','-i','testsrc2=duration=0.4:size=1920x1080:rate=60000/1001','-c:v','libx264','-preset','ultrafast','-y',str(path)],check=True)
    video = videos.register(str(path),'native','native.mp4','u','c')
    frames = videos.frames(video,0,.2,fps=0,maximum=20)
    with av.open(video['path']) as container:
        expected = [(float(f.pts*f.time_base),f.to_image()) for f in container.decode(video=0) if f.pts is not None and float(f.pts*f.time_base)<.2]
    assert len(frames)==len(expected)>=11
    assert [f['time'] for f in frames]==[round(t,6) for t,_ in expected]
    assert all(f['width']==1920 and f['height']==1080 and f['path'].endswith('.png') for f in frames)
    with Image.open(frames[0]['path']) as actual:
        assert ImageChops.difference(actual,expected[0][1]).getbbox() is None
    with pytest.raises(ValueError,match='구간을 더 짧게'):
        videos.frames(video,0,.2,fps=0,maximum=4)


def test_shared_image_cache_relabels_current_video(system,clip):
    _,_,videos=system
    first=videos.frames(clip,0,1,fps=2)
    other={**clip,'id':'new-id','filename':'new-name.mp4'}
    second=videos.frames(other,0,1,fps=2)
    assert first[0]['path']==second[0]['path']
    assert second[0]['video_id']=='new-id'
    assert second[0]['filename']=='new-name.mp4'
    parts=image_parts(second)
    assert 'new-name.mp4' in parts[0]['text'] and 'new-id' in parts[0]['text']
    assert parts[1]['detail']=='original' and parts[1]['image_url'].startswith('data:image/png;')


class PrecisionProvider:
    def __init__(self,video_id):
        self.video_id=video_id
        self.agent_calls=0
        self.precision_calls=0

    def create(self,purpose,**kwargs):
        if purpose=='precision':
            self.precision_calls+=1
            return ModelResponse({'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'summary':'합성 장면','observations':[],'grip_visible':False,'shuttle_visible':False,'limitations':['손가락이 구별되지 않음']})}]}]})
        self.agent_calls+=1
        if self.agent_calls==1:
            name,args='inspect_segment',{'video_id':self.video_id,'start':1,'end':1.8,'fps':0,'box':[0,0,.5,.5]}
        else:
            name,args='complete_analysis',{'kind':'analysis','evidence_ids':['E1'],'answer':'[00:01] 확대해 확인했지만 그립 세부는 구분되지 않습니다.'}
        return ModelResponse({'output':[{'type':'function_call','call_id':f'call{self.agent_calls}','name':name,'arguments':json.dumps(args)}]})


def test_precision_loop_requires_real_native_crop_and_keeps_notes(system,clip):
    settings,store,videos=system
    run=store.create_run('user1','chat1','fine','1초 부근 그립을 확인해줘',[clip['id']])
    provider=PrecisionProvider(clip['id'])
    answer=native_run(settings,store,videos,run,provider)
    assert '그립 세부' in answer and '직접 확인한 근거' in answer
    assert provider.precision_calls==1 and provider.agent_calls==2
    evidence=store.cached('evidence:'+run['id'])[0]
    assert evidence['native'] and evidence['sequence']['native_frames_analyzed']>=8
    assert evidence['sequence']['grip_visible'] is False


def test_no_precision_conclusion_from_sparse_frames(system,clip):
    settings,store,videos=system
    run=store.create_run('user1','chat1','fine','그립과 셔틀 궤적을 분석해줘',[clip['id']])
    session=ToolSession(settings,store,videos,run,None)
    session.execute('inspect_segment',{'video_id':clip['id'],'start':0,'end':1,'fps':2})
    with pytest.raises(ValueError,match='원본 연속 판독'):
        session.finalize('확인했습니다.',evidence_ids=['E1'])


def test_motion_tracks_are_candidates_not_measurements(tmp_path):
    frames=[]
    for i in range(12):
        im=Image.new('RGB',(240,160),'black')
        draw=ImageDraw.Draw(im)
        draw.ellipse((20+i*8,40+i*2,26+i*8,46+i*2),fill='white')
        draw.ellipse((180,70,186,76),fill='white')
        path=tmp_path/f'{i}.png';im.save(path)
        frames.append({'path':str(path),'time':i/60,'width':240,'height':160})
    result=trajectory_candidates(frames,seed=[28/240,45/160])
    assert result['tracks']
    assert len(result['tracks'][0])>=5
    assert result['tracks'][0][-1]['x']>result['tracks'][0][0]['x']
    assert result['method']=='unverified_bright_motion_candidates'
    assert '계측이 아닙니다' in result['warning']


def test_corrected_attachment_context_migrates_and_deduplicates(system,clip):
    settings,store,videos=system
    duplicate=settings.webui/'uploads'/'copy.mp4'
    shutil.copyfile(clip['path'],duplicate)
    new=videos.register(str(duplicate),'new-file','corrected.mp4','user1','chat1')
    store.create_run('user1','chat1','old','<file id="file1"/> 기존 영상',[clip['id']])
    store.create_run('user1','chat1','new','<attached_files><file id="new-file"/></attached_files> 잘못 올렸다 다시',[clip['id'],new['id']])
    assert store.active_videos('user1','chat1')==[new['id']]
    assert store.set_active_videos('user1','chat1',[clip['id'],new['id']])==[new['id']]
    with pytest.raises(PermissionError):
        store.set_active_videos('other','chat1',[clip['id']])


def test_comparison_requires_evidence_from_each_video(system,clip):
    settings,store,videos=system
    duplicate=settings.webui/'uploads'/'other.mp4'
    shutil.copyfile(clip['path'],duplicate)
    other=videos.register(str(duplicate),'other-file','other.mp4','user1','chat1')
    run=store.create_run('user1','chat1','compare','1초 스매시를 비교해줘',[clip['id'],other['id']])
    session=ToolSession(settings,store,videos,run,PrecisionProvider(clip['id']))
    session.execute('inspect_segment',{'video_id':clip['id'],'start':1,'end':1.8,'fps':0})
    with pytest.raises(ValueError,match='other.mp4'):
        session.finalize('첫 파일이 더 강합니다.',evidence_ids=['E1'])
    session.execute('inspect_segment',{'video_id':other['id'],'start':1,'end':1.8,'fps':0})
    assert 'other.mp4' in session.finalize('확인했지만 파워를 계측할 수는 없습니다.',evidence_ids=['E1','E2'])


def test_legacy_answer_is_excluded_from_new_model_context(system,clip):
    settings,store,videos=system
    old=store.create_run('user1','chat1','legacy','내가 누군지 알려줬어',[clip['id']])
    store.update_run(old['id'],'completed',answer='WRONG-OLD-CONCLUSION')
    store.execute('UPDATE runs SET analysis_version=1 WHERE id=?',(old['id'],))
    current=store.create_run('user1','chat1','fresh','다시 분석해줘',[clip['id']])
    context,_=ToolSession(settings,store,videos,current,None).execute('get_analysis_history',{})
    assert 'WRONG-OLD-CONCLUSION' not in json.dumps(context)
    assert context['history'][0]['question']=='내가 누군지 알려줬어'


class FrameIdProvider:
    def __init__(self):
        self.requests=[]

    def create(self,purpose,**kwargs):
        assert purpose=='precision'
        self.requests.append(kwargs)
        observation=kwargs['text']['format']['schema']['properties']['observations']['items']
        assert 'time' not in observation['properties']
        ids=observation['properties']['frame_id']['enum']
        result={'summary':'연속 장면 관찰','observations':[{'frame_id':ids[-1],'observation':'마지막 제공 프레임의 동작'}],
                'grip_visible':False,'shuttle_visible':False,'limitations':[]}
        return ModelResponse({'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result)}]}]})


def test_precision_frame_ids_map_to_exact_pts_across_batch_boundaries(system,clip):
    settings,store,videos=system
    run=store.create_run('user1','chat1','ids','1초 스매시를 봐줘',[clip['id']])
    provider=FrameIdProvider()
    session=ToolSession(settings,store,videos,run,provider)
    result,_=session.execute('inspect_segment',{'video_id':clip['id'],'start':0,'end':2,'fps':0})
    assert len(provider.requests)==2
    frames=result['frames']
    for batch in result['sequence']['batches']:
        assert batch['time_source']=='server_frame_pts'
        for observation in batch['observations']:
            assert observation['time']==frames[int(observation['frame_id'][1:])]['time']
    second_ids=provider.requests[1]['text']['format']['schema']['properties']['observations']['items']['properties']['frame_id']['enum']
    assert second_ids[0]=='F0018'
    assert any('frame_id=F0018' in p.get('text','') for p in provider.requests[1]['input'][1]['content'])
    session.execute('inspect_segment',{'video_id':clip['id'],'start':0,'end':2,'fps':0})
    assert len(provider.requests)==2  # Validated per-batch cache reuse makes no new vision calls.


def test_invalid_frame_reference_returns_to_harness_and_can_recover(system,clip):
    settings,store,videos=system
    run=store.create_run('user1','chat1','recover','1초 스매시를 봐줘',[clip['id']])
    class RecoveringProvider(FrameIdProvider):
        agent_calls=0
        failed=False
        def create(self,purpose,**kwargs):
            if purpose=='precision':
                if not self.failed:
                    self.failed=True
                    result={'summary':'무효 번호','observations':[{'frame_id':'F9999','observation':'잘못된 위치'}],
                            'grip_visible':False,'shuttle_visible':False,'limitations':[]}
                    return ModelResponse({'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(result)}]}]})
                return super().create(purpose,**kwargs)
            self.agent_calls+=1
            if self.agent_calls==1:
                name,args='inspect_segment',{'video_id':clip['id'],'start':0,'end':.8,'fps':0}
            elif self.agent_calls==2:
                assert '프레임 번호' in kwargs['input'][-1]['output']
                name,args='inspect_segment',{'video_id':clip['id'],'start':1,'end':1.8,'fps':0}
            else:
                name,args='complete_analysis',{'answer':'[00:01] 새 구간에서 확인했습니다.','kind':'analysis','evidence_ids':['E1']}
            return ModelResponse({'output':[{'type':'function_call','call_id':str(self.agent_calls),'name':name,'arguments':json.dumps(args)}]})
    provider=RecoveringProvider()
    answer=native_run(settings,store,videos,run,provider)
    assert '직접 확인한 근거' in answer and provider.agent_calls==3
    assert store.all("SELECT id FROM events WHERE run_id=? AND kind='precision_validation'",(run['id'],))
    assert store.cached('evidence:'+run['id'])[0]['frames'][0]['time']==1


def test_broad_comparison_leaves_frame_budget_for_both_videos(system,clip):
    settings,store,videos=system
    run=store.create_run('user1','chat1','budget','두 영상의 스매시를 비교해줘',[clip['id']])
    session=ToolSession(settings,store,videos,run,None)
    # Use the real user's ~60fps metadata; reject before any extraction or API call.
    original_video=session.video
    session.video=lambda video_id:{**original_video(video_id),'info':{**clip['info'],'fps':60}}
    with pytest.raises(ValueError,match='예산을 남겨야'):
        session.execute('inspect_segment',{'video_id':clip['id'],'start':.1,'end':1.9,'fps':0})
    assert session.images==0 and session.precision_calls==0
