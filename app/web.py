import os, json, secrets, urllib.parse, html
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from cryptography.fernet import Fernet
import httpx
from .core import init_db, connect, normalize_channel, get_setting, put_setting
from .twitch import channel_status

app=FastAPI(title='GTA Shorts Studio MVP')
from .clip_library import router as clip_library_router
app.include_router(clip_library_router)
ADMIN_KEY=os.getenv('ADMIN_KEY','')
REDIRECT_URI=os.getenv('TWITCH_REDIRECT_URI','')
FERNET_KEY=os.getenv('FERNET_KEY','')

@app.on_event('startup')
def startup(): init_db()

def authorized(request):
    # Browser session via cookie after login with password; API also supports X-Admin-Key
    token=request.cookies.get('admin') or request.headers.get('x-admin-key','')
    return bool(ADMIN_KEY) and secrets.compare_digest(token,ADMIN_KEY)

def require_admin(request):
    if not authorized(request): raise HTTPException(401,'Unauthorized. Visit /login?key=YOUR_ADMIN_KEY')

def crypt():
    if not FERNET_KEY: raise HTTPException(500,'FERNET_KEY is required')
    return Fernet(FERNET_KEY.encode())

@app.get('/health')
def health(): return {'status':'ok'}

@app.get('/login',response_class=HTMLResponse)
def login_form():
    return HTMLResponse("""<!doctype html><html lang='ru'><meta name='viewport' content='width=device-width,initial-scale=1'><meta charset='utf-8'><title>Вход</title><style>body{margin:0;display:grid;place-items:center;min-height:100vh;background:#0b101a;color:white;font:16px system-ui}.box{background:#172133;padding:24px;border-radius:16px;width:min(90vw,360px)}input,button{box-sizing:border-box;display:block;width:100%;padding:14px;margin:12px 0;border-radius:10px;font:inherit}input{background:#111a29;color:white;border:1px solid #536277}button{border:0;background:#61a8ff;font-weight:bold}</style><form class='box' method='post' action='/login'><h2>GTA Shorts Studio</h2><p>Введи свой ключ администратора из Heroku Config Vars.</p><input type='password' name='key' required autocomplete='current-password' placeholder='ADMIN_KEY'><button>Войти</button></form></html>""")

@app.post('/login')
def login_submit(key:str=Form(...)):
    if not ADMIN_KEY or not secrets.compare_digest(key,ADMIN_KEY): raise HTTPException(401,'Неверный ключ')
    resp=RedirectResponse('/',status_code=303)
    resp.set_cookie('admin',key,secure=os.getenv('COOKIE_SECURE','true')=='true',httponly=True,samesite='lax',max_age=3600*8)
    return resp

@app.get('/',response_class=HTMLResponse)
def home(request:Request):
    if not authorized(request): return RedirectResponse('/login',status_code=303)
    channel=get_setting('channel','')
    template=(Path(__file__).parent/'mobile.html').read_text(encoding='utf-8')
    return HTMLResponse(template.replace('__CHANNEL__',html.escape(channel,quote=True)))

@app.post('/api/channel')
async def set_channel(request:Request):
    require_admin(request)
    data=await request.json()
    try: channel=normalize_channel(data['url'])
    except (ValueError,KeyError) as e: raise HTTPException(400,str(e))
    status=await channel_status(channel)
    if not status['exists']: raise HTTPException(404,'Channel not found')
    # Changing a channel stops the old recording immediately after the capture worker polls DB.
    with connect() as db:
        old = db.execute('SELECT value FROM settings WHERE key=%s', ('channel',)).fetchone()
        if old is None or old['value'] != channel:
            for key, value in [('channel', channel), ('capture_enabled', 'false'),
                               ('capture_permission_channel', '')]:
                db.execute('INSERT INTO settings(key,value) VALUES(%s,%s) '
                           'ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value', (key, value))
    return {'saved':True,'channel':channel,'status':status,
            'note':'Chat and capture will switch automatically. Recording remains OFF until authorized.'}

