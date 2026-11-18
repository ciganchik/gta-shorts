import os, httpx
CLIENT_ID=os.getenv('TWITCH_CLIENT_ID','')
CLIENT_SECRET=os.getenv('TWITCH_CLIENT_SECRET','')
API='https://api.twitch.tv/helix'

async def app_token():
    async with httpx.AsyncClient(timeout=20) as client:
        r=await client.post('https://id.twitch.tv/oauth2/token',data={
            'client_id':CLIENT_ID,'client_secret':CLIENT_SECRET,'grant_type':'client_credentials'})
        r.raise_for_status()
        return r.json()['access_token']

async def api_get(path,params=None):
    token=await app_token()
    async with httpx.AsyncClient(timeout=20) as client:
        r=await client.get(API+path,params=params,headers={'Client-ID':CLIENT_ID,'Authorization':'Bearer '+token})
        r.raise_for_status()
        return r.json().get('data',[])

async def channel_status(login):
    users=await api_get('/users',{'login':login})
    if not users: return {'exists':False,'live':False,'login':login}
    uid=users[0]['id']
    streams=await api_get('/streams',{'user_id':uid})
    return {'exists':True,'live':bool(streams),'login':login,'user_id':uid,
            'title':streams[0].get('title') if streams else None,
            'viewer_count':streams[0].get('viewer_count') if streams else 0,
            'game_name':streams[0].get('game_name') if streams else None}
