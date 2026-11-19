"""Shorts Studio V4: focus-first vertical editing + optional facecam + original-language captions.

Run as part of the EXISTING dedicated render dyno, never in the web dyno.
Uses an opt-in OpenAI API key for frame layout analysis and speech-to-text.
"""
from __future__ import annotations
import base64
import json
import logging
import math
import os
import re
import subprocess
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from .core import connect
from .clip_library import require_library_admin, select_clip

log = logging.getLogger('shorts-v4')
router = APIRouter()
API = 'https://api.openai.com/v1'


def configure_db():
    with connect() as db:
        db.execute("ALTER TABLE shorts_jobs ADD COLUMN IF NOT EXISTS engine_version TEXT NOT NULL DEFAULT 'v3'")
        db.execute("ALTER TABLE shorts_jobs ALTER COLUMN engine_version SET DEFAULT 'v4'")


@router.post('/api/clips/{clip_id}/shorts/v4')
async def redo_shorts_v4(request: Request, clip_id: int):
    require_library_admin(request)
    body = await request.json()
    if body.get('permission_confirmed') is not True:
        raise HTTPException(400, 'Confirm recording and editing rights')
    if not os.getenv('OPENAI_API_KEY'):
        raise HTTPException(503, 'Set OPENAI_API_KEY in Heroku Config Vars')
    clip = select_clip(clip_id)
    if not clip or clip['status'] != 'uploaded':
        raise HTTPException(404, 'Original clip missing')
    with connect() as db:
        # Transaction-level lock prevents a requeue while the same job is active.
        row = db.execute('SELECT id,status FROM shorts_jobs WHERE clip_id=%s FOR UPDATE', (clip_id,)).fetchone()
        if row and row['status'] in ('queued', 'processing'):
            return {'status': row['status'], 'clip_id': clip_id}
        if row:
            db.execute("""UPDATE shorts_jobs SET status='queued',engine_version='v4',
                storage_key=NULL,error_message=NULL,updated_at=now() WHERE clip_id=%s""", (clip_id,))
        else:
            db.execute("""INSERT INTO shorts_jobs (clip_id, channel, status, engine_version)
                VALUES (%s,%s,'queued','v4')""", (clip_id, clip['channel']))
    return {'status': 'queued', 'clip_id': clip_id}


def checked_run(command: list[str], timeout: int = 120):
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError('FFmpeg: ' + exc.stderr.decode('utf-8', 'replace')[-700:]) from exc


def probe_dimensions(path: Path) -> tuple[int,int,float]:
    p = checked_run(['ffprobe','-v','error','-select_streams','v:0',
                     '-show_entries','stream=width,height:format=duration',
                     '-of','json', str(path)])
    raw = json.loads(p.stdout)
    stream = raw['streams'][0]
    w,h = int(stream['width']),int(stream['height'])
    duration = float(raw['format'].get('duration',60))
    if w < 320 or h < 240 or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError('Invalid source video dimensions or duration')
    return w,h,duration


def extract_frames(source: Path, tempdir: Path, duration: float) -> list[Path]:
    positions = [max(0.0, duration * p) for p in (0.17,0.5,0.83)]
    result=[]
    for i,pos in enumerate(positions):
        out=tempdir/f'sample_{i}.jpg'
        checked_run(['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-y',
                    '-ss',str(round(pos,3)),'-i',str(source),'-frames:v','1',
                    '-vf','scale=960:-2','-q:v','4',str(out)],timeout=60)
        if out.exists() and out.stat().st_size:
            result.append(out)
    if not result: raise RuntimeError('Could not extract frames for layout analysis')
    return result


def validate_layout(obj: dict) -> dict:
    """Reject suspicious/unusable positions rather than clipping random gameplay faces."""
    focus = obj.get('focus_x',0.5)
    if not isinstance(focus, (int,float)) or not math.isfinite(focus): focus=0.5
    focus = max(0.12,min(0.88,float(focus)))
    region = obj.get('facecam')
    if not isinstance(region, dict): return {'focus_x':focus,'facecam':None}
    try:
        x,y,w,h = [float(region[k]) for k in ('x','y','w','h')]
        confidence=float(region.get('confidence',0))
    except (ValueError,KeyError,TypeError): return {'focus_x':focus,'facecam':None}
    if not all(map(math.isfinite,(x,y,w,h,confidence))): return {'focus_x':focus,'facecam':None}
    # A facecam should be a separate, stable inset; reject full-screen and tiny regions.
    if confidence < 0.85 or not (0<=x<=1 and 0<=y<=1 and .08<=w<=.48 and .07<=h<=.48):
        return {'focus_x':focus,'facecam':None}
    if x+w>1.01 or y+h>1.01: return {'focus_x':focus,'facecam':None}
    return {'focus_x':focus,'facecam':{'x':x,'y':y,'w':w,'h':h}}


