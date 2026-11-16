import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS videos (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    source_id TEXT NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, info TEXT NOT NULL, player TEXT,
                    created REAL NOT NULL, UNIQUE(user_id, chat_id, source_id));
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    request_id TEXT NOT NULL, question TEXT NOT NULL, video_ids TEXT NOT NULL,
                    status TEXT NOT NULL, answer TEXT, error TEXT, cancel INTEGER DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(user_id, chat_id, request_id));
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS event_run ON events(run_id,id);
                CREATE TABLE IF NOT EXISTS observations (
                    cache_key TEXT PRIMARY KEY, video_id TEXT NOT NULL,
                    kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL, purpose TEXT NOT NULL,
                    status TEXT NOT NULL, usage TEXT, error TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_context (
                    user_id TEXT NOT NULL, chat_id TEXT NOT NULL, video_ids TEXT NOT NULL,
                    PRIMARY KEY(user_id,chat_id));
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(runs)')}
            if 'analysis_version' not in columns:
                db.execute('ALTER TABLE runs ADD COLUMN analysis_version INTEGER NOT NULL DEFAULT 1')
            if 'response_kind' not in columns:
                db.execute('ALTER TABLE runs ADD COLUMN response_kind TEXT')
            for name,kind in [('scope','TEXT'),('route','TEXT'),('review_id','TEXT')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE runs ADD COLUMN {name} {kind}')
            call_columns={r['name'] for r in db.execute('PRAGMA table_info(calls)')}
            for name,kind in [('review_id','TEXT'),('stage','TEXT'),('reserved_micros','INTEGER DEFAULT 0'),('actual_micros','INTEGER DEFAULT 0'),
                              ('model','TEXT'),('response_model','TEXT'),('pricing','TEXT'),('service_tier','TEXT')]:
                if name not in call_columns:
                    db.execute(f'ALTER TABLE calls ADD COLUMN {name} {kind}')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE, video_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, chat_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    status TEXT NOT NULL, phase TEXT NOT NULL, profile TEXT NOT NULL,
                    cap_micros INTEGER NOT NULL, snapshot TEXT, report TEXT, report_json TEXT,
                    cancel INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS review_units (
                    id TEXT PRIMARY KEY, review_id TEXT NOT NULL, kind TEXT NOT NULL,
                    start REAL NOT NULL, end REAL NOT NULL, position INTEGER NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    result TEXT, error TEXT, UNIQUE(review_id,kind,position));
                CREATE INDEX IF NOT EXISTS units_review ON review_units(review_id,kind,status);
                CREATE TABLE IF NOT EXISTS analysis_preferences (
                    user_id TEXT NOT NULL, chat_id TEXT NOT NULL, video_id TEXT NOT NULL,
                    profile TEXT NOT NULL, pending TEXT, updated REAL NOT NULL,
                    PRIMARY KEY(user_id,chat_id,video_id));
                CREATE TABLE IF NOT EXISTS user_memory_settings (
                    user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
                    history_cutoff REAL NOT NULL DEFAULT 0, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS user_memories (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL,
                    field TEXT NOT NULL, content TEXT NOT NULL, detail TEXT NOT NULL,
                    source_type TEXT NOT NULL, source_id TEXT NOT NULL, source_chat TEXT NOT NULL,
                    video_id TEXT, confidence TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(user_id,source_type,source_id,field));
                CREATE INDEX IF NOT EXISTS memory_user ON user_memories(user_id,status,kind,updated);
                CREATE TABLE IF NOT EXISTS memory_receipts (
                    user_id TEXT NOT NULL, chat_id TEXT NOT NULL, request_id TEXT NOT NULL,
                    answer TEXT NOT NULL, PRIMARY KEY(user_id,chat_id,request_id));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def one(self, sql, args=()):
        with self.connect() as db:
            row = db.execute(sql, args).fetchone()
            return dict(row) if row else None

    def all(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.connect() as db:
            db.execute(sql, args)

    def video(self, video_id, user_id, chat_id):
        row = self.one('SELECT * FROM videos WHERE id=? AND user_id=? AND chat_id=?', (video_id, user_id, chat_id))
        if not row:
            raise PermissionError('이 대화에서 접근할 수 없는 영상입니다.')
        row['info'] = json.loads(row['info'])
        return row

    def videos(self, user_id, chat_id):
        return self.all('SELECT id,filename,player,created FROM videos WHERE user_id=? AND chat_id=? ORDER BY created', (user_id, chat_id))

    def set_active_videos(self, user_id, chat_id, ids):
        selected = []
        by_hash = {}
        for video_id in ids:
            video = self.video(video_id, user_id, chat_id)
            if video['sha256'] in by_hash:
                selected.remove(by_hash[video['sha256']])
            selected.append(video_id)
            by_hash[video['sha256']] = video_id
        self.execute('INSERT OR REPLACE INTO chat_context VALUES(?,?,?)', (user_id, chat_id, encode(selected)))
        return selected

    def active_videos(self, user_id, chat_id):
        row = self.one('SELECT video_ids FROM chat_context WHERE user_id=? AND chat_id=?', (user_id, chat_id))
        if row:
            known = {v['id'] for v in self.videos(user_id, chat_id)}
            return [v for v in json.loads(row['video_ids']) if v in known]
        # Migrate existing chats: the newest actual attachment set supersedes earlier corrected uploads.
        for run in self.all('SELECT question FROM runs WHERE user_id=? AND chat_id=? ORDER BY created DESC', (user_id, chat_id)):
            sources = re.findall(r'<file\b[^>]*\bid="([^"]+)"', run['question'])
            ids = []
            for source in sources:
                video = self.one('SELECT id FROM videos WHERE user_id=? AND chat_id=? AND source_id=?', (user_id, chat_id, source))
                if video:
                    ids.append(video['id'])
            if ids:
                return self.set_active_videos(user_id, chat_id, ids)
        ids = [v['id'] for v in self.videos(user_id, chat_id)]
        return self.set_active_videos(user_id, chat_id, ids) if ids else []

    def create_run(self, user, chat, request, question, videos,initial_status='queued'):
        now = time.time()
        run_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO runs(id,user_id,chat_id,request_id,question,video_ids,status,created,updated,analysis_version) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (run_id, user, chat, request, question, encode(videos), initial_status, now, now, 3))
            return dict(db.execute('SELECT * FROM runs WHERE user_id=? AND chat_id=? AND request_id=?', (user, chat, request)).fetchone())

    def run(self, run_id, user=None, chat=None):
        row = self.one('SELECT * FROM runs WHERE id=?', (run_id,))
        if not row or (user is not None and (row['user_id'] != user or row['chat_id'] != chat)):
            raise PermissionError('이 대화에서 접근할 수 없는 작업입니다.')
        return row

    def update_run(self, run_id, status, answer=None, error=None):
        self.execute('UPDATE runs SET status=?,answer=?,error=?,updated=? WHERE id=?', (status, answer, error, time.time(), run_id))
        self.event(run_id, 'state', {'status': status, 'answer': answer, 'error': error})
        if status=='completed' and answer:
            try:
                from .memory import Memory
                Memory(self).index_run(run_id)
            except Exception as exc:
                # A memory-index failure must not discard completed coaching.
                self.event(run_id,'memory_error',{'type':type(exc).__name__})

    def event(self, run_id, kind, data):
        self.execute('INSERT INTO events(run_id,kind,data,created) VALUES(?,?,?,?)', (run_id, kind, encode(data), time.time()))

    def events(self, run_id, after=0):
        rows = self.all('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 200', (run_id, after))
        for row in rows:
            row['data'] = json.loads(row['data'])
        return rows

    def cached(self, key):
        row = self.one('SELECT data FROM observations WHERE cache_key=?', (key,))
        return json.loads(row['data']) if row else None

    def cache(self, key, video_id, kind, data):
        self.execute('INSERT OR REPLACE INTO observations VALUES(?,?,?,?,?)', (key, video_id, kind, encode(data), time.time()))

    def recover(self):
        # An interrupted provider call can have been billed. Never replay it silently.
        self.execute("UPDATE calls SET status='unknown' WHERE status='running'")
        self.execute("UPDATE calls SET status='failed' WHERE status='reserved'")
        self.execute("UPDATE reviews SET status='paused_interrupted' WHERE status='running'")
        self.execute("UPDATE review_units SET status='unknown',error='interrupted' WHERE status='running'")
        rows = self.all("SELECT id FROM runs WHERE status IN ('preparing','analyzing')")
        for row in rows:
            self.update_run(row['id'], 'failed', error='PC 또는 서비스가 중단되었습니다. 완료된 장면은 보관했습니다. 새 질문으로 재개할 수 있습니다.')
