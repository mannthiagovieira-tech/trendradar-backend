import os, time, asyncio
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

try:
    from pytrends.request import TrendReq
    PYTRENDS_OK = True
except: PYTRENDS_OK = False

ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY","")
YOUTUBE_KEY    = os.getenv("YOUTUBE_API_KEY","")
SPOTIFY_ID     = os.getenv("SPOTIFY_CLIENT_ID","")
SPOTIFY_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET","")
TIKAPI_KEY     = os.getenv("TIKAPI_KEY","")

app = FastAPI(title="DMT TrendRadar API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cache = {}
def cache_get(k,ttl=3600):
    e=_cache.get(k); return e["d"] if e and (time.time()-e["t"])<ttl else None
def cache_set(k,d): _cache[k]={"d":d,"t":time.time()}

def fetch_google_trends(terms,geo="BR"):
    key=f"gt:{':'.join(terms)}:{geo}"
    if c:=cache_get(key): return c
    if not PYTRENDS_OK: return _mock_g(terms)
    try:
        pt=TrendReq(hl="pt-BR" if geo=="BR" else "en-US",tz=-180)
        pt.build_payload(terms[:5],geo=geo,timeframe="today 3-m")
        df=pt.interest_over_time()
        if df.empty: return _mock_g(terms)
        res={}
        for t in terms:
            if t not in df.columns: continue
            s=df[t].dropna()
            if len(s)<14: continue
            rec=float(s.iloc[-7:].mean()); prv=float(s.iloc[-14:-7].mean()) or 1
            res[t]={"value":round(rec),"velocity":round(min(1.0,max(-1.0,(rec-prv)/prv)),2),"timeline":[{"date":str(d.date()),"value":int(v)} for d,v in s.items()]}
        cache_set(key,res); return res
    except Exception as e: print(f"GT error:{e}"); return _mock_g(terms)

def _mock_g(terms):
    import random; return {t:{"value":random.randint(30,70),"velocity":round(random.uniform(0.2,0.7),2),"timeline":[]} for t in terms}

async def fetch_reddit(term,geo="BR"):
    key=f"rd:{term}:{geo}"
    if c:=cache_get(key): return c
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r=await c.get(f"https://www.reddit.com/search.json?q={term}&sort=new&limit=25",headers={"User-Agent":"DMTTrendRadar/2.0"})
            posts=r.json().get("data",{}).get("children",[])
        now=datetime.now().timestamp(); recent=[p for p in posts if (now-p["data"]["created_utc"])<7*86400]
        res={"value":min(100,len(recent)*8+10),"velocity":round(min(1.0,len(recent)/15),2),"post_count":len(recent),"top_posts":[{"title":p["data"]["title"],"url":f"https://reddit.com{p['data']['permalink']}","score":p["data"].get("score",0)} for p in sorted(recent,key=lambda x:-x["data"].get("score",0))[:3]]}
        cache_set(key,res); return res
    except: import random; return {"value":random.randint(20,55),"velocity":0.4,"post_count":0,"top_posts":[]}

async def fetch_youtube(term,geo="BR"):
    key=f"yt:{term}:{geo}"
    if c:=cache_get(key): return c
    if not YOUTUBE_KEY: import random; return {"value":random.randint(30,70),"velocity":0.5,"video_count":0,"top_videos":[],"mock":True}
    try:
        region=geo if geo!="WORLD" else "US"; lang="pt" if geo=="BR" else "en"
        url=f"https://www.googleapis.com/youtube/v3/search?part=snippet&q={term}&type=video&order=date&regionCode={region}&relevanceLanguage={lang}&maxResults=20&key={YOUTUBE_KEY}"
        async with httpx.AsyncClient(timeout=10) as c:
            items=( await c.get(url)).json().get("items",[])
        res={"value":min(100,len(items)*5+20),"velocity":round(min(1.0,len(items)/20),2),"video_count":len(items),"top_videos":[{"title":i["snippet"]["title"],"url":f"https://youtube.com/watch?v={i['id']['videoId']}","channel":i["snippet"]["channelTitle"],"published":i["snippet"]["publishedAt"][:10]} for i in items[:3]]}
        cache_set(key,res); return res
    except Exception as e: print(f"YT error:{e}"); import random; return {"value":random.randint(30,70),"velocity":0.5,"video_count":0,"top_videos":[],"mock":True}

_sp_tok={"token":"","expires":0}
async def get_sp_token():
    if _sp_tok["token"] and time.time()<_sp_tok["expires"]: return _sp_tok["token"]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            d=(await c.post("https://accounts.spotify.com/api/token",data={"grant_type":"client_credentials"},auth=(SPOTIFY_ID,SPOTIFY_SECRET))).json()
            _sp_tok["token"]=d["access_token"]; _sp_tok["expires"]=time.time()+d.get("expires_in",3600)-60; return _sp_tok["token"]
    except: return ""

async def fetch_spotify(term,geo="BR"):
    key=f"sp:{term}:{geo}"
    if c:=cache_get(key): return c
    if not (SPOTIFY_ID and SPOTIFY_SECRET): import random; return {"value":random.randint(30,75),"velocity":0.4,"track_count":0,"top_tracks":[],"mock":True}
    try:
        tok=await get_sp_token()
        if not tok: import random; return {"value":random.randint(30,75),"velocity":0.4,"track_count":0,"top_tracks":[],"mock":True}
        market=geo if geo!="WORLD" else "US"
        async with httpx.AsyncClient(timeout=10) as c:
            tracks=(await c.get(f"https://api.spotify.com/v1/search?q={term}&type=track&market={market}&limit=20",headers={"Authorization":f"Bearer {tok}"})).json().get("tracks",{}).get("items",[])
        if not tracks: import random; return {"value":random.randint(30,75),"velocity":0.4,"track_count":0,"top_tracks":[],"mock":True}
        pops=[t["popularity"] for t in tracks]; avg=sum(pops)/len(pops)
        res={"value":round(avg),"velocity":round(min(1.0,avg/80),2),"track_count":len(tracks),"top_tracks":[{"name":t["name"],"artist":t["artists"][0]["name"],"popularity":t["popularity"],"url":t["external_urls"].get("spotify","")} for t in sorted(tracks,key=lambda x:-x["popularity"])[:3]]}
        cache_set(key,res); return res
    except Exception as e: print(f"SP error:{e}"); import random; return {"value":random.randint(30,75),"velocity":0.4,"track_count":0,"top_tracks":[],"mock":True}


async def fetch_tiktok(term, geo="BR"):
    key = "tk:" + term + geo
    cached = cache_get(key)
    if cached: return cached
    if not TIKAPI_KEY:
        import random; return {"value": random.randint(30,75),"velocity":0.5,"video_count":0,"top_videos":[],"mock":True}
    try:
        lang = "pt-BR" if geo == "BR" else "en"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.tikapi.io/public/search/video?keywords={term}&count=20&region={geo.lower()}&language={lang}",
                headers={"X-API-KEY": TIKAPI_KEY}
            )
            data = r.json()
        videos = data.get("data", {}).get("videos", []) or data.get("itemList", [])
        if not videos:
            import random; return {"value": random.randint(30,75),"velocity":0.5,"video_count":0,"top_videos":[],"mock":True}
        plays = [v.get("stats",{}).get("playCount",0) for v in videos]
        avg = sum(plays)/len(plays) if plays else 0
        value = min(100, round(avg/100000*30 + len(videos)*2 + 20))
        result = {
            "value": value, "velocity": round(min(1.0,len(videos)/20),2),
            "video_count": len(videos),
            "top_videos": [{"title": v.get("desc","")[:80], "url": f"https://www.tiktok.com/@{v.get('author',{}).get('uniqueId','')}", "plays": v.get("stats",{}).get("playCount",0), "likes": v.get("stats",{}).get("diggCount",0), "author": v.get("author",{}).get("nickname","")} for v in sorted(videos, key=lambda x: x.get("stats",{}).get("playCount",0), reverse=True)[:3]]
        }
        cache_set(key, result)
        return result
    except Exception as e:
        print(f"TikTok error: {e}")
        import random; return {"value": random.randint(30,75),"velocity":0.5,"video_count":0,"top_videos":[],"mock":True}

