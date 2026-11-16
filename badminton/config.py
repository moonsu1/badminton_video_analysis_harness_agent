import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from .pricing import DEFAULT_MODEL


@dataclass
class Settings:
    root: Path
    model: str = DEFAULT_MODEL
    port: int = 8765
    max_bytes: int = 4 * 1024**3
    max_seconds: int = 3600
    max_tools: int = 20
    max_fine_images: int = 240
    review_max_usd: float = 3.5
    review_summary_reserve: float = .20
    review_safety_reserve: float = .05
    review_refine_max_usd: float = .60
    review_coaching_reserve: float = .08
    runtime: str = 'native'
    hermes_url: str = 'http://127.0.0.1:8642'

    @property
    def webui(self):
        return self.root / 'webui'

    @property
    def token(self):
        return (self.root / 'service.key').read_text().strip()

    def initialize(self):
        for name in ['originals', 'cache', 'logs', 'backups', 'webui/uploads']:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        if not (self.root / 'service.key').exists():
            (self.root / 'service.key').write_text(secrets.token_urlsafe(48))
        return self


def load_settings():
    root = Path(os.getenv('BADMINTON_DATA_DIR', str(Path(os.environ['LOCALAPPDATA']) / 'BadmintonAgent'))).resolve()
    path = root / 'settings.json'
    values = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    allowed = {'model', 'port', 'max_bytes', 'max_seconds', 'max_tools', 'max_fine_images', 'runtime', 'hermes_url',
               'review_max_usd','review_summary_reserve','review_safety_reserve','review_refine_max_usd','review_coaching_reserve'}
    return Settings(root, **{k: v for k, v in values.items() if k in allowed})


def api_key():
    import keyring
    value = os.getenv('OPENAI_API_KEY') or keyring.get_password('BadmintonAgent', 'openai_api_key') or keyring.get_password('CareerScout', 'openai_api_key')
    if not value:
        raise RuntimeError('OpenAI API 키가 없습니다. 인증등록.cmd에서 등록해 주세요.')
    return value