def analyze_layout(frames: list[Path], api_key: str) -> dict:
    content=[{'type':'text','text':(
        'Return valid JSON only, with keys focus_x and facecam. Three frames are from the SAME game livestream. '
        'focus_x is normalized x coordinate 0..1 of the most consistently important GAMEPLAY action, not a person webcam. '
        'facecam is null unless a visually separate, clearly bounded streamer webcam overlay stays in a consistent location '
        'across the frames. If so facecam is {x,y,w,h,confidence} normalized to 0..1 original frame. '
        'Do not label in-game humans or game character portraits as webcam. Use null if unsure. '
        'Aim to keep the principal game action visible. No identity recognition or descriptions. JSON only.'
    )}]
    for file in frames:
        b64=base64.b64encode(file.read_bytes()).decode('ascii')
        content.append({'type':'image_url','image_url':{'url':'data:image/jpeg;base64,'+b64,'detail':'high'}})
    response = httpx.post(API+'/chat/completions',
        headers={'Authorization':'Bearer '+api_key},
        json={'model':os.getenv('SHORTS_VISION_MODEL','gpt-4.1-mini'),
              'messages':[{'role':'user','content':content}],
              'response_format':{'type':'json_object'},'temperature':0,'max_tokens':300},timeout=90)
    response.raise_for_status()
    obj=json.loads(response.json()['choices'][0]['message']['content'])
    layout=validate_layout(obj)
    log.info('Layout focus_x=%.2f facecam=%s',layout['focus_x'],bool(layout['facecam']))
    return layout


def transcribe_segments(source: Path, tempdir: Path, api_key: str) -> list[dict]:
    audio=tempdir/'speech.mp3'
    checked_run(['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-y',
                 '-i',str(source),'-vn','-ac','1','-ar','16000','-codec:a','libmp3lame',
                 '-b:a','48k',str(audio)],timeout=100)
    if not audio.exists() or audio.stat().st_size<1000: return []
    with audio.open('rb') as handle:
        resp=httpx.post(API+'/audio/transcriptions',
             headers={'Authorization':'Bearer '+api_key},
             data={'model':'whisper-1','response_format':'verbose_json',
                   'timestamp_granularities[]':'segment'},
             files={'file':('speech.mp3',handle,'audio/mpeg')}, timeout=180)
    resp.raise_for_status()
    result=[]
    for seg in resp.json().get('segments',[]):
        text=str(seg.get('text','')).strip()
        start=float(seg.get('start',0))
        end=float(seg.get('end',0))
        if text and end>start>=0:
            result.append({'start':start,'end':end,'text':text})
    return result


def ass_escape(text: str)->str:
    # Prevent injected ASS override codes; leave Cyrillic, accents, punctuation intact.
    text=text.replace('\\',' ').replace('{','(').replace('}',')').replace('\r',' ').replace('\n',' ')
    return re.sub(r'\s+',' ',text).strip()


def ass_timestamp(seconds: float)->str:
    cs=max(0,int(seconds*100))
    hours,rem=divmod(cs,360000)
    minutes,rem=divmod(rem,6000)
    secs,centes=divmod(rem,100)
    return f'{hours}:{minutes:02d}:{secs:02d}.{centes:02d}'


