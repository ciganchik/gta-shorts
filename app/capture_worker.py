"""Separate dyno process: ffmpeg recording + highlight polling in SAME dyno.

Do not scale this process unless an explicitly permitted media source and
persistent S3-compatible bucket have been configured. No Twitch URL scraping.
"""
import datetime as dt
import logging
import os
import subprocess
import time
from pathlib import Path
from .core import connect, init_db
from .capture import (UTC, claim_highlights, create_clip, delete_old_segments,
                      ffmpeg_record_cmd, finish_clip, init_capture_db,
                      require_authorized_capture, upload_clip)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('capture-worker')


def main():
    require_authorized_capture()
    init_db(); init_capture_db(connect)
    channel = os.environ['CAPTURE_CHANNEL'].strip().lower()
    source_url = os.environ['CAPTURE_SOURCE_URL']
    bucket = os.environ['S3_BUCKET']
    endpoint = os.getenv('S3_ENDPOINT_URL') or None
    offset = float(os.getenv('CAPTURE_CLOCK_OFFSET_SECONDS', '0'))
    workdir = Path(os.getenv('CAPTURE_WORK_DIR', '/tmp/gta_capture'))
    segments = workdir / 'segments'
    exports = workdir / 'exports'
    cmd = ffmpeg_record_cmd(source_url, segments)
    started = dt.datetime.now(UTC)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info('Authorized capture started for %s at %s', channel, started.isoformat())
    try:
        while proc.poll() is None:
            delete_old_segments(segments, retain_seconds=190)
            events = claim_highlights(connect, channel, since=started + dt.timedelta(seconds=20),
                                      min_age_seconds=45)
            for clip_id, event_at in events:
                output = exports / f'{channel}_{clip_id}.mp4'
                try:
                    create_clip(segments, output, event_at, pre=20, post=40,
                                offset_seconds=offset)
                    location = upload_clip(output, bucket,
                                           f'clips/{channel}/{clip_id}.mp4', endpoint=endpoint)
                    finish_clip(connect, clip_id, 'uploaded', location)
                    log.info('Exported %s', location)
                except Exception as exc:
                    log.exception('Capture failed for %s', clip_id)
                    finish_clip(connect, clip_id, 'failed', error=str(exc))
                finally:
                    output.unlink(missing_ok=True)
            time.sleep(3)
        raise RuntimeError(f'ffmpeg stopped unexpectedly with exit code {proc.returncode}')
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill()

if __name__ == '__main__':
    main()
