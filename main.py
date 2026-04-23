import os, time, asyncio, json, random, re
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx

try:
    from pytrends.request import TrendReq
    PYTRENDS_OK = True
except Exception:
    PYTRENDS_OK = False

try:
    import feedparser
    FEEDPARSER_OK = True
except Exception:
    FEEDPARSER_OK = False

# ============================================================================
# ENV VARS
# ============================================================================
ANTHROPIC_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
YOUTUBE_KEY    = os.getenv("YOUTUBE_API_KEY", "")
SPOTIFY_ID     = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
TIKAPI_KEY     = os.getenv("TIKAPI_KEY", "")
RAPIDAPI_KEY   = os.getenv("RAPIDAPI_KEY", "")

# ============================================================================
# APP
# ============================================================================
app = FastAPI(title="DMT TrendRadar API", version="4.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# CACHE
# ============================================================================
_cache: Dict[str, Dict[str, Any]] = {}

def cache_get(k: str, ttl: int = 3600):
    e = _cache.get(k)
    return e["d"] if e and (time.time() - e["t"]) < ttl else None

def cache_set(k: str, d: Any):
    _cache[k] = {"d": d, "t": time.time()}

def mock_source(base_low=30, base_high=70, velocity=0.4) -> Dict[str, Any]:
    return {"value": random.randint(base_low, base_high), "velocity": velocity, "mock": True}

def rapid_headers(host: str) -> Dict[str, str]:
    # User-Agent custom é necessário em hosts protegidos por Cloudflare
    # (youtube-v311 e shazam-api6 retornam 1010 sem UA válido)
    return {
        "X-RapidAPI-Key": RAPIDAPI_KEY,
        "X-RapidAPI-Host": host,
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

# ============================================================================
# 1. GOOGLE TRENDS (pytrends)
# ============================================================================
def fetch_google_trends(terms: List[str], geo: str = "BR") -> Dict[str, Any]:
    key = f"gt:{':'.join(terms)}:{geo}"
    if c := cache_get(key):
        return c
    if not PYTRENDS_OK:
        return {t: {"value": random.randint(30, 70), "velocity": round(random.uniform(0.2, 0.7), 2), "timeline": []} for t in terms}
    try:
        pt = TrendReq(hl="pt-BR" if geo == "BR" else "en-US", tz=-180)
        pt.build_payload(terms[:5], geo=geo, timeframe="today 3-m")
        df = pt.interest_over_time()
        if df.empty:
            return {t: {"value": 40, "velocity": 0.3, "timeline": []} for t in terms}
        res = {}
        for t in terms:
            if t not in df.columns:
                continue
            s = df[t].dropna()
            if len(s) < 14:
                continue
            rec = float(s.iloc[-7:].mean())
            prv = float(s.iloc[-14:-7].mean()) or 1
            res[t] = {
                "value": round(rec),
                "velocity": round(min(1.0, max(-1.0, (rec - prv) / prv)), 2),
                "timeline": [{"date": str(d.date()), "value": int(v)} for d, v in s.items()],
            }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[GT] error: {e}")
        return {t: {"value": random.randint(30, 70), "velocity": 0.4, "timeline": []} for t in terms}

# ============================================================================
# 2. REDDIT (público)
# ============================================================================
async def fetch_reddit(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"rd:{term}:{geo}"
    if c := cache_get(key):
        return c
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://www.reddit.com/search.json?q={term}&sort=new&limit=25",
                headers={"User-Agent": "DMTTrendRadar/4.0"},
            )
            posts = r.json().get("data", {}).get("children", [])
        now = datetime.now().timestamp()
        recent = [p for p in posts if (now - p["data"]["created_utc"]) < 7 * 86400]
        res = {
            "value": min(100, len(recent) * 8 + 10),
            "velocity": round(min(1.0, len(recent) / 15), 2),
            "post_count": len(recent),
            "top_posts": [
                {"title": p["data"]["title"], "url": f"https://reddit.com{p['data']['permalink']}", "score": p["data"].get("score", 0)}
                for p in sorted(recent, key=lambda x: -x["data"].get("score", 0))[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[Reddit] error: {e}")
        return {"value": random.randint(20, 55), "velocity": 0.4, "post_count": 0, "top_posts": [], "mock": True}

# ============================================================================
# 3. MERCADO LIVRE (público)
# ============================================================================
async def fetch_mercadolivre(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"ml:{term}:{geo}"
    if c := cache_get(key):
        return c
    site = "MLB" if geo == "BR" else "MLA"  # MLB=Brasil, MLA=Argentina
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"https://api.mercadolibre.com/sites/{site}/search?q={term}&limit=20")
            data = r.json()
        items = data.get("results", [])
        if not items:
            return {"value": random.randint(25, 60), "velocity": 0.3, "item_count": 0, "top_items": [], "mock": True}
        prices = [i.get("price", 0) for i in items if i.get("price")]
        avg_price = sum(prices) / len(prices) if prices else 0
        sold = sum(i.get("sold_quantity", 0) for i in items)
        value = min(100, round(len(items) * 2 + min(sold / 50, 40) + 20))
        res = {
            "value": value,
            "velocity": round(min(1.0, sold / 500), 2),
            "item_count": len(items),
            "avg_price": round(avg_price, 2),
            "total_sold": sold,
            "top_items": [
                {"title": i.get("title", ""), "price": i.get("price", 0), "sold": i.get("sold_quantity", 0), "url": i.get("permalink", "")}
                for i in sorted(items, key=lambda x: -x.get("sold_quantity", 0))[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[ML] error: {e}")
        return {"value": random.randint(25, 60), "velocity": 0.3, "item_count": 0, "top_items": [], "mock": True}

# ============================================================================
# 4. RSS FEEDS (hypebeast, vogue br, highsnobiety, dazed)
# ============================================================================
RSS_FEEDS = {
    "hypebeast":    "https://hypebeast.com/feed",
    "vogue_br":     "https://vogue.globo.com/rss/",
    "highsnobiety": "https://www.highsnobiety.com/feed/",
    "dazed":        "https://www.dazeddigital.com/rss",
}

async def _fetch_rss_single(name: str, url: str, term: str) -> List[Dict[str, Any]]:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "DMTTrendRadar/4.0"})
            text = r.text
        if not FEEDPARSER_OK:
            return []
        feed = feedparser.parse(text)
        matches = []
        tl = term.lower()
        for entry in feed.entries[:50]:
            title = entry.get("title", "")
            summary = entry.get("summary", "") or entry.get("description", "")
            if tl in title.lower() or tl in summary.lower():
                matches.append({
                    "source": name,
                    "title": title,
                    "url": entry.get("link", ""),
                    "published": entry.get("published", "")[:25],
                })
        return matches
    except Exception as e:
        print(f"[RSS {name}] error: {e}")
        return []

async def fetch_rss(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"rss:{term}"
    if c := cache_get(key):
        return c
    results = await asyncio.gather(*[_fetch_rss_single(n, u, term) for n, u in RSS_FEEDS.items()])
    flat: List[Dict[str, Any]] = []
    for r in results:
        flat.extend(r)
    source_counts = {n: len(r) for n, r in zip(RSS_FEEDS.keys(), results)}
    value = min(100, len(flat) * 12 + 10)
    res = {
        "value": value,
        "velocity": round(min(1.0, len(flat) / 8), 2),
        "article_count": len(flat),
        "sources_hit": [n for n, c in source_counts.items() if c > 0],
        "source_counts": source_counts,
        "top_articles": flat[:5],
    }
    cache_set(key, res)
    return res

# ============================================================================
# 5. TIKAPI (TIKAPI_KEY) — backup TikTok
# ============================================================================
async def fetch_tikapi(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"tkapi:{term}:{geo}"
    if c := cache_get(key):
        return c
    if not TIKAPI_KEY:
        return {"value": random.randint(30, 75), "velocity": 0.5, "video_count": 0, "top_videos": [], "mock": True}
    try:
        lang = "pt-BR" if geo == "BR" else "en"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.tikapi.io/public/search/video?keywords={term}&count=20&region={geo.lower()}&language={lang}",
                headers={"X-API-KEY": TIKAPI_KEY},
            )
            data = r.json()
        videos = data.get("data", {}).get("videos", []) or data.get("itemList", [])
        if not videos:
            return {"value": random.randint(30, 75), "velocity": 0.5, "video_count": 0, "top_videos": [], "mock": True}
        plays = [v.get("stats", {}).get("playCount", 0) for v in videos]
        avg = sum(plays) / len(plays) if plays else 0
        value = min(100, round(avg / 100000 * 30 + len(videos) * 2 + 20))
        res = {
            "value": value,
            "velocity": round(min(1.0, len(videos) / 20), 2),
            "video_count": len(videos),
            "top_videos": [
                {
                    "title": v.get("desc", "")[:80],
                    "url": f"https://www.tiktok.com/@{v.get('author', {}).get('uniqueId', '')}",
                    "plays": v.get("stats", {}).get("playCount", 0),
                    "likes": v.get("stats", {}).get("diggCount", 0),
                    "author": v.get("author", {}).get("nickname", ""),
                }
                for v in sorted(videos, key=lambda x: x.get("stats", {}).get("playCount", 0), reverse=True)[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[TikAPI] error: {e}")
        return {"value": random.randint(30, 75), "velocity": 0.5, "video_count": 0, "top_videos": [], "mock": True}

# ============================================================================
# 6. SPOTIFY
# ============================================================================
_sp_tok = {"token": "", "expires": 0.0}

async def _get_spotify_token() -> str:
    if _sp_tok["token"] and time.time() < _sp_tok["expires"]:
        return _sp_tok["token"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(SPOTIFY_ID, SPOTIFY_SECRET),
            )
            d = r.json()
            _sp_tok["token"] = d["access_token"]
            _sp_tok["expires"] = time.time() + d.get("expires_in", 3600) - 60
            return _sp_tok["token"]
    except Exception as e:
        print(f"[Spotify token] error: {e}")
        return ""

async def fetch_spotify(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"sp:{term}:{geo}"
    if c := cache_get(key):
        return c
    if not (SPOTIFY_ID and SPOTIFY_SECRET):
        return {"value": random.randint(30, 75), "velocity": 0.4, "track_count": 0, "top_tracks": [], "mock": True}
    try:
        tok = await _get_spotify_token()
        if not tok:
            return {"value": random.randint(30, 75), "velocity": 0.4, "track_count": 0, "top_tracks": [], "mock": True}
        market = geo if geo != "WORLD" else "US"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.spotify.com/v1/search?q={term}&type=track&market={market}&limit=20",
                headers={"Authorization": f"Bearer {tok}"},
            )
            tracks = r.json().get("tracks", {}).get("items", [])
        if not tracks:
            return {"value": random.randint(30, 75), "velocity": 0.4, "track_count": 0, "top_tracks": [], "mock": True}
        pops = [t["popularity"] for t in tracks]
        avg = sum(pops) / len(pops)
        res = {
            "value": round(avg),
            "velocity": round(min(1.0, avg / 80), 2),
            "track_count": len(tracks),
            "top_tracks": [
                {
                    "name": t["name"],
                    "artist": t["artists"][0]["name"],
                    "popularity": t["popularity"],
                    "url": t["external_urls"].get("spotify", ""),
                }
                for t in sorted(tracks, key=lambda x: -x["popularity"])[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[Spotify] error: {e}")
        return {"value": random.randint(30, 75), "velocity": 0.4, "track_count": 0, "top_tracks": [], "mock": True}

# ============================================================================
# 7. RAPIDAPI — TIKTOK SCRAPER (tiktok-scraper7.p.rapidapi.com)
# ============================================================================
async def fetch_rapid_tiktok(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"rtk:{term}:{geo}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(30, 75)
    host = "tiktok-scraper7.p.rapidapi.com"
    region = geo.lower() if geo != "WORLD" else "us"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://{host}/feed/search",
                params={"keywords": term, "region": region, "count": "20", "cursor": "0", "publish_time": "0", "sort_type": "0"},
                headers=rapid_headers(host),
            )
            data = r.json()
        videos = data.get("data", {}).get("videos", []) or data.get("data", [])
        if not videos:
            return mock_source(30, 75)
        plays = [v.get("play_count", 0) or v.get("stats", {}).get("playCount", 0) for v in videos]
        avg = sum(plays) / len(plays) if plays else 0
        value = min(100, round(avg / 100000 * 30 + len(videos) * 2 + 20))
        res = {
            "value": value,
            "velocity": round(min(1.0, len(videos) / 20), 2),
            "video_count": len(videos),
            "top_videos": [
                {
                    "title": (v.get("title") or v.get("desc", ""))[:80],
                    "url": v.get("play") or v.get("url", ""),
                    "plays": v.get("play_count", 0),
                    "likes": v.get("digg_count", 0),
                    "author": v.get("author", {}).get("nickname", "") if isinstance(v.get("author"), dict) else "",
                }
                for v in sorted(videos, key=lambda x: x.get("play_count", 0), reverse=True)[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidTikTok] error: {e}")
        return mock_source(30, 75)

# ============================================================================
# 8. RAPIDAPI — INSTAGRAM (instagram-scraper-stable-api.p.rapidapi.com)
# ============================================================================
async def fetch_rapid_instagram(term: str, geo: str = "BR") -> Dict[str, Any]:
    """instagram-scraper-stable-api: GET /search_hashtag.php?hashtag=<tag>
    Resposta: { name, posts:{count, edges}, top_posts:{edges:[{node:{...}}]}, pagination_token }
    `posts.count` é o total histórico do hashtag — excelente sinal de tamanho/popularidade.
    """
    import math
    key = f"rig:{term}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(30, 70)
    host = "instagram-scraper-stable-api.p.rapidapi.com"
    hashtag = term.replace(" ", "").replace("#", "").lower()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://{host}/search_hashtag.php",
                params={"hashtag": hashtag},
                headers=rapid_headers(host),
            )
            if r.status_code in (401, 403):
                print(f"[RapidInstagram] {r.status_code} — verifique subscription/quota: {r.text[:120]}")
                return mock_source(30, 70)
            data = r.json()
        total_count = (data.get("posts") or {}).get("count", 0) or 0
        top_edges = (data.get("top_posts") or {}).get("edges", []) or []
        if not top_edges and not total_count:
            return mock_source(30, 70)

        # Score base: log10 do total de posts (10k→48, 100k→60, 1M→72, 10M→84)
        base = min(85, round(math.log10(max(total_count, 10)) * 12)) if total_count else 30
        # Bônus: engajamento médio dos top_posts (likes + comments)
        likes = [(e.get("node") or {}).get("edge_liked_by", {}).get("count", 0) or 0 for e in top_edges]
        comments = [(e.get("node") or {}).get("edge_media_to_comment", {}).get("count", 0) or 0 for e in top_edges]
        avg_likes = round(sum(likes) / len(likes)) if likes else 0
        avg_comments = round(sum(comments) / len(comments)) if comments else 0
        eng_bonus = min(15, round(avg_likes / 5000))
        value = min(100, base + eng_bonus)
        velocity = round(min(1.0, (avg_likes / 50000) + (len(top_edges) / 30)), 2)

        def caption_of(node):
            edges = ((node or {}).get("edge_media_to_caption") or {}).get("edges") or []
            return ((edges[0] or {}).get("node", {}).get("text", "") if edges else "")[:90]

        res = {
            "value": value,
            "velocity": velocity,
            "post_count": total_count,
            "top_posts_count": len(top_edges),
            "avg_likes": avg_likes,
            "avg_comments": avg_comments,
            "top_posts": [
                {
                    "caption": caption_of(e.get("node")),
                    "likes": (e.get("node") or {}).get("edge_liked_by", {}).get("count", 0),
                    "comments": (e.get("node") or {}).get("edge_media_to_comment", {}).get("count", 0),
                    "url": f"https://instagram.com/p/{(e.get('node') or {}).get('shortcode', '')}",
                }
                for e in sorted(top_edges, key=lambda x: -((x.get("node") or {}).get("edge_liked_by", {}).get("count", 0) or 0))[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidInstagram] error: {e}")
        return mock_source(30, 70)

# ============================================================================
# 9. RAPIDAPI — PINTEREST (pinterest-scraper2.p.rapidapi.com)
# ============================================================================
async def fetch_rapid_pinterest(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"rpin:{term}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(30, 65)
    host = "pinterest-scraper2.p.rapidapi.com"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://{host}/search_pins",
                params={"keyword": term},
                headers=rapid_headers(host),
            )
            data = r.json()
        pins = data.get("data", {}).get("results", []) or data.get("pins", []) or data.get("results", [])
        if not pins:
            return mock_source(30, 65)
        saves = [p.get("repin_count", 0) or p.get("saves", 0) for p in pins]
        total_saves = sum(saves)
        value = min(100, round(len(pins) * 2 + total_saves / 100 + 25))
        res = {
            "value": value,
            "velocity": round(min(1.0, len(pins) / 25), 2),
            "pin_count": len(pins),
            "total_saves": total_saves,
            "top_pins": [
                {
                    "title": (p.get("title") or p.get("description", ""))[:80],
                    "saves": p.get("repin_count", 0),
                    "url": p.get("link") or p.get("url", ""),
                    "image": p.get("image", ""),
                }
                for p in sorted(pins, key=lambda x: -(x.get("repin_count", 0) or 0))[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidPinterest] error: {e}")
        return mock_source(30, 65)

# ============================================================================
# 10. RAPIDAPI — TWITTER (twitter241.p.rapidapi.com)
# ============================================================================
async def fetch_rapid_twitter(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"rtw:{term}:{geo}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(30, 70)
    host = "twitter241.p.rapidapi.com"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://{host}/search-v2",
                params={"type": "Top", "count": "20", "query": term},
                headers=rapid_headers(host),
            )
            data = r.json()
        tweets = (
            data.get("result", {})
                .get("timeline", {})
                .get("instructions", [{}])[0]
                .get("entries", [])
        )
        tweets = [t for t in tweets if "tweet" in str(t.get("content", "")).lower() or t.get("content", {}).get("itemContent")]
        if not tweets:
            return mock_source(30, 70)
        value = min(100, len(tweets) * 4 + 20)
        res = {
            "value": value,
            "velocity": round(min(1.0, len(tweets) / 15), 2),
            "tweet_count": len(tweets),
            "top_tweets": [
                {
                    "text": str(t.get("content", {}))[:100],
                    "url": "",
                }
                for t in tweets[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidTwitter] error: {e}")
        return mock_source(30, 70)

# ============================================================================
# 11. RAPIDAPI — YOUTUBE (youtube-v311.p.rapidapi.com)
# ============================================================================
async def fetch_rapid_youtube(term: str, geo: str = "BR") -> Dict[str, Any]:
    """youtube-v311: GET /search?q=...&part=snippet&type=video&maxResults=20
    Host protegido por Cloudflare — rapid_headers já injeta User-Agent válido.
    """
    key = f"ryt:{term}:{geo}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(30, 70)
    host = "youtube-v311.p.rapidapi.com"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://{host}/search",
                params={"q": term, "part": "snippet", "type": "video", "maxResults": "20"},
                headers=rapid_headers(host),
            )
            if r.status_code in (401, 403):
                print(f"[RapidYouTube] {r.status_code} — verifique subscription: {r.text[:120]}")
                return mock_source(30, 70)
            data = r.json()
        items = data.get("items", []) or []
        if not items:
            return mock_source(30, 70)
        total_results = (data.get("pageInfo") or {}).get("totalResults", 0) or 0
        # Score base: amount of recent videos + tamanho do resultado total
        import math
        base = min(80, len(items) * 3 + (round(math.log10(total_results) * 6) if total_results else 0))
        res = {
            "value": min(100, base),
            "velocity": round(min(1.0, len(items) / 20), 2),
            "video_count": len(items),
            "total_results": total_results,
            "top_videos": [
                {
                    "title": (i.get("snippet") or {}).get("title", ""),
                    "url": f"https://youtube.com/watch?v={(i.get('id') or {}).get('videoId', '')}",
                    "channel": (i.get("snippet") or {}).get("channelTitle", ""),
                    "published": ((i.get("snippet") or {}).get("publishedAt", "") or "")[:10],
                }
                for i in items[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidYouTube] error: {e}")
        return mock_source(30, 70)

# ============================================================================
# 12. RAPIDAPI — SHAZAM (shazam-api6.p.rapidapi.com)
#   Essa API só expõe top charts por país — não tem search por termo.
#   fetch_rapid_shazam reaproveita o top BR: se o termo bate com algum
#   título/artista do top, sinal é forte; caso contrário, sinal base.
# ============================================================================
SHAZAM_HOST = "shazam-api6.p.rapidapi.com"

async def _fetch_shazam_top(country_code: str = "BR", limit: int = 10) -> List[Dict[str, str]]:
    key = f"sh_top:{country_code}:{limit}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://{SHAZAM_HOST}/shazam/top_tracks_country",
                params={"country_code": country_code, "limit": str(limit)},
                headers=rapid_headers(SHAZAM_HOST),
            )
            if r.status_code in (401, 403):
                print(f"[Shazam] {r.status_code} — verifique subscription: {r.text[:120]}")
                return []
            data = r.json()
        tracks = data.get("tracks") or data.get("data") or []
        out: List[Dict[str, str]] = []
        for t in tracks[:limit]:
            title = t.get("title", "") or ""
            artist = t.get("subtitle", "") or t.get("artist", "") or ""
            rank = t.get("rank")
            if title:
                out.append({"title": title, "artist": artist, "rank": rank})
        if out:
            cache_set(key, out)
        return out
    except Exception as e:
        print(f"[Shazam] error: {e}")
        return []

async def fetch_rapid_shazam(term: str, geo: str = "BR") -> Dict[str, Any]:
    cc = "BR" if geo == "BR" else "US"
    top = await _fetch_shazam_top(cc, 10)
    if not top:
        return mock_source(30, 65)
    term_lc = term.lower().strip()
    hits = []
    for t in top:
        blob = f"{t.get('title','')} {t.get('artist','')}".lower()
        if term_lc and term_lc in blob:
            hits.append(t)
    value = min(100, 30 + len(hits) * 20) if hits else 30
    velocity = round(min(1.0, len(hits) / 5), 2) if hits else 0.2
    return {
        "value": value,
        "velocity": velocity,
        "track_count": len(top),
        "matches_in_top": len(hits),
        "top_tracks": [
            {"title": t.get("title",""), "artist": t.get("artist",""), "rank": t.get("rank"),
             "url": f"https://www.shazam.com/track/search?q={t.get('title','').replace(' ','+')}"}
            for t in (hits or top[:3])
        ],
    }

# Alias mantém compatibilidade com /briefing que já chama fetch_shazam_top_br
async def fetch_shazam_top_br(limit: int = 10) -> List[Dict[str, str]]:
    return await _fetch_shazam_top("BR", limit)

# ============================================================================
# 13. RAPIDAPI — AMAZON (Bilgisam Ltd)
# ============================================================================
async def fetch_rapid_amazon(term: str, geo: str = "BR") -> Dict[str, Any]:
    """amazon-products-api-prices-api-reviews-api-data-api (Bilgisam Ltd):
    GET /searchProductsWithDetails?query=...&number=20&country=BR&category=fashion&noqueue=1&page=1
    Response: { status, message, results:[...], page, cacheTime }
    """
    key = f"ramz:{term}:{geo}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(25, 65)
    host = "amazon-products-api-prices-api-reviews-api-data-api.p.rapidapi.com"
    country = "BR" if geo == "BR" else "US"
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(
                f"https://{host}/searchProductsWithDetails",
                params={
                    "query": term,
                    "number": "20",
                    "country": country,
                    "category": "fashion",
                    "noqueue": "1",
                    "page": "1",
                },
                headers=rapid_headers(host),
            )
            if r.status_code in (401, 403):
                print(f"[RapidAmazon] {r.status_code} — verifique subscription: {r.text[:120]}")
                return mock_source(25, 65)
            data = r.json()
        products = data.get("results", []) or data.get("data", {}).get("products", []) or []
        if not products:
            return mock_source(25, 65)
        def num(x):
            try: return float(str(x).replace(",", ".").split()[0])
            except: return 0.0
        ratings = [num(p.get("rating") or p.get("product_star_rating")) for p in products]
        ratings = [x for x in ratings if x > 0]
        avg_rating = sum(ratings) / len(ratings) if ratings else 0
        value = min(100, len(products) * 3 + round(avg_rating * 6) + 15)
        res = {
            "value": value,
            "velocity": round(min(1.0, len(products) / 20), 2),
            "product_count": len(products),
            "avg_rating": round(avg_rating, 2),
            "top_products": [
                {
                    "title": (p.get("title") or p.get("product_title", ""))[:80],
                    "price": p.get("price") or p.get("product_price", ""),
                    "rating": p.get("rating") or p.get("product_star_rating", ""),
                    "url": p.get("url") or p.get("product_url", ""),
                }
                for p in products[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidAmazon] error: {e}")
        return mock_source(25, 65)

# ============================================================================
# 14. RAPIDAPI — ETSY (etsy-api2.p.rapidapi.com)
#   GET /product/search?query=...  → { response: [ {title, url, images, price...} ] }
# ============================================================================
async def fetch_rapid_etsy(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"rety:{term}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(25, 60)
    host = "etsy-api2.p.rapidapi.com"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://{host}/product/search",
                params={"query": term, "limit": "20", "offset": "0",
                        "sort_on": "relevance", "sort_order": "desc"},
                headers=rapid_headers(host),
            )
            if r.status_code in (401, 403):
                print(f"[RapidEtsy] {r.status_code} — verifique subscription: {r.text[:120]}")
                return mock_source(25, 60)
            data = r.json()
        listings = data.get("response", []) or data.get("results", []) or \
                   data.get("data", {}).get("listings", []) or data.get("listings", []) or []
        if not listings:
            return mock_source(25, 60)
        def price_of(l):
            p = l.get("price") or l.get("priceSummary") or {}
            if isinstance(p, dict):
                return p.get("salePrice") or p.get("originalPrice") or p.get("amount") or ""
            return str(p)
        res = {
            "value": min(100, len(listings) * 3 + 25),
            "velocity": round(min(1.0, len(listings) / 20), 2),
            "listing_count": len(listings),
            "top_listings": [
                {
                    "title": (l.get("title") or l.get("name", ""))[:80],
                    "price": price_of(l),
                    "url": l.get("url") or l.get("listing_url", ""),
                }
                for l in listings[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidEtsy] error: {e}")
        return mock_source(25, 60)

# ============================================================================
# 15. RAPIDAPI — IMDB (imdb236.p.rapidapi.com)
# ============================================================================
async def fetch_rapid_imdb(term: str, geo: str = "BR") -> Dict[str, Any]:
    key = f"rimdb:{term}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(25, 55)
    host = "imdb236.p.rapidapi.com"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://{host}/imdb/autocomplete",
                params={"query": term},
                headers=rapid_headers(host),
            )
            data = r.json()
        titles = data.get("d", []) or data.get("results", []) or data.get("data", [])
        if not titles:
            return mock_source(25, 55)
        res = {
            "value": min(100, len(titles) * 5 + 20),
            "velocity": round(min(1.0, len(titles) / 10), 2),
            "title_count": len(titles),
            "top_titles": [
                {
                    "title": t.get("l") or t.get("title", ""),
                    "year": t.get("y") or t.get("year", ""),
                    "type": t.get("qid") or t.get("type", ""),
                    "id": t.get("id", ""),
                }
                for t in titles[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidIMDb] error: {e}")
        return mock_source(25, 55)

# ============================================================================
# 16. RAPIDAPI — FOOTBALL BR (free-api-live-football-data.p.rapidapi.com)
# ============================================================================
async def fetch_rapid_football(term: str, geo: str = "BR") -> Dict[str, Any]:
    """free-api-live-football-data: GET /football-get-matches-by-date?date=YYYYMMDD
    O termo não filtra (a API não suporta busca por termo). Usamos atividade do dia
    como proxy: muitos jogos hoje = sinal alto para a fonte football. Peso baixo (0.03 BR / 0.01 WORLD).
    """
    key = f"rfb:{geo}:{datetime.now().strftime('%Y%m%d')}"
    if c := cache_get(key):
        return c
    if not RAPIDAPI_KEY:
        return mock_source(20, 55)
    host = "free-api-live-football-data.p.rapidapi.com"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://{host}/football-get-matches-by-date",
                params={"date": datetime.now().strftime("%Y%m%d")},
                headers=rapid_headers(host),
            )
            if r.status_code in (401, 403):
                print(f"[RapidFootball] {r.status_code} — verifique subscription/quota: {r.text[:120]}")
                return mock_source(20, 55)
            data = r.json()
        matches = (data.get("response") or {}).get("matches", []) or data.get("matches", []) or []
        if not matches:
            return mock_source(20, 55)

        # Times BR (nomes únicos — evita falso positivo com "Atletico Madrid" etc.)
        BR_TEAMS = {
            "flamengo","palmeiras","corinthians","são paulo","sao paulo","fluminense",
            "botafogo","grêmio","gremio","cruzeiro","vasco","bahia","fortaleza",
            "athletico paranaense","atlético paranaense","athletico-pr","atlético-pr",
            "atlético mineiro","atletico mineiro","red bull bragantino","cuiabá","cuiaba",
            "juventude","brasileirão","brasileirao","copa do brasil",
        }
        BR_RE = re.compile(r"\b(" + "|".join(re.escape(t) for t in BR_TEAMS) + r")\b")
        def is_br_match(m):
            blob = (str(m.get("home",{})) + " " + str(m.get("away",{})) + " " + str(m.get("tournamentStage",""))).lower()
            return bool(BR_RE.search(blob))
        br_matches = [m for m in matches if is_br_match(m)] if geo == "BR" else []
        active = br_matches if (geo == "BR" and br_matches) else matches

        # Score: atividade do dia. BR_matches quando geo=BR pesam mais.
        base = min(85, len(active) * 2 + 25)
        if geo == "BR" and br_matches:
            base = min(95, base + len(br_matches) * 4)
        velocity = round(min(1.0, len(active) / 60), 2)

        def team_name(t):
            return (t or {}).get("name", "") if isinstance(t, dict) else str(t or "")
        def status_str(s):
            return (s or {}).get("label", "") if isinstance(s, dict) else str(s or "")

        res = {
            "value": base,
            "velocity": velocity,
            "match_count": len(active),
            "br_match_count": len(br_matches),
            "top_matches": [
                {
                    "home": team_name(m.get("home")),
                    "away": team_name(m.get("away")),
                    "status": status_str(m.get("status")),
                    "time": m.get("time", ""),
                }
                for m in active[:3]
            ],
        }
        cache_set(key, res)
        return res
    except Exception as e:
        print(f"[RapidFootball] error: {e}")
        return mock_source(20, 55)

# ============================================================================
# SCORING
# ============================================================================
WEIGHTS_BR = {
    "google_trends": 0.14,
    "reddit":        0.08,
    "youtube":       0.10,
    "spotify":       0.07,
    "tiktok":        0.10,
    "instagram":     0.08,
    "pinterest":     0.07,
    "twitter":       0.07,
    "shazam":        0.05,
    "amazon":        0.05,
    "etsy":          0.04,
    "imdb":          0.04,
    "football":      0.03,
    "mercadolivre":  0.05,
    "rss":           0.03,
}

WEIGHTS_WORLD = {
    "google_trends": 0.16,
    "reddit":        0.10,
    "youtube":       0.12,
    "spotify":       0.08,
    "tiktok":        0.10,
    "instagram":     0.08,
    "pinterest":     0.07,
    "twitter":       0.09,
    "shazam":        0.05,
    "amazon":        0.05,
    "etsy":          0.03,
    "imdb":          0.04,
    "football":      0.01,
    "mercadolivre":  0.00,
    "rss":           0.02,
}

def compute_score(signals: Dict[str, Any], geo: str) -> Dict[str, Any]:
    W = WEIGHTS_BR if geo == "BR" else WEIGHTS_WORLD
    raw = 0.0
    n = 0
    vel = 0.0
    early = 0
    for src, w in W.items():
        s = signals.get(src)
        if not s:
            continue
        n += 1
        boost = 1.3 if geo == "BR" and src in ("reddit", "youtube", "spotify", "mercadolivre", "rss") else 1.0
        raw += s.get("value", 0) * w * boost
        vel += s.get("velocity", 0)
        if s.get("value", 0) > 20 and src in ("reddit", "youtube", "twitter", "pinterest", "rss", "shazam"):
            early += 1
    diversity = 1 + (n / len(W)) * 0.3
    early_bonus = early * 5
    vel_bonus = (vel / max(n, 1)) * 10
    score = min(100, round(raw * diversity + early_bonus + vel_bonus))
    if geo == "BR":
        stage = (
            "Dormente" if score < 15 else
            "Importando" if score < 30 else
            "Emergindo BR" if score < 45 else
            "Pre-Trend BR" if score < 65 else
            "Trending BR" if score < 85 else
            "Pico BR"
        )
    else:
        stage = (
            "Dormente" if score < 15 else
            "Emergindo" if score < 40 else
            "Pre-Trend" if score < 65 else
            "Trending" if score < 85 else
            "Pico"
        )
    return {
        "score": score,
        "stage": stage,
        "early_bonus": early_bonus,
        "vel_bonus": round(vel_bonus, 1),
        "diversity": round(diversity, 3),
        "sources_active": n,
    }

# ============================================================================
# ANTHROPIC
# ============================================================================
async def call_anthropic(prompt: str, max_tokens: int = 1500):
    if not ANTHROPIC_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-5",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            text = r.json()["content"][0]["text"].strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            return json.loads(text)
    except Exception as e:
        print(f"[Anthropic] error: {e}")
        return None

# ============================================================================
# ENDPOINTS
# ============================================================================
@app.get("/")
def root():
    rapid_ok = bool(RAPIDAPI_KEY)
    configured = [
        s for s, v in [
            ("anthropic", ANTHROPIC_KEY),
            ("spotify", SPOTIFY_ID and SPOTIFY_SECRET),
            ("tikapi", TIKAPI_KEY),
            ("rapidapi", rapid_ok),
            ("google_trends", PYTRENDS_OK),
            ("rss", FEEDPARSER_OK),
        ] if v
    ]
    rapid_sources = [
        "tiktok_scraper", "instagram", "pinterest", "twitter",
        "youtube_v3", "shazam", "amazon", "etsy", "imdb", "football_br",
    ] if rapid_ok else []
    return {
        "service": "DMT TrendRadar API v4",
        "status": "ok",
        "configured": configured,
        "rapidapi_sources": rapid_sources,
        "public_sources": ["google_trends", "reddit", "mercadolivre", "rss"],
        "docs": "/docs",
    }

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "4.0.0",
        "pytrends": PYTRENDS_OK,
        "feedparser": FEEDPARSER_OK,
        "anthropic": bool(ANTHROPIC_KEY),
        "spotify": bool(SPOTIFY_ID and SPOTIFY_SECRET),
        "tikapi": bool(TIKAPI_KEY),
        "rapidapi": bool(RAPIDAPI_KEY),
        "cache_entries": len(_cache),
        "time": datetime.now().isoformat(),
    }

@app.get("/score")
async def score_ep(term: str = Query(...), geo: str = Query("BR")):
    geo = geo.upper()
    gp = "BR" if geo == "BR" else ""

    # Roda TODAS as fontes em paralelo
    (
        gt, rd, ml, rss,
        tikapi, sp,
        rtk, rig, rpin, rtw, ryt, rsh, ramz, rety, rimdb, rfb,
    ) = await asyncio.gather(
        asyncio.to_thread(fetch_google_trends, [term], gp),
        fetch_reddit(term, geo),
        fetch_mercadolivre(term, geo),
        fetch_rss(term, geo),
        fetch_tikapi(term, geo),
        fetch_spotify(term, geo),
        fetch_rapid_tiktok(term, geo),
        fetch_rapid_instagram(term, geo),
        fetch_rapid_pinterest(term, geo),
        fetch_rapid_twitter(term, geo),
        fetch_rapid_youtube(term, geo),
        fetch_rapid_shazam(term, geo),
        fetch_rapid_amazon(term, geo),
        fetch_rapid_etsy(term, geo),
        fetch_rapid_imdb(term, geo),
        fetch_rapid_football(term, geo),
    )

    gt_data = gt.get(term, {"value": 40, "velocity": 0.4, "timeline": []})

    # TikTok final = melhor entre RapidAPI e TikAPI
    tk_final = rtk if rtk.get("value", 0) >= tikapi.get("value", 0) else tikapi

    # YouTube final = melhor entre RapidAPI e oficial (só RapidAPI nesta v4)
    yt_final = ryt

    signals = {
        "google_trends": gt_data,
        "reddit":        rd,
        "mercadolivre":  ml,
        "rss":           rss,
        "spotify":       sp,
        "tiktok":        tk_final,
        "instagram":     rig,
        "pinterest":     rpin,
        "twitter":       rtw,
        "youtube":       yt_final,
        "shazam":        rsh,
        "amazon":        ramz,
        "etsy":          rety,
        "imdb":          rimdb,
        "football":      rfb,
    }

    scoring = compute_score(signals, geo)

    real_sources = [k for k, v in signals.items() if v and not v.get("mock")]

    return {
        "term": term,
        "geo": geo,
        **scoring,
        "timeline": gt_data.get("timeline", []),
        "fetched_at": datetime.now().isoformat(),
        "real_sources": real_sources,
        "sources": signals,
        "evidence": {
            "reddit_posts":    rd.get("top_posts", []),
            "youtube_videos":  yt_final.get("top_videos", []),
            "spotify_tracks":  sp.get("top_tracks", []),
            "tiktok_videos":   tk_final.get("top_videos", []),
            "instagram_posts": rig.get("top_posts", []),
            "pinterest_pins":  rpin.get("top_pins", []),
            "amazon_products": ramz.get("top_products", []),
            "etsy_listings":   rety.get("top_listings", []),
            "shazam_tracks":   rsh.get("top_tracks", []),
            "imdb_titles":     rimdb.get("top_titles", []),
            "ml_items":        ml.get("top_items", []),
            "rss_articles":    rss.get("top_articles", []),
        },
    }

@app.post("/briefing")
async def briefing_ep(
    term: str = Query(...),
    geo: str = Query("BR"),
    score: int = Query(50),
    stage: str = Query(""),
    related_terms: str = Query(""),
    gt_value: int = Query(0),
    reddit_value: int = Query(0),
):
    tl = related_terms.split(",") if related_terms else [term]
    rd = f"Google Trends BR:{gt_value}/100, Reddit:{reddit_value}/100" if (gt_value or reddit_value) else ""

    # Contexto musical: Top 10 Shazam BR + músicas Spotify relacionadas ao termo
    shazam_top, spotify_term = await asyncio.gather(
        fetch_shazam_top_br(10),
        fetch_spotify(term, geo),
    )
    shazam_lines = [f"{i+1}. {t['title']} — {t['artist']}" for i, t in enumerate(shazam_top)] if shazam_top else []
    spotify_lines = [
        f"{t.get('name','')} — {t.get('artist','')} (pop {t.get('popularity','?')})"
        for t in (spotify_term.get("top_tracks") or [])
    ]
    music_block = ""
    if shazam_lines:
        music_block += f"\nMúsicas trending BR agora: {'; '.join(shazam_lines)}"
    if spotify_lines:
        music_block += f"\nSpotify relacionadas a '{term}': {'; '.join(spotify_lines)}"

    prompt = f"""Diretor criativo de marca brasileira de roupas personalizadas (camisetas, bonés, estampas).
TREND:{term} GEO:{geo} SCORE:{score}/100 ESTÁGIO:{stage} TERMOS:{', '.join(tl)} {rd}{music_block}
Gere 5 ideias criativas de produto. Sempre que fizer sentido, use trocadilhos com letras/títulos das músicas trending BR e referências culturais ao momento musical (cite o artista/música no campo "motivo"). Responda APENAS JSON válido sem markdown:
{{"ideas":[{{"titulo":"","aplicacao":"","headline":"max 6 palavras","tags":["#t1"],"descricaoVisual":"","corPaleta":["c1"],"precoSugerido":"R$ XX","potencial":"ALTO|MEDIO|ESPECULATIVO","motivo":"1 frase"}}],"urgencia":"","music_context":{{"shazam_top_br":{json.dumps(shazam_lines, ensure_ascii=False)},"spotify_related":{json.dumps(spotify_lines, ensure_ascii=False)}}}}}"""
    result = await call_anthropic(prompt, 2200)
    if not result:
        return {"error": "Configure ANTHROPIC_API_KEY no Railway", "ideas": [], "urgencia": ""}
    # Garante eco do contexto musical mesmo se o modelo omitir
    result.setdefault("music_context", {"shazam_top_br": shazam_lines, "spotify_related": spotify_lines})
    return result

@app.post("/analyze")
async def analyze_ep(
    term: str = Query(...),
    geo: str = Query("BR"),
    score: int = Query(50),
    stage: str = Query(""),
    related_terms: str = Query(""),
    gt_value: int = Query(0),
    reddit_value: int = Query(0),
    velocity: float = Query(0.5),
):
    tl = related_terms.split(",") if related_terms else [term]
    rd = f"Google Trends BR:{gt_value} | Reddit:{reddit_value} | Velocidade:{velocity}" if (gt_value or reddit_value) else ""
    prompt = f"""Analista de pre-trends de moda BR. TREND:{term} GEO:{geo} SCORE:{score}/100 ESTÁGIO:{stage} TERMOS:{', '.join(tl)} {rd}
Responda APENAS JSON válido sem markdown:
{{"urgency":"ALTA|MEDIA|BAIXA","recommendation":"ação 12 palavras","whyNow":"2 frases","productAngles":["p1","p2","p3"],"estampasArtes":["e1","e2"],"riskFactors":["r1"],"timeToActDays":21,"confidenceScore":75,"culturalContextBR":"1-2 frases","leadingSources":["f1"]}}"""
    result = await call_anthropic(prompt)
    if not result:
        return {"error": "Configure ANTHROPIC_API_KEY no Railway", "urgency": "MEDIA", "recommendation": "Configure a API key"}
    return result

@app.get("/trending-br")
async def trending():
    if not PYTRENDS_OK:
        return {"trending": [], "error": "pytrends not available"}
    key = "trending_br"
    if c := cache_get(key, 1800):
        return c
    try:
        pt = TrendReq(hl="pt-BR", tz=-180)
        df = pt.trending_searches(pn="brazil")
        res = {"trending": df[0].tolist()[:20], "fetched_at": datetime.now().isoformat()}
        cache_set(key, res)
        return res
    except Exception as e:
        return {"trending": [], "error": str(e)}

@app.get("/batch-score")
async def batch(terms: str = Query(...), geo: str = Query("BR")):
    tl = [t.strip() for t in terms.split(",")][:10]
    results = {}
    for t in tl:
        try:
            await asyncio.sleep(2)
            results[t] = await score_ep(t, geo)
        except Exception as e:
            results[t] = {"error": str(e), "score": 0}
    return {"results": results, "geo": geo.upper(), "count": len(results)}
