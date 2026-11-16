"""Dedicated stdio MCP tools for the optional local Hermes profile; no shell access."""
import base64
import json
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import TextContent, ImageContent

from .agent import Provider, ToolSession
from .config import load_settings
from .store import Store, encode
from .video import VideoService

server = MCPServer('Badminton video tools')
sessions = {}


def execute(name, arguments):
    settings = load_settings().initialize()
    store = Store(settings.root / 'agent.db')
    if settings.runtime != 'hermes':
        raise PermissionError('Hermes 모드에서만 사용할 수 있는 도구입니다.')
    rows = store.all("SELECT * FROM runs WHERE status='analyzing'")
    if len(rows) != 1:
        raise PermissionError('공유 도구는 현재 단일 분석 작업에만 연결됩니다.')
    run = rows[0]
    if run['id'] not in sessions:
        sessions.clear()
        sessions[run['id']] = ToolSession(settings, store, VideoService(settings, store), run, Provider(settings, store, run['id']))
        budget = store.cached('hermes-budget:' + run['id']) or {}
        sessions[run['id']].images = budget.get('images', 0)
        sessions[run['id']].seconds = budget.get('seconds', 0)
        sessions[run['id']].precision_calls = budget.get('precision_calls', 0)
        sessions[run['id']].evidence = store.cached('evidence:' + run['id']) or []
    session = sessions[run['id']]
    if name == 'complete_analysis':
        session.check()
        answer = session.finalize(arguments['answer'], arguments['kind'], arguments['evidence_ids'])
        store.cache('hermes-final:' + run['id'], '', 'final', {'answer': answer})
        return [TextContent(type='text', text='검증된 답변을 저장했습니다. 이 답변으로 마무리하세요.\n'+answer)]
    count_key = 'hermes-count:' + run['id']
    count = store.cached(count_key) or 0
    if count >= settings.max_tools:
        raise ValueError('도구 호출 한도에 도달했습니다. 확인한 범위로 답하세요.')
    store.cache(count_key, '', 'budget', count + 1)
    result, frames = session.execute(name, arguments)
    store.cache('hermes-budget:' + run['id'], '', 'budget', {'images': session.images, 'seconds': session.seconds, 'precision_calls': session.precision_calls})
    content = [TextContent(type='text', text=encode(result))]
    for frame in frames:
        content.append(TextContent(type='text', text=f"{frame.get('filename')} 원본 시각 {frame['time']:.6f}초"))
        mime = 'image/png' if Path(frame['path']).suffix=='.png' else 'image/jpeg'
        content.append(ImageContent(type='image', mimeType=mime, data=base64.b64encode(Path(frame['path']).read_bytes()).decode()))
    return content


@server.tool(description='이 작업에 연결된 영상 정보와 저장된 대상 선수 확인')
def get_video_info(video_id: str):
    return execute('get_video_info', {'video_id': video_id})


@server.tool(description='전체 영상을 10초 간격으로 훑은 관찰 기록. 구체적 코칭에는 inspect_segment로 추가 확인 필요')
def get_overview(video_id: str):
    return execute('get_overview', {'video_id': video_id})


@server.tool(description='질문과 관련된 후보 시간 구간을 찾아 반환')
def find_segments(video_id: str, query: str):
    return execute('find_segments', {'video_id': video_id, 'query': query})


@server.tool(description='원본 해상도 연속 장면. fps=0은 원본 프레임 전부, 최대3초. 선택적 box는0~1 원본ROI. 짧은 타격 구간을 지정한다.')
def inspect_segment(video_id: str, start: float, end: float, fps: float = 0, box: list[float] | None = None):
    return execute('inspect_segment', {'video_id': video_id, 'start': start, 'end': end, 'fps': fps, 'box': box})


@server.tool(description='셔틀 비행 영역의 원본 연속 장면 판독과 미검증 이동 후보 좌표. box는0~1 원본ROI, seed는ROI내 초기 후보[x,y].')
def inspect_trajectory(video_id: str, start: float, end: float, box: list[float], seed: list[float] | None = None):
    return execute('inspect_trajectory', {'video_id': video_id, 'start': start, 'end': end, 'box': box, 'seed': seed})


@server.tool(description='원본 시각 한 장면의 영역 확대. box=[left,top,right,bottom], 각 값0~1')
def crop_region(video_id: str, time: float, box: list[float]):
    return execute('crop_region', {'video_id': video_id, 'time': time, 'box': box})


@server.tool(description='이 대화의 이전 질문/답변과 저장된 선수 지정 확인')
def get_analysis_history():
    return execute('get_analysis_history', {})


@server.tool(description='사용자가 명시한 옷 색이나 코트 위치를 대상 선수로 저장. 얼굴 인식 또는 추측 지정 금지')
def set_player(video_id: str, description: str):
    return execute('set_player', {'video_id': video_id, 'description': description})


@server.tool(description='같은 대화의 기존 영상 판독 근거를 새 질문에 재사용. run_id와 해당 작업의 E번호 목록.')
def reuse_observations(run_id: str, evidence_ids: list[str]):
    return execute('reuse_observations', {'run_id':run_id,'evidence_ids':evidence_ids})


@server.tool(description='최종 답변 제출. kind=general(일반 지식),context(이전 설명),analysis(새 영상 관찰),clarification(확인 질문). analysis만 실제 E번호 목록을 제출하고 나머지는 빈 배열.')
def complete_analysis(answer: str, kind: str, evidence_ids: list[str]):
    if kind not in {'general', 'context', 'analysis', 'clarification'}:
        raise ValueError('지원하지 않는 답변 종류입니다.')
    return execute('complete_analysis', {'answer': answer, 'kind': kind, 'evidence_ids': evidence_ids})


if __name__ == '__main__':
    server.run(transport='stdio')