def write_ass(segments: list[dict], output: Path, has_facecam: bool) -> bool:
    if not segments:return False
    # The caption baseline stays in GAMEPLAY above the streamer's bottom panel.
    y=965 if has_facecam else 1080
    head='''[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 0
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,33,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,3,1,2,40,40,140,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    lines=[head]
    for segment in segments:
        words=ass_escape(segment['text']).split()
        if not words:continue
        groups=[]
        current=[]
        for word in words:
            if current and (len(' '.join(current+[word]))>35 or len(current)>=6):
                groups.append(' '.join(current));current=[]
            current.append(word)
        if current:groups.append(' '.join(current))
        start=float(segment['start']); end=float(segment['end'])
        for i,group in enumerate(groups):
            a=start+(end-start)*i/len(groups)
            b=start+(end-start)*(i+1)/len(groups)
            if b-a<.15:continue
            lines.append(f'Dialogue: 0,{ass_timestamp(a)},{ass_timestamp(b)},Default,,0,0,0,,{{\\an2\\pos(360,{y})}}{group}\n')
    output.write_text(''.join(lines),encoding='utf-8')
    return len(lines)>1


def crop_params(width:int,height:int,focus:float,target_w:int,target_h:int)->tuple[int,int,int,int]:
    """Source crop filling the chosen output rectangle, with x pan toward action."""
    ratio=target_w/target_h
    if width/height>ratio:
        h=height; w=max(2,(int(height*ratio)//2)*2)
    else:
        w=width; h=max(2,(int(width/ratio)//2)*2)
    x=max(0,min(width-w,int(focus*width-w/2)))
    y=max(0,(height-h)//2)
    return (w,h,x,y)


def ffmpeg_vertical_cmd(source:Path,out:Path,dim:tuple[int,int],layout:dict,ass:Path|None,fps:int=30)->list[str]:
    width,height=dim
    facecam=layout['facecam']
    game_h=1000 if facecam else 1280
    cw,ch,cx,cy=crop_params(width,height,layout['focus_x'],720,game_h)
    filters=[f'[0:v]fps={fps},split=2[game][face]'] if facecam else [f'[0:v]fps={fps}[game]']
    filters.append(f'[game]crop={cw}:{ch}:{cx}:{cy},scale=720:{game_h}:flags=lanczos,setsar=1[gamefull]')
    if facecam:
        reg=facecam
        rx=max(0,int(reg['x']*width));ry=max(0,int(reg['y']*height))
        rw=max(2,min(width-rx,int(reg['w']*width)));rh=max(2,min(height-ry,int(reg['h']*height)))
        # Keep proportions and avoid enlarging a very small source webcam past usefulness.
        filters.append(f'[face]crop={rw}:{rh}:{rx}:{ry},scale=380:220:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[smallface]')
        filters.append('[gamefull]pad=720:1280:0:0:color=0x101722[canvas]')
        filters.append('[canvas][smallface]overlay=(W-w)/2:1030+(220-h)/2:shortest=1[composed]')
    else:
        filters.append('[gamefull]null[composed]')
    if ass is not None:
        # temporary ASS path has no shell metacharacters, since TemporaryDirectory is used
        filters.append(f'[composed]ass={ass.as_posix()}[v]')
    else:
        filters.append('[composed]format=yuv420p[v]')
    graph=';'.join(filters)
    return ['ffmpeg','-hide_banner','-nostdin','-loglevel','error','-y',
            '-i',str(source),'-filter_complex',graph,'-map','[v]','-map','0:a:0?',
            '-c:v','libx264','-preset','veryfast','-crf','19','-threads','2',
            '-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart',str(out)]


def render_v4(source:Path,out:Path,tempdir:Path):
    api_key=os.getenv('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY missing. V4 needs voice recognition and vision; set it in Heroku')
    fps=int(os.getenv('SHORTS_V4_FPS','30'))
    if fps not in (30,60):raise RuntimeError('SHORTS_V4_FPS must be 30 or 60')
    width,height,duration=probe_dimensions(source)
    frames=extract_frames(source,tempdir,duration)
    layout=analyze_layout(frames,api_key)
    segments=transcribe_segments(source,tempdir,api_key)
    ass=tempdir/'captions.ass'
    subtitles=ass if write_ass(segments,ass,bool(layout['facecam'])) else None
    if not subtitles:
        log.info('No speech recognized; video will have no captions')
    cmd=ffmpeg_vertical_cmd(source,out,(width,height),layout,subtitles,fps)
    log.info('Rendering V4 %s fps=%s facecam=%s captions=%s',source.name,fps,bool(layout['facecam']),bool(subtitles))
    checked_run(cmd,timeout=1500)
    if not out.is_file() or out.stat().st_size<1024:raise RuntimeError('V4 MP4 is missing or empty')
    return {'facecam_detected':bool(layout['facecam']),'caption_segments':len(segments),'fps':fps}