def compute_score(signals,geo):
    W={"google_trends":0.25,"reddit":0.20,"youtube":0.18,"spotify":0.12,"twitter":0.10,"tiktok":0.10,"instagram":0.05} if geo=="BR" else {"google_trends":0.25,"reddit":0.18,"youtube":0.20,"spotify":0.12,"twitter":0.12,"tiktok":0.08,"instagram":0.05}
    raw=0;n=0;vel=0;en=0
    for src,w in W.items():
        s=signals.get(src)
        if not s: continue
        n+=1; boost=1.4 if geo=="BR" and src in["reddit","youtube","spotify"] else 1.0
        raw+=s["value"]*w*boost; vel+=s.get("velocity",0)
        if s["value"]>20 and src in["reddit","youtube","twitter"]: en+=1
    div=1+(n/len(W))*0.25; eb=en*7; vb=(vel/max(n,1))*12
    score=min(100,round(raw*div+eb+vb))
    if geo=="BR":
        stage="Dormente" if score<15 else "Importando" if score<30 else "Emergindo BR" if score<45 else "Pre-Trend BR" if score<65 else "Trending BR" if score<85 else "Pico BR"
    else:
        stage="Dormente" if score<15 else "Emergindo" if score<40 else "Pre-Trend" if score<65 else "Trending" if score<85 else "Pico"
    return{"score":score,"stage":stage,"early_bonus":eb,"vel_bonus":round(vb,1),"diversity":round(div,3)}

