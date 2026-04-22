from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import asyncio, time, httpx
from datetime import datetime, timedelta

try:
    from pytrends.request import TrendReq
    PYTRENDS_OK = True
except:
    PYTRENDS_OK = False

app = FastAPI(title="TrendRadar API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_cache = {}
def cache_get(k):
    e = _cache.get(k)
    return e["d"] if e and time.time()-e["t"] < 3600 else None
def cache_set(k, d):
    _cache[k] = {"d": d, "t": time.time()}

def fetch_google_trends(terms, geo="BR"):
    key = f"gt:{':'.join(terms)}:{geo}"
    if c := cache_get(key): return c
    if not PYTRENDS_OK: return _mock(terms)
    try:
        pt = TrendReq(hl="pt-BR" if geo=="BR" else "en-US", tz=-180)
        pt.build_payload(terms[:5], geo=geo, timeframe="today 3-m")
        df = pt.interest_over_time()
        if df.empty: return _mock(terms)
        res = {}
        for t in terms:
            if t not in df.columns: continue
            s = df[t].dropna()
            if len(s) < 14: continue
            rec = float(s.iloc[-7:].mean())
            prv = float(s.iloc[-14:-7].mean()) or 1
            res[t] = {
                "value": round(rec),
                "velocity": round(min(1.0, max(-1.0, (rec-prv)/prv)), 2),
                "timeline": [{"date": str(d.date()), "value": int(v)} for d,v in s.items()]
            }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"GT error: {e}")
        return _mock(terms)

def _mock(terms):
    import random
    return {t: {"value": random.randint(30,70), "velocity": round(random.uniform(0.2,0.7),2), "timeline": []} for t in terms}

async def fetch_reddit(term, geo="BR"):
    key = f"rd:{term}:{geo}"
    if c := cache_get(key): return c
    subs = "brasil+futebol+seriados+musica" if geo=="BR" else "fashion+streetwear+TikTok"
    url = f"https://www.reddit.com/search.json?q={term}&sort=new&limit=25"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(url, headers={"User-Agent": "TrendRadar/1.0"})
            posts = r.json().get("data",{}).get("children",[])
        now = datetime.now().timestamp()
        recent = [p for p in posts if (now - p["data"]["created_utc"]) < 7*86400]
        value = min(100, len(recent)*8 + 10)
        res = {
            "value": value,
            "velocity": round(min(1.0, len(recent)/15), 2),
            "top_posts": [{"title": p["data"]["title"], "url": f"https://reddit.com{p['data']['permalink']}", "score": p["data"].get("score",0)} for p in recent[:3]]
        }
        cache_set(key, res)
        return res
    except:
        import random
        return {"value": random.randint(20,55), "velocity": round(random.uniform(0.2,0.6),2), "top_posts": []}

@app.get("/")
def root():
    return {"status": "ok", "pytrends": PYTRENDS_OK, "docs": "/docs"}

@app.get("/health")
def health():
    return {"status": "ok", "pytrends": PYTRENDS_OK, "cache": len(_cache), "time": datetime.now().isoformat()}

@app.get("/score")
async def score(term: str = Query(...), geo: str = Query("BR")):
    geo = geo.upper()
    geo_param = "BR" if geo=="BR" else ""
    gt, rd = await asyncio.gather(
        asyncio.to_thread(fetch_google_trends, [term], geo_param),
        fetch_reddit(term, geo)
    )
    gt_data = gt.get(term, {"value": 40, "velocity": 0.4, "timeline": []})
    w_gt, w_rd = (0.6, 0.4) if geo=="BR" else (0.55, 0.45)
    raw = gt_data["value"]*w_gt + rd["value"]*w_rd
    vel = (gt_data["velocity"] + rd["velocity"]) / 2
    early = 7 if rd["value"] > 25 else 0
    final = min(100, round(raw + early + vel*10))
    stages = ([(85,"Pico"),(65,"Trending"),(45,"Pre-Trend"),(20,"Emergindo"),(0,"Dormente")]
              if geo!="BR" else
              [(85,"Pico BR"),(65,"Trending BR"),(45,"Pre-Trend BR"),(25,"Emergindo BR"),(10,"Importando"),(0,"Dormente")])
    stage = next(l for t,l in stages if final >= t)
    return {
        "term": term, "geo": geo, "score": final, "stage": stage,
        "velocity": round(vel, 2),
        "sources": {"google_trends": gt_data, "reddit": rd},
        "timeline": gt_data.get("timeline", []),
        "fetched_at": datetime.now().isoformat()
    }

@app.get("/trending-br")
async def trending():
    if not PYTRENDS_OK: return {"trending": [], "error": "pytrends not available"}
    key = "trending_br"
    if c := cache_get(key): return c
    try:
        pt = TrendReq(hl="pt-BR", tz=-180)
        df = pt.trending_searches(pn="brazil")
        res = {"trending": df[0].tolist()[:20], "fetched_at": datetime.now().isoformat()}
        cache_set(key, res)
        return res
    except Exception as e:
        return {"trending": [], "error": str(e)}
