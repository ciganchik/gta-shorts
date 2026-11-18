import os, json, re, statistics, datetime as dt

DB = os.getenv('DATABASE_URL', '')

def connect():
    if not DB:
        raise RuntimeError('Set DATABASE_URL (Heroku Postgres)')
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DB, row_factory=dict_row)

def init_db():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS settings (key text PRIMARY KEY, value text NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS chat_samples (
          id bigserial PRIMARY KEY, channel text NOT NULL,
          event_at timestamptz NOT NULL DEFAULT now(), user_id text NOT NULL,
          message text NOT NULL)''')
        db.execute('''CREATE INDEX IF NOT EXISTS chat_samples_channel_time ON chat_samples(channel,event_at DESC)''')
        db.execute('''CREATE TABLE IF NOT EXISTS highlights (
          id bigserial PRIMARY KEY, channel text NOT NULL, event_at timestamptz NOT NULL,
          count_10s integer NOT NULL, baseline numeric NOT NULL,
          unique_users integer NOT NULL, score numeric NOT NULL,
          reason text NOT NULL, status text NOT NULL DEFAULT 'candidate',
          created_at timestamptz NOT NULL DEFAULT now())''')
        db.execute('''CREATE INDEX IF NOT EXISTS highlights_channel_time ON highlights(channel,event_at DESC)''')

def put_setting(key, value):
    with connect() as db:
        db.execute('INSERT INTO settings VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value', (key,value))

def get_setting(key, default=None):
    with connect() as db:
        row=db.execute('SELECT value FROM settings WHERE key=%s',(key,)).fetchone()
    return row['value'] if row else default

def normalize_channel(raw):
    raw = raw.strip().strip('/')
    raw = re.sub(r'^https?://(www\.)?twitch\.tv/','',raw,flags=re.I)
    if not re.fullmatch(r'[a-zA-Z0-9_]{3,25}', raw):
        raise ValueError('Enter channel name or twitch.tv/channel URL')
    return raw.lower()

class SpikeDetector:
    """Per-channel rate detector: compare 10-second count with median of earlier 10s buckets."""
    def __init__(self, threshold=3.0, min_count=8, cooldown_seconds=75):
        self.threshold=threshold
        self.min_count=min_count
        self.cooldown_seconds=cooldown_seconds

    def evaluate(self, buckets, last_detected_at=None):
        # buckets: ordered list of (timestamp, {user_ids}); newest bucket is candidate
        if len(buckets)<7: return None
        now, users=buckets[-1]
        previous=buckets[-31:-1]
        baseline=max(1,statistics.median(len(x[1]) for x in previous))
        rate=len(users)
        unique=len(set(users))
        if rate<self.min_count or rate/baseline<self.threshold or unique<3: return None
        if last_detected_at and (now-last_detected_at).total_seconds()<self.cooldown_seconds: return None
        score=round(min(100,35*(rate/baseline)+min(unique,30)),1)
        return {'event_at':now,'count_10s':rate,'baseline':baseline,'unique_users':unique,'score':score,
                'reason':f'Chat spike: {rate} messages/10s, baseline {baseline:.1f}, unique users {unique}'}
