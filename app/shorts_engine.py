"""On-demand Shorts render: PostgreSQL queue, private R2, FFmpeg worker.

The dedicated render dyno handles work. Web endpoints do not download or transcode video.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import boto3
from botocore.config import Config
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .core import connect
from .clip_library import require_library_admin, decode_storage_key, select_clip

log = logging.getLogger(__name__)
router = APIRouter()


def r2_client():
    endpoint = os.getenv('S3_ENDPOINT_URL')
    bucket = os.getenv('S3_BUCKET')
    if not endpoint or not bucket:
        raise RuntimeError('R2 endpoint/bucket not configured')
    return boto3.client('s3', endpoint_url=endpoint, config=Config(signature_version='s3v4')), bucket


def initialize():
    from .capture import init_capture_db
    init_capture_db(connect)
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS shorts_jobs (
          id BIGSERIAL PRIMARY KEY,
          clip_id BIGINT NOT NULL UNIQUE REFERENCES clips(id),
          channel TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued',
          storage_key TEXT,
          error_message TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )''')
        db.execute('''CREATE INDEX IF NOT EXISTS shorts_jobs_status_id ON shorts_jobs(status,id)''')


def valid_output_key(key: str, channel: str, clip_id: int) -> bool:
    return bool(re.fullmatch(r'^[a-z0-9_]{3,25}$', channel) and
                key == f'shorts/{channel}/{clip_id}.mp4')


def job_for_clip(clip_id: int):
    with connect() as db:
        return db.execute('SELECT * FROM shorts_jobs WHERE clip_id=%s', (clip_id,)).fetchone()


@router.post('/api/clips/{clip_id}/shorts')
async def queue_shorts(request: Request, clip_id: int):
    require_library_admin(request)
    body = await request.json()
    if body.get('permission_confirmed') is not True:
        raise HTTPException(400, 'Confirm rights to make a derivative Shorts video')
    clip = select_clip(clip_id)
    if not clip or clip['status'] != 'uploaded':
        raise HTTPException(404, 'Uploaded original clip not found')
    try:
        client, bucket = r2_client()
        decode_storage_key(clip['storage_key'], bucket, clip['channel'])
    except (RuntimeError, ValueError):
        raise HTTPException(409, 'Invalid original clip location')
    # UPSERT is deliberately idempotent. A completed render is never overwritten.
    with connect() as db:
        row = db.execute('''INSERT INTO shorts_jobs(clip_id,channel,status)
             VALUES (%s,%s,'queued')
             ON CONFLICT(clip_id) DO UPDATE
             SET status=CASE WHEN shorts_jobs.status='failed' THEN 'queued' ELSE shorts_jobs.status END,
                 error_message=CASE WHEN shorts_jobs.status='failed' THEN NULL ELSE shorts_jobs.error_message END,
                 updated_at=now()
             RETURNING id,status''', (clip_id,clip['channel'])).fetchone()
    return {'job_id':row['id'],'status':row['status'],'clip_id':clip_id}


@router.get('/api/clips/{clip_id}/shorts')
def shorts_status(request: Request, clip_id: int):
    require_library_admin(request)
    row=job_for_clip(clip_id)
    if not row:
        return {'clip_id':clip_id,'status':'not_created'}
    return {'clip_id':clip_id,'status':row['status'],'error':row['error_message']}


@router.get('/api/clips/{clip_id}/shorts/play')
def shorts_play(request:Request,clip_id:int):
    return shorts_link(request,clip_id,False)


@router.get('/api/clips/{clip_id}/shorts/download')
def shorts_download(request:Request,clip_id:int):
    return shorts_link(request,clip_id,True)


def shorts_link(request,clip_id,download):
    require_library_admin(request)
    row=job_for_clip(clip_id)
    if not row or row['status'] != 'uploaded':
        raise HTTPException(404,'Shorts not ready')
    if not valid_output_key(row['storage_key'] or '',row['channel'],clip_id):
        raise HTTPException(409,'Invalid stored Shorts key')
    client,bucket=r2_client()
    params={'Bucket':bucket,'Key':row['storage_key']}
    if download:
        params['ResponseContentDisposition']=f'attachment; filename="{row["channel"]}_{clip_id}_shorts.mp4"'
    return RedirectResponse(client.generate_presigned_url('get_object', Params=params, ExpiresIn=300),
                            headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})


def render_cmd(source:Path,output:Path)->list[str]:
    """Preserve all gameplay/facecam using a blurred full-bleed background.

    720x1280/30fps limits CPU and memory pressure on initial Basic dyno tests.
    """
    filter_graph=(
        '[0:v]fps=30,split=2[bg][fg];'
        '[bg]scale=720:1280:force_original_aspect_ratio=increase,'
        'crop=720:1280,boxblur=12:2[blur];'
        '[fg]scale=720:-2[front];'
        '[blur][front]overlay=(W-w)/2:(H-h)/2:shortest=1,format=yuv420p[v]'
    )
    return ['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-y',
            '-i',str(source),'-filter_complex',filter_graph,
            '-map','[v]','-map','0:a:0?',
            '-c:v','libx264','-preset','veryfast','-crf','20','-threads','2',
            '-pix_fmt','yuv420p','-c:a','aac','-b:a','160k',
            '-movflags','+faststart',str(output)]


def claim_job():
    with connect() as db:
        row=db.execute('''SELECT id,clip_id,channel FROM shorts_jobs
              WHERE status='queued' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED''').fetchone()
        if row:
            db.execute("UPDATE shorts_jobs SET status='processing',updated_at=now() WHERE id=%s", (row['id'],))
        return row


def update_job(job_id,status,storage_key=None,error=None):
    with connect() as db:
        db.execute('''UPDATE shorts_jobs SET status=%s,storage_key=%s,
              error_message=%s,updated_at=now() WHERE id=%s''',
                   (status,storage_key,(error or '')[:900] or None,job_id))


def process_job(job):
    clip=select_clip(job['clip_id'])
    if not clip or clip['status']!='uploaded' or clip['channel']!=job['channel']:
        raise RuntimeError('Original clip record is unavailable or mismatched')
    client,bucket=r2_client()
    source_key=decode_storage_key(clip['storage_key'],bucket,clip['channel'])
    key=f'shorts/{clip["channel"]}/{clip["id"]}.mp4'
    with tempfile.TemporaryDirectory(prefix='shorts_') as tmp:
        source=Path(tmp)/'original.mp4'
        output=Path(tmp)/'shorts.mp4'
        client.download_file(bucket,source_key,str(source))
        if source.stat().st_size == 0:
            raise RuntimeError('Empty original MP4')
        from .shorts_v4 import render_v4
        log.info('Rendering V4 clip id=%s',job['clip_id'])
        render_v4(source,output,Path(tmp))
        if not output.is_file() or output.stat().st_size < 1024:
            raise RuntimeError('Rendered MP4 missing/empty')
        client.upload_file(str(output),bucket,key,ExtraArgs={'ContentType':'video/mp4'})
        log.info('Shorts uploaded %s',key)
    return key


def main():
    import time
    logging.basicConfig(level=logging.INFO)
    if not shutil.which('ffmpeg'):
        raise RuntimeError('FFmpeg not found')
    initialize()
    # A previous render dyno could have been restarted mid-encode. Make those jobs retryable.
    with connect() as db:
        db.execute("""UPDATE shorts_jobs SET status='failed',
            error_message='Рендер был прерван перезапуском. Нажми Повторить Shorts.',
            updated_at=now() WHERE status='processing'""")
    log.info('Shorts render worker ready')
    while True:
        job=claim_job()
        if not job:
            time.sleep(5)
            continue
        try:
            key=process_job(job)
            update_job(job['id'],'uploaded',storage_key=key)
        except Exception as exc:
            log.exception('Shorts job failed id=%s',job['id'])
            update_job(job['id'],'failed',error=str(exc))


if __name__=='__main__':
    main()
