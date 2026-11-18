"""Authorized capture worker: Twitch via Streamlink -> FFmpeg, or direct media URL.

Runs Streamlink and FFmpeg in the same dyno, so the TS ring buffer is local.
"""
import datetime as dt
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .core import connect, init_db
from .capture import (
    UTC, claim_highlights, create_clip, delete_old_segments,
    ffmpeg_record_cmd, finish_clip, init_capture_db,
    require_authorized_capture, upload_clip,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('capture-worker')


def twitch_channel_from_url(url: str) -> str | None:
    """Return a channel name only for a simple twitch.tv/channel URL."""
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.netloc.lower() not in ('twitch.tv', 'www.twitch.tv'):
        return None
    pieces = [part for part in parsed.path.split('/') if part]
    if len(pieces) != 1 or parsed.query or parsed.fragment:
        return None
    name = pieces[0].lower()
    import re
    return name if re.fullmatch(r'[a-z0-9_]{3,25}', name) else None


def start_recorders(source_url: str, channel: str, segments: Path, logdir: Path):
    """Return (streamlink_proc or None, ffmpeg_proc, opened_log_files)."""
    logdir.mkdir(parents=True, exist_ok=True)
    logs = []
    streamlink = None
    twitch_channel = twitch_channel_from_url(source_url)
    if twitch_channel:
        if twitch_channel != channel:
            raise ValueError('CAPTURE_SOURCE_URL channel must equal CAPTURE_CHANNEL')
        if not shutil.which('streamlink'):
            raise RuntimeError('streamlink command missing; add Streamlink to requirements.txt')
        streamlink_log = open(logdir / 'streamlink.log', 'ab', buffering=0)
        logs.append(streamlink_log)
        # --stdout sends the actual MPEG-TS bytes over a pipe; no playback window.
        # Stream quality defaults to best; use CAPTURE_QUALITY to override if needed.
        quality = os.getenv('CAPTURE_QUALITY', 'best')
        streamlink = subprocess.Popen(
            ['streamlink', '--stdout', source_url, quality],
            stdout=subprocess.PIPE, stderr=streamlink_log,
        )
        # FFmpeg consumes Streamlink's bytes, rather than the Twitch HTML page.
        cmd = ffmpeg_record_cmd('file:pipe:0', segments)
        cmd[cmd.index('file:pipe:0')] = 'pipe:0'
        ffmpeg_log = open(logdir / 'ffmpeg.log', 'ab', buffering=0)
        logs.append(ffmpeg_log)
        try:
            ffmpeg = subprocess.Popen(
                cmd,
                stdin=streamlink.stdout,
                stdout=subprocess.DEVNULL,
                stderr=None,
            )
        except Exception:
            streamlink.terminate()
            streamlink.wait(timeout=10)
            for h in logs: h.close()
            raise
        # Crucial: close parent's copy so FFmpeg receives EOF when Streamlink exits.
        streamlink.stdout.close()
    else:
        if source_url.startswith('https://') and urlparse(source_url).netloc.lower().endswith('twitch.tv'):
            raise ValueError('Expected https://www.twitch.tv/CHANNEL, without additional path/query')
        ffmpeg_log = open(logdir / 'ffmpeg.log', 'ab', buffering=0)
        logs.append(ffmpeg_log)
        cmd = ffmpeg_record_cmd(source_url, segments)
        ffmpeg = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=ffmpeg_log)
    return streamlink, ffmpeg, logs


def stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main():
    require_authorized_capture()
    init_db()
    init_capture_db(connect)
    channel = os.environ['CAPTURE_CHANNEL'].strip().lower()
    source_url = os.environ['CAPTURE_SOURCE_URL'].strip()
    bucket = os.environ['S3_BUCKET']
    endpoint = os.getenv('S3_ENDPOINT_URL') or None
    offset = float(os.getenv('CAPTURE_CLOCK_OFFSET_SECONDS', '0'))
    workdir = Path(os.getenv('CAPTURE_WORK_DIR', '/tmp/gta_capture'))
    segments = workdir / 'segments'
    exports = workdir / 'exports'
    segments.mkdir(parents=True, exist_ok=True)
    exports.mkdir(parents=True, exist_ok=True)
    streamlink, ffmpeg, opened_logs = start_recorders(source_url, channel, segments, workdir / 'logs')
    started = dt.datetime.now(UTC)
    log.info('Authorized capture started for %s at %s', channel, started.isoformat())
    try:
        while ffmpeg.poll() is None:
            if streamlink is not None and streamlink.poll() is not None:
                raise RuntimeError(f'Streamlink stopped (exit {streamlink.returncode}); check streamlink.log')
            delete_old_segments(segments, retain_seconds=190)
            events = claim_highlights(connect, channel,
                                      since=started + dt.timedelta(seconds=20),
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
        raise RuntimeError(f'FFmpeg stopped unexpectedly (exit {ffmpeg.returncode}); check ffmpeg.log')
    finally:
        stop_process(ffmpeg)
        stop_process(streamlink)
        for handle in opened_logs:
            handle.close()


if __name__ == '__main__':
    main()