async def call_anthropic(prompt,max_tokens=1500):
    if not ANTHROPIC_KEY: return None
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r=await c.post("https://api.anthropic.com/v1/messages",headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},json={"model":"claude-sonnet-4-5","max_tokens":max_tokens,"messages":[{"role":"user","content":prompt}]})
            text=r.json()["content"][0]["text"].strip()
            if text.startswith("```"): text=text.split("\n",1)[1].rsplit("```",1)[0].strip()
            import json; return json.loads(text)
    except Exception as e: print(f"Anthropic error:{e}"); return None

@app.post("/briefing")
async def briefing_ep(term:str=Query(...),geo:str=Query("BR"),score:int=Query(50),stage:str=Query(""),related_terms:str=Query(""),gt_value:int=Query(0),reddit_value:int=Query(0)):
    tl=related_terms.split(",") if related_terms else [term]
    rd=f"Google Trends BR:{gt_value}/100, Reddit:{reddit_value}/100" if gt_value or reddit_value else ""
    prompt=f"""Diretor criativo de marca brasileira de roupas personalizadas (camisetas, bonés, estampas).
TREND:{term} GEO:{geo} SCORE:{score}/100 ESTÁGIO:{stage} TERMOS:{', '.join(tl)} {rd}
Gere 5 ideias criativas de produto. Responda APENAS JSON válido sem markdown:
{{"ideas":[{{"titulo":"","aplicacao":"","headline":"max 6 palavras","tags":["#t1"],"descricaoVisual":"","corPaleta":["c1"],"precoSugerido":"R$ XX","potencial":"ALTO|MEDIO|ESPECULATIVO","motivo":"1 frase"}}],"urgencia":""}}"""
    result=await call_anthropic(prompt,2000)
    if not result: return{"error":"Configure ANTHROPIC_API_KEY no Railway","ideas":[],"urgencia":""}
    return result

