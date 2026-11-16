import asyncio
import hmac
import json
import logging
import errno
import time
from typing import Literal
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .agent import Cancelled, native_run
from .config import load_settings
from .store import Store, encode
from .video import VideoService

log = logging.getLogger(__name__)
TERMINAL = {'completed', 'failed', 'cancelled'}


class WorkerLease:
    def __init__(self, path):
        self.path, self.handle = path, None

    def acquire(self):
        import os
        self.handle = self.path.open('a+b')
        if self.path.stat().st_size == 0:
            self.handle.write(b'1')
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError('같은 데이터 폴더의 분석 서비스가 이미 실행 중입니다.')

    def release(self):
        if self.handle:
            self.handle.close()


class Identity(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=200)


class VideoInput(Identity):
    source_path: str
    source_id: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=500)


class RunInput(Identity):
    request_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=20000)
    video_ids: list[str] = Field(default_factory=list, max_length=10)
    scope: Literal['auto','match','clip'] = 'auto'


class ResumeInput(Identity):
    request_id: str = Field(min_length=1,max_length=200)
    additional_usd: float = Field(default=0,ge=0,le=100)


class MemoryCommand(Identity):
    request_id:str=Field(min_length=1,max_length=200)
    question:str=Field(min_length=1,max_length=20000)


class ContextInput(Identity):
    video_ids: list[str] = Field(min_length=1, max_length=10)


def public_run(row):
    return {k:row[k] for k in ['id', 'status', 'question', 'answer', 'error', 'created', 'updated','response_kind']}


