"""Private R2 clip library: authenticated metadata and short-lived signed reads.

No R2 object is made public. Existing `clips` rows are the authoritative index.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import quote

import boto3
from botocore.config import Config
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from .core import connect

router = APIRouter()
_CHANNEL_RE = re.compile(r'^[a-z0-9_]{3,25}$')


def require_library_admin(request: Request):
    # Import at call-time to avoid circular imports when web.py includes this router.
    from .web import require_admin
    require_admin(request)


def r2_client():
    endpoint = os.environ.get('S3_ENDPOINT_URL')
    bucket = os.environ.get('S3_BUCKET')
    if not endpoint or not bucket:
        raise HTTPException(503, 'Cloudflare R2 is not configured')
    return boto3.client('s3', endpoint_url=endpoint,
                        config=Config(signature_version='s3v4')), bucket


def decode_storage_key(uri: str, configured_bucket: str, channel: str) -> str:
    """Accept only our app's own clip locations, not arbitrary S3 objects."""
    prefix = f's3://{configured_bucket}/clips/{channel}/'
    if not uri or not uri.startswith(prefix):
        raise ValueError('Clip path does not match its channel or bucket')
    key = uri[len(f's3://{configured_bucket}/'):]
    # Current exporter writes numeric clip IDs under clips/channel/, never nested paths.
    if not re.fullmatch(rf'clips/{re.escape(channel)}/[0-9]+\.mp4', key):
        raise ValueError('Unrecognized clip key format')
    return key


def select_clip(clip_id: int):
    with connect() as db:
        return db.execute('''SELECT c.id,c.channel,c.event_at,c.created_at,c.status,
                                  c.storage_key,c.error_message,
                                  h.score,h.reason
                           FROM clips c LEFT JOIN highlights h ON h.id=c.highlight_id
                           WHERE c.id=%s''', (clip_id,)).fetchone()


@router.get('/api/clips')
def list_clips(request: Request, channel: str = '', limit: int = 50, offset: int = 0):
    require_library_admin(request)
    if channel and not _CHANNEL_RE.fullmatch(channel.lower()):
        raise HTTPException(400, 'Invalid channel')
    if not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        raise HTTPException(400, 'Invalid pagination')
    args = []
    where = "WHERE c.status='uploaded'"
    if channel:
        where += ' AND c.channel=%s'
        args.append(channel.lower())
    with connect() as db:
        rows = db.execute(f'''SELECT c.id,c.channel,c.event_at,c.created_at,
                              h.score,h.reason
                              FROM clips c LEFT JOIN highlights h ON h.id=c.highlight_id
                              {where} ORDER BY c.created_at DESC,c.id DESC
                              LIMIT %s OFFSET %s''', (*args, limit, offset)).fetchall()
        channels = db.execute('''SELECT DISTINCT channel FROM clips
                                 WHERE status='uploaded' ORDER BY channel''').fetchall()
    return {
        'items': [{'id':row['id'], 'channel':row['channel'],
                   'event_at':row['event_at'].isoformat(),
                   'created_at':row['created_at'].isoformat(),
                   'score':float(row['score']) if row['score'] is not None else None,
                   'reason':row['reason']} for row in rows],
        'channels':[row['channel'] for row in channels],
        'has_more':len(rows)==limit,
    }


def signed_clip_link(request: Request, clip_id: int, download: bool):
    require_library_admin(request)
    row = select_clip(clip_id)
    if not row or row['status'] != 'uploaded':
        raise HTTPException(404, 'Uploaded clip not found')
    client, bucket = r2_client()
    try:
        key = decode_storage_key(row['storage_key'], bucket, row['channel'])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    params = {'Bucket':bucket,'Key':key}
    if download:
        # Keep download filename ASCII safe; channel and clip_id are validated.
        params['ResponseContentDisposition'] = f'attachment; filename="{row["channel"]}_{clip_id}.mp4"'
    try:
        link = client.generate_presigned_url('get_object', Params=params,
                                              ExpiresIn=300, HttpMethod='GET')
    except Exception as exc:
        raise HTTPException(502, 'Cannot sign R2 access URL') from exc
    # Never allow the browser or reverse proxy to cache the redirect.
    return RedirectResponse(link, status_code=302,
                            headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})


@router.get('/api/clips/{clip_id}/play')
def play_clip(request: Request, clip_id: int):
    return signed_clip_link(request, clip_id, download=False)


@router.get('/api/clips/{clip_id}/download')
def download_clip(request: Request, clip_id: int):
    return signed_clip_link(request, clip_id, download=True)
