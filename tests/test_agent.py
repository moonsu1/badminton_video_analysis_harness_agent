import json

import pytest

from badminton.agent import ModelResponse, ToolSession, native_run


class ScriptedProvider:
    def __init__(self, video_id):
        self.video_id = video_id
        self.inputs = []

    def create(self, purpose, **kwargs):
        self.inputs.append(kwargs['input'].copy())
        if len(self.inputs) == 1:
            return ModelResponse({'output': [{'type': 'function_call', 'call_id': 'a', 'name': 'inspect_segment', 'arguments': json.dumps({'video_id': self.video_id, 'start': 0, 'end': 1, 'fps': 2})}]})
        return ModelResponse({'output': [{'type': 'function_call', 'call_id': 'done', 'name': 'complete_analysis', 'arguments': json.dumps({'answer':'관찰 [00:00] 확인한 합성 시험 장면입니다.','kind':'analysis','evidence_ids':['E1']})}]})


def test_tool_loop_passes_real_images(system, clip):
    settings, store, videos = system
    run = store.create_run('user1', 'chat1', 'test', '확인', [clip['id']])
    provider = ScriptedProvider(clip['id'])
    answer = native_run(settings, store, videos, run, provider)
    assert '[00:00]' in answer
    assert any(part.get('type') == 'input_image' for item in provider.inputs[-1] if item.get('role') == 'user' and isinstance(item.get('content'), list) for part in item['content'])
    assert len(store.cached('evidence:' + run['id'])[0]['frames']) == 2


def test_tools_reject_foreign_video_and_limit_images(system, clip):
    settings, store, videos = system
    run = store.create_run('user1', 'chat1', 'test', '확인', [clip['id']])
    session = ToolSession(settings, store, videos, run, None)
    with pytest.raises(PermissionError):
        session.execute('get_video_info', {'video_id': 'foreign'})
    session.images = settings.max_fine_images
    with pytest.raises(ValueError, match='한도'):
        session.execute('inspect_segment', {'video_id': clip['id'], 'start': 0, 'end': 1, 'fps': 1})


def test_hallucinated_timestamp_is_not_published(system, clip):
    settings, store, videos = system
    run = store.create_run('user1', 'chat1', 'test', '확인', [clip['id']])
    class BadProvider:
        calls = 0
        def create(self, *args, **kwargs):
            self.calls += 1
            if self.calls==1:
                name,arguments='inspect_segment',{'video_id':clip['id'],'start':0,'end':1,'fps':2}
            else:
                name,arguments='complete_analysis',{'answer':'[42:00] 확인했습니다.','kind':'analysis','evidence_ids':['E1']}
            return ModelResponse({'output':[{'type':'function_call','call_id':str(self.calls),'name':name,'arguments':json.dumps(arguments)}]})
    with pytest.raises(RuntimeError, match='영상 시각'):
        native_run(settings, store, videos, run, BadProvider())
