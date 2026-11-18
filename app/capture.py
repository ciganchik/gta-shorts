"""Stage 2: authorized stream capture, event-driven clips and object-store export.

This module intentionally does not scrape Twitch or bypass access controls.
Provide a direct media source you are authorized to record, along with bucket credentials.
The capture worker and its temporary buffer MUST run in the same dyno/process.
"""
from __future__ import annotations
import datetime as dt
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger('capture')
UTC = dt.timezone.utc
SEGMENT_PATTERN = re.compile(r'^seg_(\d{8}T\d{6})\.ts$')


def parse_segment_time(path: Path) -> dt.datetime | None:
    match = SEGMENT_PATTERN.fullmatch(path.name)
    if not match:
        return None
    return dt.datetime.strptime(match.group(1), '%Y%m%dT%H%M%S').replace(tzinfo=UTC)


def segment_candidates(folder: Path, start: dt.datetime, end: dt.datetime, margin: int = 4) -> list[Path]:
    """Find segments by UTC wall-clock filenames, including alignment margin."""
    lower, upper = start - dt.timedelta(seconds=margin), end + dt.timedelta(seconds=margin)
    result = [(t, p) for p in folder.glob('seg_*.ts') if (t := parse_segment_time(p)) and lower <= t <= upper]
    return [path for _, path in sorted(result)]


def delete_old_segments(folder: Path, retain_seconds: int = 180, now: dt.datetime | None = None):
    cutoff = (now or dt.datetime.now(UTC)) - dt.timedelta(seconds=retain_seconds)
    for p in folder.glob('seg_*.ts'):
        t = parse_segment_time(p)
        if t and t < cutoff:
            p.unlink(missing_ok=True)


def ffmpeg_record_cmd(input_url: str, folder: Path, segment_seconds: int = 2) -> list[str]:
    if not input_url or not input_url.startswith(('https://', 'http://', 'file:')):
        raise ValueError('CAPTURE_SOURCE_URL must be an authorized HTTP(S) media URL or file: test input')
    folder.mkdir(parents=True, exist_ok=True)
    return [
        'ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'warning', '-y',
        '-i', input_url, '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
        '-f', 'segment', '-segment_time', str(segment_seconds),
        '-segment_atclocktime', '1', '-strftime', '1', '-reset_timestamps', '1',
        str(folder / 'seg_%Y%m%dT%H%M%S.ts'),
    ]


def create_clip(folder: Path, output: Path, event_at: dt.datetime, pre: int = 20, post: int = 40,
                offset_seconds: float = 0.0, run=subprocess.run) -> Path:
    """Produce MP4 around UTC event, retaining original aspect ratio and audio.

    Time boundaries are approximate: source delays, GOP and TS segment alignment
    should be calibrated during a real stream. Existing files only; no fetch fallback.
    """
    if event_at.tzinfo is None:
        raise ValueError('event_at must be timezone aware')
    corrected = event_at.astimezone(UTC) + dt.timedelta(seconds=offset_seconds)
    start, end = corrected - dt.timedelta(seconds=pre), corrected + dt.timedelta(seconds=post)
    paths = segment_candidates(folder, start, end)
    if len(paths) < 5:
        raise RuntimeError('Not enough buffered segments to create this clip')
    first = parse_segment_time(paths[0])
    seek = max(0.0, (start-first).total_seconds())
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.txt', delete=False) as manifest:
        manifest_path = Path(manifest.name)
        for path in paths:
            manifest.write("file '" + str(path.resolve()).replace("'", "'\\''") + "'\n")
    try:
        cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'error', '-y',
               '-f', 'concat', '-safe', '0', '-i', str(manifest_path),
               '-ss', f'{seek:.3f}', '-t', str(pre + post),
               '-map', '0:v:0', '-map', '0:a?', '-c:v', 'libx264',
               '-preset', 'veryfast', '-crf', '23', '-c:a', 'aac', '-b:a', '128k',
               '-movflags', '+faststart', str(output)]
        run(cmd, check=True, capture_output=True)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError('FFmpeg completed without a usable MP4')
        return output
    finally:
        manifest_path.unlink(missing_ok=True)


def upload_clip(path: Path, bucket: str, key: str, endpoint: str | None = None) -> str:
    """S3-compatible upload (including R2); never exposes an untrusted public link."""
    import boto3
    kwargs = {'endpoint_url': endpoint} if endpoint else {}
    client = boto3.client('s3', **kwargs)
    client.upload_file(str(path), bucket, key, ExtraArgs={'ContentType': 'video/mp4'})
    return f's3://{bucket}/{key}'


def init_capture_db(connect):
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS clips (
          id BIGSERIAL PRIMARY KEY, highlight_id BIGINT UNIQUE NOT NULL,
          channel TEXT NOT NULL, event_at TIMESTAMPTZ NOT NULL,
          status TEXT NOT NULL, storage_key TEXT,
          error_message TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')


def claim_highlights(connect, channel: str, since: dt.datetime, min_age_seconds: int = 45, limit: int = 10):
    """Exactly-once claim through PK of highlight_id, no old events before recording."""
    now = dt.datetime.now(UTC)
    with connect() as db:
        rows = db.execute('''SELECT h.id, h.event_at FROM highlights h
          LEFT JOIN clips c ON c.highlight_id=h.id
          WHERE h.channel=%s AND h.event_at>=%s AND h.event_at<=%s
            AND c.id IS NULL ORDER BY h.event_at ASC LIMIT %s''',
          (channel, since, now-dt.timedelta(seconds=min_age_seconds), limit)).fetchall()
        claimed=[]
        for row in rows:
            created = db.execute('''INSERT INTO clips(highlight_id,channel,event_at,status)
                   VALUES (%s,%s,%s,'processing') ON CONFLICT (highlight_id) DO NOTHING
                   RETURNING id''', (row['id'], channel, row['event_at'])).fetchone()
            if created:
                claimed.append((created['id'], row['event_at']))
        return claimed


def finish_clip(connect, clip_id: int, status: str, location: str | None = None, error: str | None = None):
    with connect() as db:
        db.execute('UPDATE clips SET status=%s, storage_key=%s, error_message=%s WHERE id=%s',
                   (status, location, (error or '')[:1000] or None, clip_id))


def require_authorized_capture():
    if os.getenv('CAPTURE_PERMISSION_CONFIRMED', '').lower() != 'true':
        raise RuntimeError('Set CAPTURE_PERMISSION_CONFIRMED=true only with a channel owner agreement')
    for name in ['CAPTURE_SOURCE_URL', 'CAPTURE_CHANNEL', 'S3_BUCKET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY']:
        if not os.getenv(name):
            raise RuntimeError(f'Missing {name}; capture disabled')
    if not shutil.which('ffmpeg'):
        raise RuntimeError('ffmpeg missing; install with an apt buildpack before running video dyno')