@app.post("/analyze")
async def analyze_ep(term:str=Query(...),geo:str=Query("BR"),score:int=Query(50),stage:str=Query(""),related_terms:str=Query(""),gt_value:int=Query(0),reddit_value:int=Query(0),velocity:float=Query(0.5)):
    tl=related_terms.split(",") if related_terms else [term]
    rd=f"Google Trends BR:{gt_value} | Reddit:{reddit_value} | Velocidade:{velocity}" if gt_value or reddit_value else ""
    prompt=f"""Analista de pre-trends de moda BR. TREND:{term} GEO:{geo} SCORE:{score}/100 ESTÁGIO:{stage} TERMOS:{', '.join(tl)} {rd}
Responda APENAS JSON válido sem markdown:
{{"urgency":"ALTA|MEDIA|BAIXA","recommendation":"ação 12 palavras","whyNow":"2 frases","productAngles":["p1","p2","p3"],"estampasArtes":["e1","e2"],"riskFactors":["r1"],"timeToActDays":21,"confidenceScore":75,"culturalContextBR":"1-2 frases","leadingSources":["f1"]}}"""
    result=await call_anthropic(prompt)
    if not result: return{"error":"Configure ANTHROPIC_API_KEY no Railway","urgency":"MEDIA","recommendation":"Configure a API key"}
    return result

@app.get("/score")
async def score_ep(term:str=Query(...),geo:str=Query("BR")):
    geo=geo.upper(); gp="BR" if geo=="BR" else ""
    gt,rd,yt,sp=await asyncio.gather(asyncio.to_thread(fetch_google_trends,[term],gp),fetch_reddit(term,geo),fetch_youtube(term,geo),fetch_spotify(term,geo))
    gt_data=gt.get(term,{"value":40,"velocity":0.4,"timeline":[]})
    signals={"google_trends":gt_data,"reddit":rd,"youtube":yt,"spotify":sp}
    scoring=compute_score(signals,geo)
    real=[s for s,d in[("google_trends",PYTRENDS_OK),("reddit",rd.get("post_count",0)>0),("youtube",yt.get("video_count",0)>0),("spotify",sp.get("track_count",0)>0)] if d]
    return{"term":term,"geo":geo,**scoring,"timeline":gt_data.get("timeline",[]),"fetched_at":datetime.now().isoformat(),"real_sources":real,"sources":{"google_trends":gt_data,"reddit":rd,"youtube":yt,"spotify":sp},"evidence":{"reddit_posts":rd.get("top_posts",[]),"youtube_videos":yt.get("top_videos",[]),"spotify_tracks":sp.get("top_tracks",[])}}

@app.get("/")
def root():
    cfg=[s for s,v in[("anthropic",ANTHROPIC_KEY),("youtube",YOUTUBE_KEY),("spotify",SPOTIFY_ID),("tiktok",TIKAPI_KEY),("google_trends",PYTRENDS_OK)] if v]
    return{"service":"DMT TrendRadar API v2","status":"ok","configured_sources":cfg,"docs":"/docs"}

@app.get("/health")
def health():
    return{"status":"ok","pytrends":PYTRENDS_OK,"anthropic":bool(ANTHROPIC_KEY),"youtube":bool(YOUTUBE_KEY),"spotify":bool(SPOTIFY_ID and SPOTIFY_SECRET),"cache_entries":len(_cache),"time":datetime.now().isoformat()}

@app.get("/trending-br")
async def trending():
    if not PYTRENDS_OK: return{"trending":[],"error":"pytrends not available"}
    key="trending_br"
    if c:=cache_get(key,1800): return c
    try:
        pt=TrendReq(hl="pt-BR",tz=-180); df=pt.trending_searches(pn="brazil")
        res={"trending":df[0].tolist()[:20],"fetched_at":datetime.now().isoformat()}; cache_set(key,res); return res
    except Exception as e: return{"trending":[],"error":str(e)}

@app.get("/batch-score")
async def batch(terms:str=Query(...),geo:str=Query("BR")):
    tl=[t.strip() for t in terms.split(",")][:10]; results={}
    for t in tl:
        try: await asyncio.sleep(2); results[t]=await score_ep(t,geo)
        except Exception as e: results[t]={"error":str(e),"score":0}
    return{"results":results,"geo":geo.upper(),"count":len(results)}