def create_app(settings=None, runner=None):
    settings = (settings or load_settings()).initialize()
    store = Store(settings.root / 'agent.db')
    videos = VideoService(settings, store)
    runner = runner or native_run
    from .review_store import Reviews
    reviews=Reviews(settings,store)
    short_tasks=set()
    short_slots=asyncio.Semaphore(1)

    async def regular(row):
        run_id=row['id']
        try:
            store.update_run(run_id,'analyzing')
            if settings.runtime=='hermes' and runner is native_run:
                from .hermes import hermes_run
                answer=await asyncio.to_thread(hermes_run,settings,store,videos,row)
            else:
                answer=await asyncio.to_thread(runner,settings,store,videos,row)
            if store.run(run_id)['cancel']:store.update_run(run_id,'cancelled',answer=answer)
            else:store.update_run(run_id,'completed',answer=answer)
        except Cancelled:store.update_run(run_id,'cancelled')
        except Exception as exc:
            log.error('run=%s error_type=%s',run_id,type(exc).__name__)
            message=str(exc) if isinstance(exc,(ValueError,PermissionError,RuntimeError)) else '작업을 완료하지 못했습니다. 상태확인.cmd의 로그를 확인해 주세요.'
            store.update_run(run_id,'failed',error=message)

    async def short(row):
        async with short_slots:
            await regular(row)

    async def heavy_worker():
        from .review import MatchReviewer
        while True:
            review=store.one("SELECT * FROM reviews WHERE status='queued' ORDER BY created LIMIT 1")
            clip=store.one("SELECT * FROM runs WHERE status='queued' AND scope='clip' AND route IS NOT NULL ORDER BY created LIMIT 1")
            if clip and (not review or clip['created']<review['created']):
                if clip['cancel']:store.update_run(clip['id'],'cancelled')
                else:await regular(clip)
            elif review:
                row=store.run(review['run_id'])
                store.update_run(row['id'],'analyzing')
                try:
                    answer=await asyncio.to_thread(MatchReviewer(settings,store,videos,row,review['id']).execute)
                    followers=store.all("SELECT * FROM runs WHERE review_id=? AND status IN ('preparing','analyzing')",(review['id'],))
                    for follower in followers:
                        store.update_run(follower['id'],'cancelled' if follower['cancel'] else 'completed',answer=answer)
                except Exception as exc:
                    log.error('review=%s error_type=%s',review['id'],type(exc).__name__)
                    store.execute("UPDATE reviews SET status='paused_error' WHERE id=?",(review['id'],))
                    for follower in store.all("SELECT id FROM runs WHERE review_id=? AND status IN ('preparing','analyzing')",(review['id'],)):
                        store.update_run(follower['id'],'failed',error='전체 리뷰가 중단됐습니다. 완료된 구간과 비용 기록은 보관했습니다. 리뷰계속으로 재개할 수 있습니다.')
            else:await asyncio.sleep(.2)

    async def worker():
        while True:
            row = store.one("SELECT * FROM runs WHERE status='queued' AND route IS NULL ORDER BY created LIMIT 1")
            if not row:
                await asyncio.sleep(.3)
                continue
            run_id = row['id']
            if row['cancel']:
                store.update_run(run_id, 'cancelled')
                continue
            try:
                store.update_run(run_id, 'preparing')
                store.event(run_id, 'progress', {'message': '질문과 대화 맥락 확인 중'})
                if runner is native_run:
                    from .routing import route_request
                    route=await asyncio.to_thread(route_request,settings,store,row)
                    from .intake import prepare
                    intake=prepare(store,row,route);route=intake['route']
                    if not intake['ready']:
                        store.execute("UPDATE runs SET response_kind='clarification' WHERE id=?",(run_id,))
                        store.update_run(run_id,'completed',answer=intake['question'])
                        continue
                    row=store.run(run_id)
                    ids=json.loads(row['video_ids'])
                    if route['intent']=='match':
                        if len(ids)!=1:
                            store.update_run(run_id,'completed',answer='전체 경기 리뷰는 영상1개씩 진행합니다. 분석할 영상 파일명을 지정해 주세요.')
                            continue
                        video=store.video(ids[0],row['user_id'],row['chat_id'])
                        if not video['player']:
                            store.update_run(run_id,'completed',answer='경기 전체에서 어떤 선수를 볼지 옷 색과 코트 위치로 알려 주세요. 영상은 다시 올리지 않아도 됩니다.')
                            continue
                        review=reviews.ensure(row,video)
                        existing_profile=json.loads(review['profile']).get('analysis_profile')
                        if review['status']=='completed' and existing_profile and existing_profile!=route.get('analysis_profile',existing_profile):
                            route.update(intent='clip',reuse_review_id=review['id'])
                            store.execute("UPDATE runs SET route=?,scope='clip',review_id=NULL WHERE id=?",(encode(route),run_id))
                            store.update_run(run_id,'queued')
                            continue
                        if review['run_id']==run_id:
                            store.execute("UPDATE calls SET review_id=? WHERE run_id=? AND purpose='routing'",(review['id'],run_id))
                        if review['status'] not in {'running','queued'}:
                            store.update_run(run_id,'completed',answer=review['report'] or '저장된 리뷰가 중단된 상태입니다. 리뷰계속으로 재개할 수 있습니다.')
                        else:
                            store.update_run(run_id,'analyzing')
                        continue
                    if route['intent']=='clip':
                        store.execute("UPDATE runs SET scope='clip' WHERE id=?",(run_id,))
                        store.update_run(run_id,'queued')
                        continue
                    row=store.run(run_id)
                    task=asyncio.create_task(short(row));short_tasks.add(task);task.add_done_callback(short_tasks.discard)
                    continue
                store.update_run(run_id, 'analyzing')
                if settings.runtime == 'hermes' and runner is native_run:
                    from .hermes import hermes_run
                    answer = await asyncio.to_thread(hermes_run, settings, store, videos, row)
                else:
                    answer = await asyncio.to_thread(runner, settings, store, videos, row)
                if store.run(run_id)['cancel']:
                    store.update_run(run_id, 'cancelled')
                else:
                    store.update_run(run_id, 'completed', answer=answer)
            except Cancelled:
                store.update_run(run_id, 'cancelled')
            except Exception as exc:
                # Avoid raw provider bodies/headers/paths in user-visible errors or logs.
                name = type(exc).__name__
                log.error('run=%s error_type=%s', run_id, name)
                if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, 112}:
                    message = '영상을 처리할 디스크 공간이 부족합니다. 여유 공간을 확보한 뒤 새 질문으로 요청해 주세요.'
                elif name in {'AuthenticationError', 'PermissionDeniedError'}:
                    message = 'OpenAI 인증 또는 모델 접근 권한을 확인해 주세요. 인증등록.cmd와 상태확인.cmd를 사용할 수 있습니다.'
                elif name == 'RateLimitError':
                    message = 'OpenAI 사용량 또는 요청 제한에 도달했습니다. 결제·한도를 확인하거나 잠시 후 새 질문으로 요청해 주세요. 완료된 관찰은 저장했습니다.'
                elif name == 'APIStatusError' or name in {'BadRequestError', 'InternalServerError', 'NotFoundError'}:
                    message = '모델 API가 요청을 처리하지 못했습니다. 모델 설정·접근 권한을 상태확인.cmd에서 확인해 주세요.'
                elif isinstance(exc, (ValueError, PermissionError, RuntimeError)):
                    message = str(exc)
                else:
                    message = f'분석 중 오류({name})가 발생했습니다. 완료된 장면은 보관했습니다. 상태확인.cmd로 확인해 주세요.'
                store.update_run(run_id, 'failed', error=message)

    @asynccontextmanager
    async def lifespan(app):
        lease = WorkerLease(settings.root / 'worker.lock')
        lease.acquire()
        try:
            store.recover()
            videos.cleanup()
            task = asyncio.create_task(worker())
            heavy=asyncio.create_task(heavy_worker()) if runner is native_run else None
            try:
                yield
            finally:
                task.cancel()
                if heavy:heavy.cancel()
                for active in short_tasks:active.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                if heavy:
                    try:await heavy
                    except asyncio.CancelledError:pass
        finally:
            lease.release()

    app = FastAPI(title='Badminton Agent', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.store, app.state.videos = settings, store, videos

    def auth(authorization: str = Header(default='')):
        if not hmac.compare_digest(authorization, 'Bearer ' + settings.token):
            raise HTTPException(401, '인증이 필요합니다.')

    @app.exception_handler(PermissionError)
    async def denied(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={'detail': '이 대화에서 접근할 수 없는 항목입니다.'})

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={'detail': str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={'detail': '업로드한 파일을 찾을 수 없습니다. 영상 첨부를 다시 시도해 주세요.'})

    @app.get('/health')
    def health():
        return {'status': 'ok', 'runtime': settings.runtime, 'model': settings.model,
                'reasoning_effort':'none','service_tier':'default','pricing_version':2,
                'memory_version':1,'memory_scope':'user','memory_storage':'local_sqlite',
                'analysis_version': 3, 'completion_version': 5,
                'response_modes': ['general', 'context', 'analysis', 'clarification', 'partial'],
                'precision': 'original_pts_png', 'precision_version': 4,
                'precision_time_source': 'server_frame_pts', 'max_fine_images': settings.max_fine_images,
                'match_review_version':1,'scene_coaching_version':4,'analysis_intake_version':1,
                'report_format':'five-part-v1','review_max_usd':settings.review_max_usd}

    def result_row(row):
        result=public_run(row)
        if row.get('review_id'):
            result['review']=reviews.status(row['review_id'],row['user_id'],row['chat_id'])
        return result

    @app.post('/internal/videos', dependencies=[Depends(auth)])
    async def register(body: VideoInput):
        row = await asyncio.to_thread(videos.register, body.source_path, body.source_id, body.filename, body.user_id, body.chat_id)
        return {k:row[k] for k in ['id', 'filename', 'info', 'player']}

    @app.get('/internal/videos', dependencies=[Depends(auth)])
    def list_videos(user_id: str, chat_id: str):
        return store.videos(user_id, chat_id)

    @app.post('/internal/context', dependencies=[Depends(auth)])
    def update_context(body: ContextInput):
        return {'video_ids': store.set_active_videos(body.user_id, body.chat_id, body.video_ids)}

    @app.post('/internal/runs', dependencies=[Depends(auth)])
    def submit(body: RunInput):
        from .memory import Memory,is_command
        if is_command(body.question):
            answer=Memory(store).command(body.user_id,body.chat_id,body.request_id,body.question)
            row=store.create_run(body.user_id,body.chat_id,body.request_id,body.question,[],initial_status='completed')
            store.execute("UPDATE runs SET response_kind='memory' WHERE id=?",(row['id'],))
            store.update_run(row['id'],'completed',answer=answer)
            return result_row(store.run(row['id']))
        ids = body.video_ids or store.active_videos(body.user_id, body.chat_id)
        if not body.video_ids:
            import re
            clean = re.sub(r'<attached_files>.*?</attached_files>', '', body.question, flags=re.S)
            active = [store.video(i, body.user_id, body.chat_id) for i in ids]
            exact = [v['id'] for v in active if v['filename'] in clean]
            aliases = set(re.findall(r'(?<!\d)(\d{4})(?!\d)', clean))
            named = [v['id'] for v in active if re.match(r'^20\d{6}', v['filename']) and v['filename'][4:8] in aliases]
            ids = exact or named or ids
        for video_id in ids:
            store.video(video_id, body.user_id, body.chat_id)
        row=store.create_run(body.user_id, body.chat_id, body.request_id, body.question, ids)
        store.execute("UPDATE runs SET scope=COALESCE(scope,?) WHERE id=?",(body.scope,row['id']))
        return result_row(store.run(row['id']))

    @app.get('/internal/memory',dependencies=[Depends(auth)])
    def memory_list(user_id:str,chat_id:str):
        from .memory import Memory
        memory=Memory(store)
        return {'enabled':bool(memory.settings(user_id)['enabled']),
                'items':[memory.public(row) for row in memory.rows(user_id)]}

    @app.post('/internal/memory/command',dependencies=[Depends(auth)])
    def memory_command(body:MemoryCommand):
        from .memory import Memory
        answer=Memory(store).command(body.user_id,body.chat_id,body.request_id,body.question)
        if answer is None:raise ValueError('개인 기억 관리 명령을 입력해 주세요.')
        return {'answer':answer}

    @app.get('/internal/runs/latest', dependencies=[Depends(auth)])
    def latest(user_id: str, chat_id: str):
        row = store.one("SELECT * FROM runs WHERE user_id=? AND chat_id=? AND COALESCE(response_kind,'')!='memory' ORDER BY created DESC LIMIT 1", (user_id, chat_id))
        return result_row(row) if row else None

    @app.get('/internal/runs/{run_id}', dependencies=[Depends(auth)])
    def status(run_id: str, user_id: str, chat_id: str):
        return result_row(store.run(run_id, user_id, chat_id))

    @app.get('/internal/reviews/latest',dependencies=[Depends(auth)])
    def latest_review(user_id:str,chat_id:str):
        row=store.one('SELECT * FROM reviews WHERE user_id=? AND chat_id=? ORDER BY updated DESC LIMIT 1',(user_id,chat_id))
        return {**reviews.status(row['id'],user_id,chat_id),'run_id':row['run_id']} if row else None

    @app.get('/internal/reviews/{review_id}',dependencies=[Depends(auth)])
    def review_status(review_id:str,user_id:str,chat_id:str):
        return reviews.status(review_id,user_id,chat_id)

    @app.post('/internal/reviews/{review_id}/resume',dependencies=[Depends(auth)])
    def resume_review(review_id:str,body:ResumeInput):
        row=reviews.resume(review_id,body.user_id,body.chat_id,body.additional_usd,body.request_id)
        return result_row(store.run(row['run_id'],body.user_id,body.chat_id))

    @app.get('/internal/runs/{run_id}/events', dependencies=[Depends(auth)])
    async def events(run_id: str, user_id: str, chat_id: str, after: int = Query(0, ge=0)):
        store.run(run_id, user_id, chat_id)
        async def stream():
            cursor = after
            source_run=run_id
            while True:
                current=store.run(run_id,user_id,chat_id)
                if current.get('review_id'):
                    canonical=reviews.get(current['review_id'],user_id,chat_id)['run_id']
                    store.run(canonical,user_id,chat_id)
                    if canonical!=source_run:source_run=canonical;cursor=0
                batch = store.events(source_run, cursor)
                for item in batch:
                    cursor = item['id']
                    yield f"id: {cursor}\nevent: {item['kind']}\ndata: {encode(item)}\n\n"
                if store.run(run_id)['status'] in TERMINAL and not store.events(source_run, cursor):
                    return
                if not batch:
                    yield ': heartbeat\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post('/internal/runs/{run_id}/cancel', dependencies=[Depends(auth)])
    def cancel(run_id: str, body: Identity):
        row = store.run(run_id, body.user_id, body.chat_id)
        if row['status'] not in TERMINAL:
            store.execute('UPDATE runs SET cancel=1,updated=? WHERE id=?', (time.time(), run_id))
            if row.get('review_id'):store.execute('UPDATE reviews SET cancel=1 WHERE id=?',(row['review_id'],))
        return {'id': run_id, 'cancel_requested': row['status'] not in TERMINAL}

    @app.delete('/internal/videos/{video_id}', dependencies=[Depends(auth)])
    def delete_video(video_id: str, user_id: str, chat_id: str):
        from pathlib import Path
        row = store.video(video_id, user_id, chat_id)
        active = store.all("SELECT video_ids FROM runs WHERE status IN ('queued','preparing','analyzing')")
        if any(video_id in json.loads(r['video_ids']) for r in active):
            raise HTTPException(409, '이 영상을 분석 중입니다. 작업을 취소하고 완료된 뒤 삭제해 주세요.')
        path = Path(row['path']).resolve()
        if path.is_relative_to((settings.root / 'originals').resolve()):
            path.unlink(missing_ok=True)
        store.execute('DELETE FROM observations WHERE video_id=?', (video_id,))
        from .memory import Memory
        Memory(store).forget_video(user_id,video_id)
        store.execute('DELETE FROM analysis_preferences WHERE user_id=? AND chat_id=? AND video_id=?',(user_id,chat_id,video_id))
        for review in store.all('SELECT id FROM reviews WHERE video_id=? AND user_id=? AND chat_id=?',(video_id,user_id,chat_id)):
            store.execute('DELETE FROM review_units WHERE review_id=?',(review['id'],))
            store.execute('DELETE FROM calls WHERE review_id=?',(review['id'],))
            store.execute('DELETE FROM reviews WHERE id=?',(review['id'],))
        related = store.all('SELECT id,video_ids FROM runs WHERE user_id=? AND chat_id=?', (user_id, chat_id))
        for run in related:
            if video_id in json.loads(run['video_ids']):
                store.execute('DELETE FROM events WHERE run_id=?', (run['id'],))
                store.execute('DELETE FROM calls WHERE run_id=?', (run['id'],))
                store.execute('DELETE FROM observations WHERE cache_key IN (?,?,?,?)', ('evidence:' + run['id'], 'hermes-budget:' + run['id'], 'hermes-count:' + run['id'], 'hermes-final:' + run['id']))
                store.execute('DELETE FROM runs WHERE id=?', (run['id'],))
        store.execute('DELETE FROM videos WHERE id=?', (video_id,))
        return {'deleted': video_id, 'source_id': row['source_id']}

    return app


if __name__ == '__main__':
    import uvicorn
    config = load_settings().initialize()
    uvicorn.run(create_app(config), host='127.0.0.1', port=config.port, log_level='info')
