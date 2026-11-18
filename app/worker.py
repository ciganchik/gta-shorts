"""EventSub WebSocket worker. One Twitch channel at a time.
Requires authorized chat account; some channels require broadcaster authorization.
"""
import asyncio, json, os, logging, datetime as dt
from collections import defaultdict
import httpx, websockets
from cryptography.fernet import Fernet
from .core import init_db, connect, get_setting, SpikeDetector
from .twitch import api_get

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
log=logging.getLogger('worker')
API='https://api.twitch.tv/helix/eventsub/subscriptions'
WS='wss://eventsub.wss.twitch.tv/ws'

async def token_info():
    encrypted=get_setting('chat_user_token')
    if not encrypted:return None
    f=Fernet(os.environ['FERNET_KEY'].encode())
    tokens=json.loads(f.decrypt(encrypted.encode()))
    async with httpx.AsyncClient(timeout=20) as client:
        v=await client.get('https://id.twitch.tv/oauth2/validate',headers={'Authorization':'OAuth '+tokens['access_token']})
        if v.status_code==200 and v.json().get('expires_in',0)>300: return tokens['access_token']
        r=await client.post('https://id.twitch.tv/oauth2/token',data={
            'grant_type':'refresh_token','refresh_token':tokens['refresh_token'],
            'client_id':os.environ['TWITCH_CLIENT_ID'],
            'client_secret':os.environ['TWITCH_CLIENT_SECRET']})
        r.raise_for_status()
        new=r.json()
    with connect() as db:
        db.execute('UPDATE settings SET value=%s WHERE key=%s',
                   (f.encrypt(json.dumps(new).encode()).decode(),'chat_user_token'))
    return new['access_token']

async def subscribe(token,session_id,broadcaster_id,user_id):
    payload={'type':'channel.chat.message','version':'1',
             'condition':{'broadcaster_user_id':broadcaster_id,'user_id':user_id},
             'transport':{'method':'websocket','session_id':session_id}}
    async with httpx.AsyncClient(timeout=20) as client:
        r=await client.post(API,json=payload,headers={
            'Client-ID':os.environ['TWITCH_CLIENT_ID'],
            'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        if r.status_code>=400:
            log.error('Chat subscription rejected: HTTP %s %s',r.status_code,r.text[:500])
        r.raise_for_status()
        log.info('Chat subscribed for broadcaster %s',broadcaster_id)

def parse_time(timestamp):
    if not timestamp:return dt.datetime.now(dt.timezone.utc)
    return dt.datetime.fromisoformat(timestamp.replace('Z','+00:00'))

class ChannelChanged(Exception):
    pass

class ChannelMonitor:
    def __init__(self,channel):
        self.channel=channel
        self.buckets=defaultdict(list)
        self.detector=SpikeDetector()
        self.last_detected=None
        self.last_flushed=None

    def message(self,data,timestamp):
        event=data.get('event',{})
        message=event.get('message',{}).get('text','')[:2000]
        userid=event.get('chatter_user_id','unknown')
        at=parse_time(timestamp)
        boundary=int(at.timestamp())//10*10
        self.buckets[boundary].append(userid)
        with connect() as db:
            db.execute('INSERT INTO chat_samples(channel,event_at,user_id,message) VALUES(%s,%s,%s,%s)',
                       (self.channel,at,userid,message))
        self.flush(at)

    def flush(self,now):
        current=int(now.timestamp())//10*10
        old=[k for k in sorted(self.buckets) if k<current]
        for key in old:
            if self.last_flushed is not None and key<=self.last_flushed:continue
            # trailing five-minute observation window (including empty intervals)
            history=[(dt.datetime.fromtimestamp(t,dt.timezone.utc),self.buckets.get(t,[])) for t in range(key-300,key+1,10)]
            candidate=self.detector.evaluate(history,self.last_detected)
            if candidate:
                with connect() as db:
                    db.execute('''INSERT INTO highlights(channel,event_at,count_10s,baseline,unique_users,score,reason)
                     VALUES(%s,%s,%s,%s,%s,%s,%s)''',(
                        self.channel,candidate['event_at'],candidate['count_10s'],candidate['baseline'],
                        candidate['unique_users'],candidate['score'],candidate['reason']))
                self.last_detected=candidate['event_at']
                log.info('HIGHLIGHT CANDIDATE %s %s',self.channel,candidate['reason'])
            self.last_flushed=key
        # retain only recent buckets
        for k in list(self.buckets):
            if k<current-310:del self.buckets[k]

async def run_channel(channel):
    user_id=get_setting('chat_user_id')
    if not user_id:raise RuntimeError('Authorize chat user in /auth/twitch first')
    broadcaster=(await api_get('/users',{'login':channel}))
    if not broadcaster:raise RuntimeError('Channel does not exist')
    broadcaster_id=broadcaster[0]['id']
    monitor=ChannelMonitor(channel)
    url=WS
    while True:
        token=await token_info()
        if not token:raise RuntimeError('Twitch authorization missing')
        try:
            async with websockets.connect(url,ping_interval=25,ping_timeout=25,max_size=2**20) as ws:
                # recommended: subscribe within 10s of welcome
                while True:
                    # Poll selected channel independently of message traffic.
                    if get_setting('channel') != channel:
                        raise ChannelChanged()
                    try:
                        raw=await asyncio.wait_for(ws.recv(),timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    obj=json.loads(raw)
                    kind=obj.get('metadata',{}).get('message_type')
                    if kind=='session_welcome':
                        session_id=obj['payload']['session']['id']
                        if url==WS:await subscribe(token,session_id,broadcaster_id,user_id)
                        log.info('EventSub session %s',session_id)
                    elif kind=='session_reconnect':
                        url=obj['payload']['session']['reconnect_url']
                        log.info('Twitch requested reconnect')
                        break
                    elif kind=='notification':
                        if obj.get('metadata',{}).get('subscription_type')=='channel.chat.message':
                            monitor.message(obj['payload'],obj.get('metadata',{}).get('message_timestamp'))
                    elif kind=='revocation':
                        raise RuntimeError('Twitch revoked the chat subscription')
        except ChannelChanged:
            log.info('Channel changed; closing old EventSub session for %s', channel)
            return
        except Exception:
            log.exception('EventSub disconnected; retrying')
            url=WS
            await asyncio.sleep(10)

async def main():
    init_db()
    while True:
        channel=get_setting('channel')
        if not channel or not get_setting('chat_user_token'):
            log.info('Waiting for channel and Twitch authorization')
            await asyncio.sleep(15)
            continue
        try: await run_channel(channel)
        except Exception:
            log.exception('Worker error')
            await asyncio.sleep(15)

if __name__=='__main__':asyncio.run(main())