@app.post('/api/capture/start')
async def start_capture(request: Request):
    require_admin(request)
    data = await request.json()
    if data.get('permission_confirmed') is not True:
        raise HTTPException(400, 'Confirm channel-owner permission for recording and editing')
    with connect() as db:
        channel = db.execute('SELECT value FROM settings WHERE key=%s', ('channel',)).fetchone()
        if not channel or not channel['value']:
            raise HTTPException(400, 'Select a channel first')
        for key, value in [('capture_permission_channel', channel['value']), ('capture_enabled', 'true')]:
            db.execute('INSERT INTO settings(key,value) VALUES(%s,%s) '
                       'ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value', (key,value))
    return {'capture_requested': True, 'channel': channel['value']}

@app.post('/api/capture/stop')
def stop_capture(request: Request):
    require_admin(request)
    put_setting('capture_enabled', 'false')
    return {'capture_requested': False}

@app.get('/api/capture/status')
def capture_status(request: Request):
    require_admin(request)
    channel = get_setting('channel', '')
    permitted = get_setting('capture_permission_channel', '') == channel
    requested = get_setting('capture_enabled', 'false') == 'true' and permitted
    with connect() as db:
        row = db.execute('SELECT value FROM settings WHERE key=%s', ('capture_heartbeat',)).fetchone()
    heartbeat = json.loads(row['value']) if row else {}
    return {'channel':channel, 'capture_requested':requested, 'worker':heartbeat}

@app.get('/api/status')
async def status(request:Request):
    require_admin(request)
    channel=get_setting('channel')
    if not channel:return {'configured':False}
    result=await channel_status(channel)
    result['chat_authorized']=bool(get_setting('chat_user_token'))
    return result

@app.get('/api/highlights')
def highlights(request:Request):
    require_admin(request)
    channel=get_setting('channel')
    if not channel:return []
    with connect() as db:
        rows=db.execute('SELECT id,event_at,count_10s,baseline,unique_users,score,reason,status FROM highlights WHERE channel=%s ORDER BY score DESC, event_at DESC LIMIT 10',(channel,)).fetchall()
    return [dict(row, event_at=row['event_at'].isoformat(),baseline=float(row['baseline']),score=float(row['score'])) for row in rows]

@app.get('/auth/twitch')
def twitch_auth(request:Request):
    require_admin(request)
    if not REDIRECT_URI:raise HTTPException(500,'Set TWITCH_REDIRECT_URI')
    state=secrets.token_urlsafe(24)
    response=RedirectResponse('https://id.twitch.tv/oauth2/authorize?'+urllib.parse.urlencode({
        'client_id':os.environ['TWITCH_CLIENT_ID'],'redirect_uri':REDIRECT_URI,
        'response_type':'code','scope':'user:read:chat','state':state}))
    response.set_cookie('oauth_state',state,httponly=True,secure=os.getenv('COOKIE_SECURE','true')=='true',samesite='lax',max_age=600)
    return response

@app.get('/auth/callback')
async def twitch_callback(request:Request,code:str,state:str):
    if not state or not secrets.compare_digest(state,request.cookies.get('oauth_state','')):
        raise HTTPException(400,'Invalid OAuth state')
    async with httpx.AsyncClient(timeout=20) as client:
        r=await client.post('https://id.twitch.tv/oauth2/token',data={
            'client_id':os.environ['TWITCH_CLIENT_ID'],
            'client_secret':os.environ['TWITCH_CLIENT_SECRET'],
            'code':code,'grant_type':'authorization_code','redirect_uri':REDIRECT_URI})
        r.raise_for_status(); tokens=r.json()
        me=await client.get('https://api.twitch.tv/helix/users',headers={
            'Client-ID':os.environ['TWITCH_CLIENT_ID'],
            'Authorization':'Bearer '+tokens['access_token']})
        me.raise_for_status(); user=me.json()['data'][0]
    put_setting('chat_user_token',crypt().encrypt(json.dumps(tokens).encode()).decode())
    put_setting('chat_user_id',user['id'])
    response=HTMLResponse(f'<h2>Twitch connected: {user["display_name"]}</h2><p>Return to GTA Shorts Studio. Worker reconnect may be required.</p>')
    response.delete_cookie('oauth_state')
    return response
