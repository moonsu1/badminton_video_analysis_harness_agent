"""Run while Open WebUI is stopped: migrate data and install reviewed local plugins."""
import asyncio
import os
import secrets
import shutil
import time
from pathlib import Path

from .config import load_settings

PROJECT = Path(__file__).resolve().parent.parent


def environment(settings):
    env = os.environ.copy()
    secret = settings.root / 'webui.secret'
    if not secret.exists():
        legacy = PROJECT / '.webui_secret_key'
        secret.write_text(legacy.read_text() if legacy.exists() else secrets.token_urlsafe(48))
        if legacy.exists():
            legacy.unlink()
    env.update({
        'BADMINTON_DATA_DIR': str(settings.root), 'DATA_DIR': str(settings.webui),
        'FROM_INIT_PY': 'true', 'STATIC_DIR': str(settings.webui / 'static'),
        'WEBUI_SECRET_KEY': secret.read_text().strip(), 'PYTHONUTF8': '1',
        'DO_NOT_TRACK': 'true', 'ANONYMIZED_TELEMETRY': 'false',
        'ENABLE_OLLAMA_API': 'false', 'ENABLE_OPENAI_API': 'false',
        'ENABLE_SIGNUP': 'false', 'WEBUI_AUTH': 'true',
        'DEFAULT_MODELS': 'badminton_coach', 'RAG_FILE_MAX_SIZE': '4096',
        'DEFAULT_LOCALE': 'ko-KR',
        'RAG_EMBEDDING_ENGINE': 'openai', 'RAG_EMBEDDING_MODEL_AUTO_UPDATE': 'false',
        'WHISPER_MODEL_AUTO_UPDATE': 'false', 'ENABLE_VERSION_UPDATE_CHECK': 'false',
        'HF_HUB_OFFLINE': '1', 'CORS_ALLOW_ORIGIN': 'http://127.0.0.1:8080;http://localhost:8080',
        'UVICORN_WORKERS': '1',
        'BADMINTON_SERVICE_URL': f'http://127.0.0.1:{settings.port}',
    })
    remote = settings.root / 'remote-url.txt'
    if remote.exists():
        url = remote.read_text().strip()
        env['WEBUI_URL'] = url
        env['CORS_ALLOW_ORIGIN'] += ';' + url
    return env


async def install_plugins(settings):
    from open_webui.models.functions import Functions, FunctionForm
    from open_webui.models.models import Models, ModelForm
    from open_webui.models.config import Config
    from open_webui.models.users import Users
    first = await Users.get_first_user()
    owner = first.id if first else ''
    for function_id, kind, filename, name in [
        ('badminton_video_bridge', 'filter', 'openwebui_video_filter.py', '배드민턴 영상 첨부'),
        ('badminton_coach', 'pipe', 'openwebui_coach_pipe.py', '배드민턴 코치'),
    ]:
        content = (PROJECT / filename).read_text(encoding='utf-8')
        existing = await Functions.get_function_by_id(function_id)
        if not existing:
            await Functions.insert_new_function(owner, kind, FunctionForm(id=function_id, name=name, content=content, meta={'description': name}))
        await Functions.update_function_by_id(function_id, {'content': content, 'name': name, 'is_active': True, 'is_global': False}, user_id=owner)
    form = ModelForm(id='badminton_coach', name='배드민턴 코치', meta={
        'description': 'GPT-6 Luna 기반입니다. 새 채팅에서도 개인 프로필·이전 경기의 코칭·연습 경과를 참고합니다. 기억목록·기억수정·기억삭제·기억끄기를 사용할 수 있습니다. 경기 운영·전술 또는 기술·자세 중 원하는 분석을 알려 주세요. 최초 전체 리뷰 상한은 USD 3.5입니다.',
        'filterIds': ['badminton_video_bridge'],
        'capabilities': {'file_upload': True, 'vision': False, 'file_context': False, 'web_search': False, 'image_generation': False, 'code_interpreter': False},
        'suggestion_prompts': [{'content':'경기 영상이야. 흰색 상의가 나야. 전체 특징,내 수준,잘한 점,아쉬운 점,개선 계획을 리포트로 분석해줘.'},
                               {'content':'이 경기에서 내 스매시 타점과 자세를 봐줘.'},
                               {'content':'개인 강습 영상이야. 내가 배운 동작을 어떻게 수행하고 무엇을 교정하면 좋을지 분석해줘.'},
                               {'content':'스매시의 기본 그립을 설명해줘.'}],
    }, params={})
    if await Models.get_model_by_id('badminton_coach'):
        await Models.update_model_by_id('badminton_coach', form, user_id=owner)
    else:
        await Models.insert_new_model(form, owner)
    await Config.upsert({'ui.default_models': 'badminton_coach', 'ui.default_locale': 'ko-KR', 'ui.enable_signup': False,
                         'openai.enable': False, 'ollama.enable': False, 'rag.file.max_size': 4096,
                         'rag.embedding_engine': 'openai'})


def main():
    settings = load_settings().initialize()
    source = (PROJECT / 'data').resolve()
    if source.exists() and (source / 'webui.db').exists() and not (settings.webui / 'webui.db').exists():
        destination = (settings.root / 'backups' / ('legacy-webui-' + str(int(time.time())))).resolve()
        assert source.parent == PROJECT.resolve()
        assert destination.is_relative_to(settings.root.resolve() / 'backups')
        shutil.move(str(source), str(destination))
        shutil.copytree(destination, settings.webui, dirs_exist_ok=True)
    os.environ.update(environment(settings))
    import open_webui.config  # Initializes the upstream schema before registering plugins.
    asyncio.run(install_plugins(settings))
    print('Open WebUI plugins and local data are ready.')


if __name__ == '__main__':
    main()
