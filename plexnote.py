#!/usr/bin/env python3
"""
plexnote.py – Discord-Webhook für neu hinzugefügte Plex-Medien (Tautulli-Trigger)
Komplett neu, 06/2025, logisch gegliedert in Funktionsblöcke
"""
# ─────────────────────────────────────────────────────────────
# 1. KONFIGURATION & GRUNDLAGEN
# ─────────────────────────────────────────────────────────────

import os, sys, re, time, json, html, argparse, urllib.parse, unicodedata, contextlib, threading
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List
import requests

# ---- Einstellungen ----
class Config:	
	WEBHOOK_URL      = os.getenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123456789012345678/abcDEFghIJklMNopQRstUVwxYZ1234567890abcdEFGHijklmNOPQ")
	TAUTULLI_URL     = os.getenv("TAUTULLI_URL",        "http://localhost:8181")
	TAUTULLI_API_KEY = os.getenv("TAUTULLI_API_KEY",    "1234abcd5678efgh9012ijkl3456mnop")
	TVDB_API_KEY     = os.getenv("TVDB_API_KEY",        "abcd1234-5678-90ef-gh12-ijklmnopqrst")
	TMDB_API_KEY     = os.getenv("TMDB_API_KEY",        "abcd5678efgh9012ijkl3456mnop7890")
	PLEX_BASE_URL    = os.getenv("PLEX_BASE_URL",       "https://app.plex.tv")
	PLEX_SERVER_ID   = os.getenv("PLEX_SERVER_ID",      "1234567890abcdef1234567890abcdef12345678")

	PLACEHOLDER_IMG  = "https://cdn.discordapp.com/attachments/000000000000000000/000000000000000000/placeholder_image.webp"

    POSTED_KEYS_FILE = "posted.json"
    POSTED_KEYS_MAX  = 200

    COLOR_MOVIE, COLOR_SEASON, COLOR_SHOW = 0x1abc9c, 0x3498db, 0xe67e22
    MAX_LINE_LEN, MAX_LINES, PLOT_LIMIT   = 45, 4, 150
    MAX_WORD_SPLIT_LEN, SINGLE_LINE_LIMIT = 60, 36
    HTTP_TIMEOUT, TMDB_TIMEOUT            = 20, 4
    EMBED_STYLE                           = os.getenv("EMBED_STYLE", "boxed").lower() # boxed | telegram | klassisch

    RETRY_TOTAL = 3
    DISCORD_TIMEOUT = 15

INDENT       = " " * 6
NBSP_INDENT  = INDENT.replace(" ", "\u00A0")
ZWS          = "\u200B"

# ---- Einfaches Logging ----
def log(level: str, msg: str):
    print(f"{level.upper():7} {msg}", file=sys.stderr if level in ("error", "warn") else sys.stdout)

# ---- Config-Prüfung ----
def check_config():
    required = [
        ("WEBHOOK_URL", Config.WEBHOOK_URL),
        ("TAUTULLI_URL", Config.TAUTULLI_URL),
        ("TAUTULLI_API_KEY", Config.TAUTULLI_API_KEY),
        ("PLEX_SERVER_ID", Config.PLEX_SERVER_ID)
    ]
    missing = [n for n, v in required if not v]
    if missing:
        log("error", f"Fehlende Konfiguration: {', '.join(missing)}")
        sys.exit(1)

check_config()

# ---- Cross-Platform File-Lock für posted.json ----
class FileLock:
    def __init__(self, path):
        self.path = path
        self.locked = False
        self._fd = None
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        # Existiert die Datei nicht? Dann im Schreibmodus erzeugen.
        if not os.path.exists(self.path):
            self._fd = open(self.path, "w+", encoding="utf-8")
        else:
            self._fd = open(self.path, "r+", encoding="utf-8")
        self._fd.seek(0)
        if os.name == "posix":
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_EX)
        elif os.name == "nt":
            import msvcrt
            msvcrt.locking(self._fd.fileno(), msvcrt.LK_LOCK, 1)
        self.locked = True
        self._fd.seek(0)
        return self._fd

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.locked:
            if os.name == "posix":
                import fcntl
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
            self.locked = False
            self._fd.close()
        self._lock.release()

@contextlib.contextmanager
def locked_posted_keys():
    """JSON-Liste mit File-Lock cross-platform öffnen & pflegen."""
    with FileLock(Config.POSTED_KEYS_FILE) as f:
        try:
            f.seek(0)
            raw = f.read().strip()
            if not raw:
                data = []
            else:
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, ValueError, EOFError):
                    data = []
            yield data
            f.seek(0)
            json.dump(data[-Config.POSTED_KEYS_MAX:], f, indent=2)
            f.truncate()
        except Exception as e:
            log("error", f"FileLock/JSON: {e}")
            yield []

# ─────────────────────────────────────────────────────────────
# 2. HTTP-SESSION & API-CLIENTS (TAUTULLI, TMDB, TVDB)
# ─────────────────────────────────────────────────────────────

# ---- Gemeinsame Requests-Session mit HTTP-Adapter ----
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_maxsize=4)
session.mount("http://", adapter)
session.mount("https://", adapter)
tget  = lambda url, **kw:  session.get(url,  timeout=kw.pop("timeout", Config.HTTP_TIMEOUT), **kw)
tpost = lambda url, **kw: session.post(url, timeout=kw.pop("timeout", Config.HTTP_TIMEOUT), **kw)

# ---- Tautulli API Wrapper ----
def tautulli_api(cmd: str, **params) -> dict:
    params.update({"apikey": Config.TAUTULLI_API_KEY, "cmd": cmd})
    try:
        r = tget(f"{Config.TAUTULLI_URL}/api/v2", params=params)
        return r.json().get("response", {}).get("data", {}) if r.ok else {}
    except Exception as e:
        log("error", f"Tautulli API {cmd}: {e}")
        return {}

def fetch_metadata(rating_key: str, include_children: int = 0) -> dict:
    return tautulli_api("get_metadata", rating_key=rating_key, include_children=include_children)

def guess_latest_rating_key() -> Optional[str]:
    ra = tautulli_api("get_recently_added", count=1).get("recently_added", [])
    return str(ra[0]["rating_key"]) if ra else None

# ---- TMDB API Wrapper ----
def tmdb_get(path, params=None, timeout=None):
    p = params or {}
    p["api_key"] = Config.TMDB_API_KEY
    try:
        r = tget(f"https://api.themoviedb.org/3/{path}", params=p, timeout=timeout or Config.TMDB_TIMEOUT)
        if r.ok: return r.json()
    except Exception as e:
        log("warn", f"TMDB GET {path}: {e}")
    return {}

def tmdb_fetch_overview(tmdb_id: str, is_movie: bool) -> Optional[str]:
    if not tmdb_id: return None
    mtype = "movie" if is_movie else "tv"
    data = tmdb_get(f"{mtype}/{tmdb_id}", params={"language": "de-DE"})
    return data.get("overview")

def tmdb_fetch_credits(tmdb_id: str, is_movie: bool) -> dict:
    if not tmdb_id: return {}
    mtype = "movie" if is_movie else "tv"
    return tmdb_get(f"{mtype}/{tmdb_id}/credits", params={"language": "de-DE"})

def tmdb_fetch_episode_plot(tmdb_id, season_num, episode_num, lang="de-DE"):
    if not tmdb_id or not season_num or not episode_num: return None
    data = tmdb_get(f"tv/{tmdb_id}/season/{season_num}/episode/{episode_num}", params={"language": lang})
    return data.get("overview")

def tmdb_fetch_edition(tmdb_id: str) -> Optional[str]:
    if not tmdb_id: return None
    data = tmdb_get(f"movie/{tmdb_id}/alternative_titles")
    for t in data.get("titles", []):
        low = t.get("title", "").lower()
        for kw in [
            "extended cut", "director's cut", "special edition", "unrated",
            "ultimate edition", "final cut", "collector's edition", "redux",
            "restored", "anniversary edition", "imax", "3d"
        ]:
            if kw in low:
                m = re.search(rf"\b({kw})\b", low, flags=re.I)
                return (m.group(1) if m else t["title"]).title()
    return None

# ---- TVDB API Wrapper inkl. Token-Caching ----
TVDB_TOKEN_CACHE = {"token": None, "ts": 0}
def get_tvdb_token():
    if TVDB_TOKEN_CACHE["token"] and time.time() - TVDB_TOKEN_CACHE["ts"] < 82800:
        return TVDB_TOKEN_CACHE["token"]
    url = "https://api4.thetvdb.com/v4/login"
    payload = {"apikey": Config.TVDB_API_KEY}
    resp = tpost(url, json=payload)
    resp.raise_for_status()
    token = resp.json()["data"]["token"]
    TVDB_TOKEN_CACHE["token"] = token
    TVDB_TOKEN_CACHE["ts"] = time.time()
    return token

def fetch_tvdb_episode_title(episode_id):
    if not episode_id:
        return None
    try:
        token = get_tvdb_token()
        # 1. Versuche Deutsch
        url = f"https://api4.thetvdb.com/v4/episodes/{episode_id}/translations/deu"
        headers = {"Authorization": f"Bearer {token}"}
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("name"):
            return resp.json()["data"]["name"]
        # 2. Fallback Englisch
        url = f"https://api4.thetvdb.com/v4/episodes/{episode_id}/translations/eng"
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("name"):
            return resp.json()["data"]["name"]
        # 3. Fallback: Original aus Episode
        url = f"https://api4.thetvdb.com/v4/episodes/{episode_id}"
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("name"):
            return resp.json()["data"]["name"]
    except Exception as e:
        log("warn", f"TVDB-Episode-Title: {e}")
    return None

def fetch_tvdb_episode_plot(episode_id):
    if not episode_id:
        return None
    try:
        token = get_tvdb_token()
        # 1. Versuche Deutsch
        url = f"https://api4.thetvdb.com/v4/episodes/{episode_id}/translations/deu"
        headers = {"Authorization": f"Bearer {token}"}
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("overview"):
            return resp.json()["data"]["overview"]
        # 2. Fallback Englisch
        url = f"https://api4.thetvdb.com/v4/episodes/{episode_id}/translations/eng"
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("overview"):
            return resp.json()["data"]["overview"]
        # 3. Fallback: Original aus Episode
        url = f"https://api4.thetvdb.com/v4/episodes/{episode_id}"
        resp = tget(url, headers=headers)
        d = resp.json().get("data", {}) if resp.ok else {}
        return d.get("overview") or d.get("summary")
    except Exception as e:
        log("warn", f"TVDB-Episode-Plot: {e}")
    return None

def fetch_tvdb_season_plot(season_id):
    if not season_id:
        return None
    try:
        token = get_tvdb_token()
        # 1. Versuche Deutsch
        url = f"https://api4.thetvdb.com/v4/seasons/{season_id}/translations/deu"
        headers = {"Authorization": f"Bearer {token}"}
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("overview"):
            return resp.json()["data"]["overview"]
        # 2. Fallback Englisch
        url = f"https://api4.thetvdb.com/v4/seasons/{season_id}/translations/eng"
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("overview"):
            return resp.json()["data"]["overview"]
        # 3. Fallback: Original
        url = f"https://api4.thetvdb.com/v4/seasons/{season_id}"
        resp = tget(url, headers=headers)
        d = resp.json().get("data", {}) if resp.ok else {}
        return d.get("overview") or d.get("summary")
    except Exception as e:
        log("warn", f"TVDB-Season-Plot: {e}")
    return None

def fetch_tvdb_show_plot(series_id):
    if not series_id:
        return None
    try:
        token = get_tvdb_token()
        # 1. Versuche Deutsch
        url = f"https://api4.thetvdb.com/v4/series/{series_id}/translations/deu"
        headers = {"Authorization": f"Bearer {token}"}
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("overview"):
            return resp.json()["data"]["overview"]
        # 2. Fallback Englisch
        url = f"https://api4.thetvdb.com/v4/series/{series_id}/translations/eng"
        resp = tget(url, headers=headers)
        if resp.ok and resp.json().get("data", {}).get("overview"):
            return resp.json()["data"]["overview"]
        # 3. Fallback: Original
        url = f"https://api4.thetvdb.com/v4/series/{series_id}"
        resp = tget(url, headers=headers)
        d = resp.json().get("data", {}) if resp.ok else {}
        return d.get("overview") or d.get("summary")
    except Exception as e:
        log("warn", f"TVDB-Series-Plot: {e}")
    return None

def get_tvdb_artwork(series_id: str, kind: str = "fanart") -> Optional[str]:
    """
    kind: 'fanart' (Backdrop) oder 'poster'
    Liefert die erste passende Artwork-URL von TVDB.
    """
    if not series_id:
        return None
    try:
        token  = get_tvdb_token()
        url    = f"https://api4.thetvdb.com/v4/artwork/series/{series_id}"
        hdr    = {"Authorization": f"Bearer {token}"}
        r      = tget(url, headers=hdr, params={"type": kind})
        data   = r.json().get("data", []) if r.ok else []
        if data:
            fn = data[0]["fileName"].lstrip("/")
            return f"https://artworks.thetvdb.com/banners/{fn}"
    except Exception as e:
        log("warn", f"TVDB Artwork ({kind}): {e}")
    return None

# ---- Umbruch Zeile 2 an richtiger Stelle ----    
def smart_linebreak_subtitle(text: str,
                             maxlen: int = 40,
                             minlen: int = 36,
                             prefix: str = "📺 Aus: ") -> str:
    """
    Bricht den Untertitel nach 36-40 Zeichen um, ohne Worte zu trennen.
    Bevorzugt Sonderzeichen ( - : , . | ), sonst letzte Wortgrenze.
    Alles hinter 'Aus:' wird in Folgelinien bündig eingerückt.
    """
    if not text or len(text) <= maxlen:
        return text                                     # nichts zu tun

    # Prefix bestimmen – Standard ist "📺 Aus: ", sonst alles bis erstes Leerzeichen nach :
    if text.startswith(prefix):
        real_prefix = prefix
    else:
        m = re.match(r"^(.+?:\s*)", text)
        real_prefix = m.group(1) if m else ""

    indent = " " * len(real_prefix)                    # gleichbreite Einrückung
    body   = text[len(real_prefix):]                   # Teil nach dem Prefix

    max_body = maxlen - len(real_prefix)
    min_body = max(0,  minlen - len(real_prefix))

    # 1️⃣  Kandidaten: Sonderzeichen im Zielfenster 36-40
    split_pos = -1
    for m in re.finditer(r"[\-:|.,]", body):
        pos = m.end()
        if min_body <= pos <= max_body:
            split_pos = pos                            # größter Treffer gewinnt

    # 2️⃣  Kandidaten: letztes Leerzeichen im Zielfenster
    if split_pos == -1:
        for m in re.finditer(r"\s", body):
            pos = m.start()
            if min_body <= pos <= max_body:
                split_pos = pos

    # 3️⃣  Fallback: letztes Leerzeichen vor max_body
    if split_pos == -1:
        split_pos = body.rfind(" ", 0, max_body)

    # Hat gar nichts gepasst – lieber Original zurückgeben
    if split_pos == -1 or split_pos >= len(body) - 4:
        return text

    # Kopf / Rumpf zuschneiden & Deko-Zeichen entfernen
    head = body[:split_pos].rstrip(" -:|.,")
    tail = body[split_pos:].lstrip(" -:|.,").lstrip()

    return f"{real_prefix}{head}\n{indent}{tail}"

# ─────────────────────────────────────────────────────────────
# 3. TEXT-UTILS, DUPE-HANDLING, MEDIA-TYPE, TITEL, BILD
# ─────────────────────────────────────────────────────────────

# ---- Zeichen- und Format-Utilities ----
safe_int     = lambda v, d=0: int(v) if str(v).isdigit() else d
indent_block = lambda txt: ZWS + "\n".join(f"{NBSP_INDENT}{l}" for l in txt.splitlines())

def normalize_plot_text(txt: str) -> str:
    txt = html.unescape(txt or "")
    txt = re.sub(r"(<br\s*/?>|\|)", " ", txt, flags=re.I)
    txt = re.sub(r"[\s\u00A0\u2000-\u200B\u202F\u205F\u3000]+", " ", txt)
    return txt.strip()

def insert_line_breaks(txt: str, max_len=Config.MAX_LINE_LEN, max_lines=Config.MAX_LINES) -> str:
    words, lines, cur = txt.split(), [], ""
    for w in words:
        if len(w) > Config.MAX_WORD_SPLIT_LEN:
            while len(w) > max_len:
                if len(lines) >= max_lines: return "\n".join(lines)
                lines.append(w[:max_len - 1] + "-"); w = w[max_len - 1:]
            cur += w + " "
        elif len(cur) + len(w) + 1 > max_len:
            if len(lines) >= max_lines: return "\n".join(lines)
            lines.append(cur.rstrip()); cur = w + " "
        else:
            cur += w + " "
        if len(lines) >= max_lines: return "\n".join(lines)
    if cur and len(lines) < max_lines: lines.append(cur.rstrip())
    return "\n".join(lines)

def strip_year_codes(t: str) -> str:
    t = re.sub(r"\s*\(\d{4}\)", "", t)
    t = re.sub(r"S\d{1,2}E\d{1,2}", "", t, flags=re.I)
    return t.strip(" -–:|")

def is_non_latin(text):
    if not text: return False
    count = sum(1 for c in text if unicodedata.name(c, "").startswith(("CJK", "HIRAGANA", "KATAKANA")))
    return count > 3

def clean_generic_phrases(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"(S\d{1,2}E\d{1,2}|S\d{1,2}|E\d{1,2})", "", t, flags=re.I)
    t = re.sub(r"(?i)\b(staffel|season|folge|episode|ep|teil|volume|chapter|tba|tbd)\b", "", t)
    t = re.sub(r"(?i)[#:–\-|•]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" -–:|")

# ---- Media-Typ & Nummernlogik ----
def detect_media_type(item: dict) -> str:
    mt = (item.get("media_type") or "").lower()
    if mt in {"movie", "season", "episode"}:
        return mt
    if mt == "show":
        if item.get("media_index") or item.get("parent_media_index"):
            print("WARNUNG: 'show' mit media_index erkannt! Prüfen, ob das wirklich eine Episode ist.")
        return "show"
    return "show"


def get_season_number(item) -> int:
    mt = item.get("media_type", "").lower()
    if mt == "season":
        # Fallback: media_index > index > 0
        return int(item.get("media_index") or item.get("index") or 0)
    else:
        # Fallback für alle anderen Typen: parent_media_index > index > 0
        return int(item.get("parent_media_index") or item.get("index") or 0)

# ---- Duplikat-Signatur (unique Key pro Eintrag) ----
def build_dupe_signature(item: Dict) -> str:
    mt   = item.get("media_type", "").lower()
    tit  = (item.get("title") or item.get("parent_title") or item.get("grandparent_title") or "").strip().lower()
    year = str(item.get("year") or item.get("originally_available_at") or "")
    season = str(get_season_number(item))
    epi    = str(item.get("media_index") or "")
    if   mt == "movie":   return f"movie::{tit}::{year}"
    if   mt == "season":  return f"season::{tit}::s{season}"
    if   mt == "episode": return f"episode::{tit}::s{season}::e{epi}"
    if   mt == "show":    return f"show::{tit}::{year}"
    return f"unknown::{tit}::{year}"

# ---- TMDB & TVDB GUID-Handling für IDs ----
def collect_guids(meta: dict) -> List[str]:
    return (meta.get("guids") or []) + (meta.get("parent_guids") or []) + (meta.get("grandparent_guids") or [])

def _extract_guid(guids: List[str], prefix: str) -> Optional[str]:
    for g in guids:
        m = re.match(rf"{prefix}://(\d+)", g)
        if m:
            return m.group(1)
    return None

# ---- TVDB- / TMDB-IDs (kompakt & robust) -------------------------------------------
gx = lambda lst, pre: _extract_guid([g for g in lst if g.startswith(f"{pre}://")], pre)

def get_tvdb_series_id(item, season_meta={}, series_meta={}):
    # Suche Serie-ID in ALLEN relevanten Feldern
    for m in (series_meta, season_meta, item):
        for src in ["guids", "parent_guids", "grandparent_guids"]:
            sid = gx(m.get(src, []), "tvdb")
            if sid:
                return sid
    return None

def get_tvdb_episode_id(item, season_meta={}, series_meta={}):
    # Suche Episode-ID in ALLEN relevanten Feldern
    all_guids = (
        item.get("guids", []) +
        item.get("parent_guids", []) +
        item.get("grandparent_guids", []) +
        season_meta.get("guids", []) +
        season_meta.get("parent_guids", []) +
        series_meta.get("guids", []) +
        series_meta.get("parent_guids", [])
    )
    ep_id = gx(all_guids, "tvdb-episode")
    if ep_id:
        return ep_id
    # Fallback: manchmal steckt sie als "tvdb://<epid>", solange sie nicht der Serien-ID entspricht
    fallback_id = gx(all_guids, "tvdb")
    series_id = get_tvdb_series_id(item, season_meta, series_meta)
    if fallback_id and fallback_id != series_id:
        return fallback_id
    return None

def get_tvdb_season_id(item, season_meta={}, series_meta={}):
    # Suche Season-ID in ALLEN relevanten Feldern
    all_guids = (
        collect_guids(item) + collect_guids(season_meta) +
        collect_guids(series_meta)
    )
    season_id = gx(all_guids, "tvdb-season")
    if season_id:
        return season_id
    # Fallback wie oben
    fallback_id = gx(all_guids, "tvdb")
    series_id = get_tvdb_series_id(item, season_meta, series_meta)
    if fallback_id and fallback_id != series_id:
        return fallback_id
    return None

resolve_tmdb_from_tvdb = lambda vid: (lambda r: str(r["tv_results"][0]["id"]) if r and r.get("tv_results") else None)(
    tmdb_get(f"find/{vid}", params={"external_source": "tvdb_id"})) if vid else None

def search_tmdb_tv_by_name(name: str, year: Optional[str] = None):
    if not name: return None
    q = {"query": name, "language": "de-DE"}
    if year: q["first_air_date_year"] = year
    r = tmdb_get("search/tv", params=q)
    return str(r["results"][0]["id"]) if r and r.get("results") else None

def get_tmdb_id(item, series_meta={}, season_meta={}):
    g = collect_guids(series_meta) + collect_guids(season_meta) + collect_guids(item)
    tmdb = gx(g, "tmdb")
    if not tmdb:
        tvdb = gx(g, "tvdb")
        if tvdb: tmdb = resolve_tmdb_from_tvdb(tvdb)
    if not tmdb and item.get("media_type", "").lower() in {"show", "series", "season", "episode", "tvshow"}:
        tmdb = search_tmdb_tv_by_name(
            (series_meta.get("title") or item.get("parent_title") or item.get("title") or "").strip(),
            series_meta.get("parent_year") or item.get("parent_year") or series_meta.get("year") or item.get("year"))
    if not tmdb and item.get("media_type") == "movie":
        r = tmdb_get("search/movie", params={
            "query": (item.get("title") or "").strip(),
            "year": str(item.get("year") or ""), "language": "de-DE"})
        tmdb = str(r["results"][0]["id"]) if r and r.get("results") else None
    return tmdb

def build_tvdb_link(item, season_meta={}, series_meta={}):
    """
    Gibt den besten TVDB-Link für Serie, Staffel oder Episode zurück.
    Benutzt Slug, Staffelnummer und Episode-ID, wenn möglich.
    Fällt auf /episode/<epid> zurück, falls kein Slug.
    """
    # Slug bestimmen (original_title oder slug aus Metadaten; Plex: "original_title", "slug" oder "grandparent_slug")
    slug = (
        item.get("slug") or item.get("parent_slug") or item.get("grandparent_slug") or
        series_meta.get("slug") or series_meta.get("original_title") or
        series_meta.get("slug") or item.get("original_title")
    )
    if slug:
        slug = str(slug).replace(" ", "-").replace("_", "-").lower()

    mt = item.get("media_type", "").lower()
    tvdb_ep_id = get_tvdb_episode_id(item, season_meta, series_meta)
    tvdb_season_id = get_tvdb_season_id(item, season_meta, series_meta)
    tvdb_series_id = get_tvdb_series_id(item, season_meta, series_meta)
    s_idx = get_season_number(item)

    if mt == "episode" and tvdb_ep_id:
        if slug:
            # Bevorzugt: /series/<slug>/episodes/<epid>
            return f"https://thetvdb.com/series/{slug}/episodes/{tvdb_ep_id}"
        else:
            # Fallback: /episode/<epid>
            return f"https://thetvdb.com/episode/{tvdb_ep_id}"
    elif mt == "season" and slug and s_idx:
        return f"https://thetvdb.com/series/{slug}/seasons/official/{s_idx}"
    elif mt in {"show", "series"} and slug:
        return f"https://thetvdb.com/series/{slug}"
    elif tvdb_series_id:
        return f"https://thetvdb.com/series/{tvdb_series_id}"
    return None

# ---- Bild-Logik (TMDB Poster/Backdrop/Placeholder) ----
def get_tmdb_backdrop(tmdb_id: str, is_movie: bool) -> Optional[str]:
    mtype = "movie" if is_movie else "tv"
    data = tmdb_get(f"{mtype}/{tmdb_id}/images", params={"include_image_language": "de,null,en"})
    if data and data.get("backdrops"):
        return "https://image.tmdb.org/t/p/w780" + data["backdrops"][0]["file_path"]
    return None

def get_tmdb_poster(tmdb_id: str, is_movie: bool) -> Optional[str]:
    mtype = "movie" if is_movie else "tv"
    data = tmdb_get(f"{mtype}/{tmdb_id}/images", params={"include_image_language": "de,null,en"})
    if data and data.get("posters"):
        return "https://image.tmdb.org/t/p/w500" + data["posters"][0]["file_path"]
    return None

def choose_image(tmdb_id: str,
                 tvdb_series_id: str,
                 is_movie: bool,
                 style: str) -> str:
    """
    Reihenfolge:
    TMDB-Backdrop → TMDB-Poster → TVDB-Fanart → TVDB-Poster → Placeholder
    """
    # 1) TMDB
    if tmdb_id:
        if style != "telegram":
            img = (get_tmdb_backdrop(tmdb_id, is_movie)
                   or get_tmdb_poster(tmdb_id, is_movie))
        else:                               # Telegram-Embed will Poster
            img = get_tmdb_poster(tmdb_id, is_movie)
        if img:
            return img

    # 2) TVDB
    if tvdb_series_id:
        img = (get_tvdb_artwork(tvdb_series_id, "fanart")
               or get_tvdb_artwork(tvdb_series_id, "poster"))
        if img:
            return img

    # 3) Fallback
    return Config.PLACEHOLDER_IMG

# ---- Dummy-Episodentitel: Regex/Set für generische Titel ----
GENERIC_EP_TITLE = re.compile(
    r"^(folge|episode|ep|teil|chapter)\b.*$|^(tba|tbd|unknown|unbekannt|no title|n\.a\.|not available)$",
    re.I
)
DUMMY_EP_TITLES = {
    "tba", "tbd", "unknown", "unbekannt", "no title", "n.a.", "not available"
}

# ---- Titel-Generator mit Fallback für Episoden ----
def build_title(item: Dict, season_meta: dict = {}, series_meta: dict = {}) -> str:
    mt   = item.get("media_type", "").lower()
    tit  = (item.get("title") or "").strip()
    ptit = (item.get("parent_title") or "").strip()
    gpt  = (item.get("grandparent_title") or "").strip()
    pslug = (item.get("parent_slug") or "").strip()
    clean_title = strip_year_codes(tit)
    maxlen = Config.SINGLE_LINE_LIMIT

    # Fallback-Chain für Episoden
    if mt == "episode":
        title_candidates = [clean_title]
        # Prüfe Plex-Title (generisch? - alles was mit folge/episode/ep/teil beginnt, egal ob mit oder ohne Zahl)
        if (
            GENERIC_EP_TITLE.match(clean_title)
            or clean_title.lower() in DUMMY_EP_TITLES
            or is_non_latin(clean_title)
            or len(clean_title) < 2
        ):
            # 2. TMDB-Titel holen
            tmdb_id = get_tmdb_id(item, series_meta, season_meta)
            s_idx = get_season_number(item)
            e_idx = safe_int(item.get("media_index"))
            tmdb_title = None
            if tmdb_id:
                tmdb_title = tmdb_get(f"tv/{tmdb_id}/season/{s_idx}/episode/{e_idx}", params={"language": "de-DE"}).get("name")
            if tmdb_title:
                title_candidates.append(tmdb_title)
            # Prüfe TMDB-Title (generisch?)
            if not tmdb_title or (
                GENERIC_EP_TITLE.match(tmdb_title)
                or tmdb_title.lower() in DUMMY_EP_TITLES
                or is_non_latin(tmdb_title)
                or len(tmdb_title) < 2
            ):
                # 3. TVDB-Titel holen
                tvdb_ep_id = get_tvdb_episode_id(item, season_meta, series_meta)
                if tvdb_ep_id:
                    tvdb_title = fetch_tvdb_episode_title(tvdb_ep_id)
                    if tvdb_title:
                        title_candidates.append(tvdb_title)
        # Wähle ersten non-generic Titel aus der Kette
        for cand in title_candidates:
            if cand and not (
                GENERIC_EP_TITLE.match(cand)
                or cand.lower() in DUMMY_EP_TITLES
                or is_non_latin(cand)
                or len(cand) < 2
            ):
                clean_title = cand
                break

    if len(clean_title) > maxlen:
        cut = clean_title[:maxlen].rsplit(" ", 1)[0]
        clean_title = cut + " …"

    if mt == "movie":
        return f"🎬 {clean_title}"

    elif mt == "episode":
        if gpt and gpt.lower() not in clean_title.lower():
            subtitle = f"Aus: {gpt}"
            subtitle = smart_linebreak_subtitle(subtitle, maxlen=40) 
            return f"🍿 {clean_title}\n📺 {subtitle}"
        else:
            return f"🍿 {clean_title}"

    elif mt == "season":
        serie = gpt or ptit or pslug.replace("-", " ").title()
        staffel = strip_year_codes(tit)
        if staffel and serie and staffel.lower() not in serie.lower():
            subtitle = f"Aus: {serie}"
            subtitle = smart_linebreak_subtitle(subtitle, maxlen=40)  
            return f"📦 {staffel}\n📺 {subtitle}"
        else:
            return f"📦 {staffel or serie}"

    else:
        return f"📺 {clean_title}"
    

# ─────────────────────────────────────────────────────────────
# 4. EMBED-BUILDING (METADATEN, BLÖCKE, BILD, FOOTER, LINKS)
# ─────────────────────────────────────────────────────────────

# ---- Sprache & Subs-Parsing ----
def get_language_lists(item: dict) -> Tuple[List[str], List[str]]:
    audio, subs, parts = [], [], []
    for mi in item.get("media_info", []):
        parts.extend(mi.get("parts", []))
    for p in parts:
        for st in p.get("streams", []):
            typ = int(st.get("type", 0))
            lang = st.get("languageCode") or st.get("subtitle_language_code") or st.get("language")
            if not lang: continue
            lang = lang.lower()
            if typ == 2: audio.append(lang)
            if typ == 3: subs.append(lang)
    if not audio:
        for p in parts:
            m = re.search(r"\[([A-Za-z]{2}(?:\+[A-Za-z]{2})*)\]", p.get("file", ""))
            if m:
                audio = [c.lower() for c in m.group(1).split("+")]; break
    return sorted(set(audio)), sorted(set(subs))

# ---- Codec/Auflösung/Studio ----
def find_codec_res(obj: Any) -> Tuple[str, str]:
    if isinstance(obj, dict):
        codec = obj.get("video_codec") or obj.get("stream_video_codec")
        res   = (obj.get("video_resolution") or obj.get("video_full_resolution") or
                 obj.get("stream_video_resolution"))
        if codec or res:
            if isinstance(res, str) and res.isdigit():
                res = f"{res}p"
            elif isinstance(res, str) and "x" in res:
                h = res.split("x")[-1]
                if h.isdigit():
                    res = f"{h}p"
            return str(codec).upper(), str(res)
        for v in obj.values():
            cc, rr = find_codec_res(v)
            if cc or rr: return cc, rr
    elif isinstance(obj, list):
        for v in obj:
            cc, rr = find_codec_res(v)
            if cc or rr: return cc, rr
    return "", ""

def fetch_codec_res(rating_key: str) -> Tuple[str, str]:
    meta = fetch_metadata(rating_key, include_children=1)
    if isinstance(meta, list): meta = meta[0]
    return find_codec_res(meta)

def fetch_studio(rating_key: str) -> str:
    meta = fetch_metadata(rating_key)
    if isinstance(meta, list): meta = meta[0]
    return meta.get("studio", "")

# ---- Link-Builder (TMDB / IMDB / TVDB / Plex) ----
def get_tmdb_link(item: dict, series_meta: dict = {}, season_meta: dict = {}) -> str:
    guids = collect_guids(series_meta) + collect_guids(season_meta) + collect_guids(item)
    tmdb = get_tmdb_id(item, series_meta, season_meta)
    imdb = _extract_guid(guids, "imdb")
    mt   = item.get("media_type", "").lower()

    def tmdb_exists_link():
        if tmdb:
            mtype = "movie" if mt == "movie" else "tv"
            path = "movie" if mt == "movie" else "tv"
            if mtype == "movie":
                return f"https://www.themoviedb.org/movie/{tmdb}?language=de-DE"
            else:
                return f"https://www.themoviedb.org/tv/{tmdb}?language=de-DE"
        return None

    if mt == "movie":
        if tmdb: return tmdb_exists_link()
        if imdb: return f"https://www.imdb.com/title/tt{imdb}"
        return "https://www.themoviedb.org"
    if tmdb:
        if mt == "season":
            s = get_season_number(item)
            return f"https://www.themoviedb.org/tv/{tmdb}/season/{s}?language=de-DE"
        if mt == "episode":
            s = get_season_number(item)
            e = int(item.get("media_index") or 0)
            return f"https://www.themoviedb.org/tv/{tmdb}/season/{s}/episode/{e}?language=de-DE"
        return tmdb_exists_link()
    if imdb: return f"https://www.imdb.com/title/tt{imdb}"
    return "https://www.themoviedb.org"

def get_plex_link(item: dict) -> str:
    rk  = item["rating_key"]
    key = urllib.parse.quote(f"/library/metadata/{rk}", safe="")
    return f"{Config.PLEX_BASE_URL}/desktop/#!/server/{Config.PLEX_SERVER_ID}/details?key={key}"

def get_tmdb_trailer_url(tmdb_id: str, is_movie: bool) -> Optional[str]:
    if not tmdb_id: return None
    mtype = "movie" if is_movie else "tv"
    data = tmdb_get(f"{mtype}/{tmdb_id}/videos")
    vids = data.get("results", []) if data else []
    for pref in ("de", "en", None):
        for v in vids:
            if v["site"].lower() == "youtube" and v["type"].lower() == "trailer":
                if pref is None or v.get("iso_639_1", "").lower() == pref:
                    return f"https://www.youtube.com/watch?v={v['key']}"
    return None

def get_plex_trailer_url(item: dict) -> Optional[str]:
    try:
        media = item.get("Media", [])
        if media and isinstance(media, list):
            part = media[0].get("Part", [])
            if part and isinstance(part, list):
                key = part[0].get("key")
                if key: return f"{Config.PLEX_BASE_URL}{key}"
    except Exception as e:
        log("warn", f"Plex-Trailer: {e}")
    return None

def get_tmdb_status(item: dict, series_meta: dict = {}, season_meta: dict = {}) -> Optional[str]:
    tmdb = get_tmdb_id(item, series_meta, season_meta)
    if not tmdb: return None
    data = tmdb_get(f"tv/{tmdb}", params={"language": "de-DE"})
    if data:
        return {
            "Returning Series": "Laufend",
            "Ended":            "Beendet",
            "Canceled":         "Abgesetzt",
            "In Production":    "In Produktion",
            "Planned":          "Geplant",
            "Pilot":            "Pilotfolge",
        }.get(data.get("status"))
    return None

# ---- Embed-Generator Hauptfunktion ----
def build_embed(item: dict, season_meta: dict = {}, series_meta: dict = {}) -> Dict:
    style = Config.EMBED_STYLE
    mtype = detect_media_type(item)
    color = Config.COLOR_MOVIE if mtype == "movie" else Config.COLOR_SEASON if mtype == "season" else Config.COLOR_SHOW
    embed: Dict = {"title": build_title(item, season_meta, series_meta), "color": color, "fields": []}

    lib = item.get("library_name") or season_meta.get("library_name") or series_meta.get("library_name")
    rel = (item.get("originally_available_at") or season_meta.get("originally_available_at") or series_meta.get("originally_available_at"))
    rel_fmt = datetime.strptime(rel, "%Y-%m-%d").strftime("%d.%m.%Y") if rel else None

    RATING_MAP = {
        "TV-Y": "FSK 0", "TV-Y7": "FSK 6", "TV-G": "FSK 0", "TV-PG": "FSK 6", "TV-14": "FSK 12", "TV-MA": "FSK 16",
        "PG": "FSK 6", "PG-13": "FSK 12", "R": "FSK 16", "NC-17": "FSK 18", "UR": "Ungeprüft",
        "BPjM Restricted": "FSK 18+ (indiziert)",
        "de": "FSK 0", "de/0": "FSK 0", "de/6": "FSK 6", "de/12": "FSK 12", "de/12+": "FSK 12+",
        "de/16": "FSK 16", "de/18": "FSK 18"
    }

    cr = item.get("content_rating") or season_meta.get("content_rating") or series_meta.get("content_rating")
    fsk = RATING_MAP.get(cr.strip(), cr.strip()) if cr else None
    rating = (item.get("rating") or item.get("audience_rating") or item.get("user_rating") or
              season_meta.get("rating") or season_meta.get("audience_rating") or season_meta.get("user_rating") or
              series_meta.get("rating") or series_meta.get("audience_rating") or series_meta.get("user_rating"))
    rating_str = f"{float(rating):.1f}/10" if str(rating).replace(".", "", 1).isdigit() else rating

    # Laufzeit
    if mtype == "season":
        children = fetch_metadata(item["rating_key"], include_children=1).get("children", [])
        mins = sum(int(ep.get("duration", 0)) for ep in children) // 60000
    else:
        dur = item.get("duration") or season_meta.get("duration") or series_meta.get("duration")
        mins = int(dur) // 60000 if dur else None
    dauer_str = f"{mins // 60} Std. {mins % 60} Min" if mins and mins >= 60 else (f"{mins} Min" if mins else None)

    genres = item.get("genres") or season_meta.get("genres") or series_meta.get("genres") or []
    genre = ", ".join(genres[:2]) if genres else None
    tmdb_status = get_tmdb_status(item, series_meta, season_meta)
    tmdb_id = get_tmdb_id(item, series_meta, season_meta)
    tvdb_series_id = get_tvdb_series_id(item, season_meta, series_meta)
    tvdb_season_id = get_tvdb_season_id(item, season_meta, series_meta)
    tvdb_ep_id     = get_tvdb_episode_id(item, season_meta, series_meta)

    # --- Cast & Crew ---
    actors    = item.get("actors")    or season_meta.get("actors")    or series_meta.get("actors")    or []
    writers   = item.get("writers")   or season_meta.get("writers")   or series_meta.get("writers")   or []
    producers = item.get("producers") or season_meta.get("producers") or series_meta.get("producers") or []
    directors = item.get("directors") or season_meta.get("directors") or series_meta.get("directors") or []

    tmdb_credits = {}
    if tmdb_id and (not actors or not writers or not producers or not directors):
        tmdb_credits = tmdb_fetch_credits(tmdb_id, mtype == "movie") or {}

    actor = actors[0] if mtype in {"movie", "episode"} and actors else None
    if not actor and tmdb_credits.get("cast"):
        actor = tmdb_credits["cast"][0]["name"]

    def tmdb_get_crew(job: str) -> Optional[str]:
        if tmdb_credits.get("crew"):
            for p in tmdb_credits["crew"]:
                if p.get("job", "").lower() == job.lower():
                    return p.get("name")
        return None

    writer   = writers[0]   if writers   else tmdb_get_crew("Writer")
    producer = producers[0] if producers else tmdb_get_crew("Producer")
    director = directors[0] if directors else tmdb_get_crew("Director")
    main_info = ("Autor: "     + writer)   if writer   else \
                ("Produzent: " + producer) if producer else \
                ("Regie: "     + director) if director else ""

    # --- Media-Info-Block ---
    if style == "boxed":
        mi = []
        if genre:     mi.append(f"[**Genre**]  {genre}")
        if rel_fmt:   mi.append(f"[**Jahr**]  {rel_fmt}")
        if mtype in {"season", "show", "episode"} and tmdb_status:
            mi.append(f"[**Status**]  {tmdb_status}")
        if fsk or rating_str:
            b = rating_str or ""
            if fsk: b += f" ({fsk})" if rating_str else fsk
            mi.append(f"[**Bewertung**]  {b}")
        if dauer_str: mi.append(f"[**Dauer**]  {dauer_str}")
        embed["fields"].append({
            "name": f"📌 **Media-Info:** {lib}" if lib else "📌 **Media-Info:**",
            "value": indent_block("\n".join(mi)),
            "inline": False
        })
    elif style == "telegram":
        bold = lambda t: f"**{t}**" if t else ""
        info = [
            f"Bereich → {bold(lib)}" if lib else "",
            f"Release → {bold(rel_fmt)}" if rel_fmt else "",
            f"Bewertung → {bold(', '.join(filter(None, [fsk, rating_str])))}" if fsk or rating_str else "",
            f"Dauer → {bold(dauer_str)}" if dauer_str else "",
            f"Genre → {bold(genre)}" if genre else "",
            f"Status → {bold(tmdb_status)}" if mtype in {"season", "show", "episode"} and tmdb_status else "",
            f"Starring → {bold(actor)}" if mtype == "movie" and actor else ""
        ]
        info = [x for x in info if x]
        if info:
            embed["description"] = indent_block("\n".join(info))
    else:  # klassisch
        if lib:        embed["fields"].append({"name": "Library", "value": lib, "inline": True})
        if rel_fmt:    embed["fields"].append({"name": "Veröffentlicht", "value": rel_fmt, "inline": True})
        if fsk or rating_str:
            embed["fields"].append({"name": "Bewertung", "value": ", ".join(filter(None, [fsk, rating_str])), "inline": True})
        if dauer_str:  embed["fields"].append({"name": "Dauer", "value": dauer_str, "inline": True})
        if genre:      embed["fields"].append({"name": "Genre", "value": genre, "inline": True})
        if mtype in {"season", "show", "episode"} and tmdb_status:
            embed["fields"].append({"name": "Status", "value": tmdb_status, "inline": True})
        if mtype == "movie" and actor:
            embed["fields"].append({"name": "Starring", "value": actor, "inline": True})

    # --- Handlung / Plot ---
    plot = (item.get("summary") or item.get("plot") or season_meta.get("summary") or
            season_meta.get("plot") or series_meta.get("summary") or series_meta.get("plot"))

    # Plot-Fallbacklogik je nach Medientyp
    if mtype == "episode" and not plot:
        s_idx = get_season_number(item); e_idx = safe_int(item.get("media_index"))
        plot = None
        # TMDB Episode
        if tmdb_id:
            plot = tmdb_fetch_episode_plot(tmdb_id, s_idx, e_idx, lang="de-DE") or \
                   tmdb_fetch_episode_plot(tmdb_id, s_idx, e_idx, lang="en-US")
        # TVDB Episode
        if not plot and tvdb_ep_id:
            plot = fetch_tvdb_episode_plot(tvdb_ep_id)
    elif mtype == "season" and not plot:
        if tmdb_id: plot = tmdb_fetch_overview(tmdb_id, False)
        if not plot and tvdb_season_id:
            plot = fetch_tvdb_season_plot(tvdb_season_id)
    elif mtype in {"show", "series", "tvshow"} and not plot:
        if tmdb_id: plot = tmdb_fetch_overview(tmdb_id, False)
        if not plot and tvdb_series_id:
            plot = fetch_tvdb_show_plot(tvdb_series_id)
    elif mtype == "movie" and not plot:
        plot = tmdb_fetch_overview(tmdb_id, True)

    if plot:
        norm = normalize_plot_text(plot)
        too_long = len(norm) > Config.PLOT_LIMIT
        if too_long: norm = norm[:Config.PLOT_LIMIT].rstrip()
        norm_wrapped = insert_line_breaks(norm)
        lines = norm_wrapped.splitlines()
        abgeschnitten = (too_long or len(" ".join(lines)) < len(norm))
        if abgeschnitten and lines and not lines[-1].endswith(("…", "...")):
            lines[-1] = lines[-1].rstrip(" .") + " …"
        plot_txt = indent_block("\n".join(lines))
    else:
        plot_txt = indent_block("_Leider liegen zu diesem Titel noch_\n_keine weiteren Informationen vor._")

    h_title = f"📝 Handlung – Starring ▸ {actor}" if (style == "boxed" and actor) else "📝 Handlung"
    embed["fields"].append({"name": h_title, "value": plot_txt, "inline": False})

    # --- Details-Block ---
    season_total = safe_int(series_meta.get("childCount"))
    s_idx = get_season_number(item)
    e_idx = safe_int(item.get("media_index"))

    if   mtype == "movie":   details_label = f"🎞️ Details – Film → {item.get('year', '')}"
    elif mtype == "season":  details_label = f"🎞️ Details – Staffel → {s_idx}" + (f" von {season_total}" if season_total else "")
    elif mtype == "episode": details_label = f"🎞️ Details – Serie → S{s_idx:02}E{e_idx:02}"
    elif mtype == "show":    details_label = "🎞️ Details – Serie" + (f" → {season_total} Staffeln" if season_total else "")
    else:                    details_label = "🎞️ Details"

    audio_langs, sub_langs = get_language_lists(item)
    if audio_langs:
        details_label += f" ← {', '.join(audio_langs)}"

    edition = item.get("edition_title") or item.get("edition") or ""
    if not edition and mtype == "movie" and tmdb_id:
        edition = tmdb_fetch_edition(tmdb_id)
    edition_line = f"Edition: {edition}" if edition else ""

    trailer      = get_tmdb_trailer_url(tmdb_id, mtype == "movie")
    plex_trailer = get_plex_trailer_url(item)

    # --- Link-Logik ---
    links = []
    # TMDB-Link **nur** wenn es wirklich eine Seite dazu gibt!
    tmdb_link = None
    if tmdb_id:
        if mtype == "movie":
            tmdb_link = f"https://www.themoviedb.org/movie/{tmdb_id}?language=de-DE"
        elif mtype == "season":
            s = get_season_number(item)
            # Prüfe, ob Staffel auf TMDB existiert
            if tmdb_get(f"tv/{tmdb_id}/season/{s}", params={"language": "de-DE"}).get("id"):
                tmdb_link = f"https://www.themoviedb.org/tv/{tmdb_id}/season/{s}?language=de-DE"
        elif mtype == "episode":
            s = get_season_number(item)
            e = safe_int(item.get("media_index"))
            if tmdb_get(f"tv/{tmdb_id}/season/{s}/episode/{e}", params={"language": "de-DE"}).get("id"):
                tmdb_link = f"https://www.themoviedb.org/tv/{tmdb_id}/season/{s}/episode/{e}?language=de-DE"
        else:
            # Serie
            tmdb_link = f"https://www.themoviedb.org/tv/{tmdb_id}?language=de-DE"

    if tmdb_link:
        links.append(f"[TMDB]({tmdb_link})")
    elif mtype in {"episode", "season", "show", "series"}:
        tvdb_link = build_tvdb_link(item, season_meta, series_meta)
        if tvdb_link:
            links.append(f"[TVDB]({tvdb_link})")

    links.append(f"[PLEX]({get_plex_link(item)})")
    if trailer:           links.append(f"▶️ [Trailer]({trailer})")
    elif plex_trailer:    links.append(f"▶️ [Plex Trailer]({plex_trailer})")
    links_str = " | ".join(links)

    subs_line = ""
    if sub_langs:
        shown = sub_langs[:4]; rem = len(sub_langs) - len(shown)
        subs_line = "Untertitel: " + ", ".join(shown) + (f" + {rem} weitere" if rem > 0 else "")

    details_parts = []
    if subs_line:    details_parts.append(subs_line)
    if edition_line: details_parts.append(edition_line)
    if main_info:    details_parts.append(f"{main_info} • {links_str}")
    else:            details_parts.append(links_str)

    details_val = "\n".join(details_parts)
    if style in {"boxed", "telegram"}:
        details_val = indent_block(details_val)
    embed["fields"].append({"name": details_label, "value": details_val, "inline": False})

    # --- Bildwahl ---
    img_url = choose_image(
        tmdb_id          = tmdb_id,
        tvdb_series_id   = tvdb_series_id,
        is_movie         = (mtype == "movie"),
        style            = style
    )
    embed["image"] = {"url": img_url}

    # --- Footer: Studio • Codec • Auflösung • Datum ---
    codec, res = find_codec_res(item)
    if not codec or not res:
        codec, res = fetch_codec_res(item["rating_key"])
    studio = (item.get("studio") or season_meta.get("studio") or
              series_meta.get("studio") or fetch_studio(item["rating_key"]))
    footer = " • ".join(p for p in (
        studio, codec, res, datetime.now().strftime("%d.%m.%Y, %H:%M")) if p)
    embed["footer"] = {"text": footer}

    return embed

# ─────────────────────────────────────────────────────────────
# 5. MAIN-BLOCK (ARG/ENV/STDIN, DUPES, POST, STATUS)
# ─────────────────────────────────────────────────────────────

def get_rating_key():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--rating_key")
    args, _ = ap.parse_known_args()
    if args.rating_key:
        return args.rating_key
    env_names = ["rating_key", "TAUTULLI_RATING_KEY", "RATING_KEY", "ratingKey"]
    for name in env_names:
        rk = os.environ.get(name)
        if rk:
            return rk
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            if raw.strip():
                if raw.strip().isdigit():
                    return raw.strip()
                data = json.loads(raw)
                for name in env_names:
                    if name in data:
                        return data[name]
        except Exception:
            pass
    return None

def main() -> None:
    rk = get_rating_key() or guess_latest_rating_key()
    if not rk:
        log("error", "rating_key fehlt – Abbruch.")
        sys.exit(1)

    item = fetch_metadata(rk)
    if not item:
        log("error", "Metadaten nicht gefunden"); sys.exit(1)

    # ---- Duplikat-Check + pending-Eintrag --------------------
    with locked_posted_keys() as posted:
        sig = build_dupe_signature(item)
        if any(d.get("rating_key") == str(rk) or d.get("signature") == sig for d in posted):
            log("info", "Bereits gepostet – abgebrochen."); return
        posted.append({"rating_key": str(rk), "signature": sig,
                       "ts": int(time.time()), "status": "pending"})

    # ---- Saison- / Serien-Metadaten laden --------------------
    season_meta, series_meta = {}, {}
    if item.get("media_type") == "episode":
        if item.get("parent_rating_key"):
            season_meta = fetch_metadata(item["parent_rating_key"])
        if item.get("grandparent_rating_key"):
            series_meta = fetch_metadata(item["grandparent_rating_key"])
    elif item.get("media_type") == "season" and item.get("parent_rating_key"):
        series_meta = fetch_metadata(item["parent_rating_key"])

    embed = build_embed(item, season_meta, series_meta)

    # ---- Discord POST mit Retry ------------------------------
    status = "fail"
    for attempt in range(1, Config.RETRY_TOTAL + 1):
        try:
            resp = requests.post(Config.WEBHOOK_URL, json={"embeds": [embed]}, timeout=Config.DISCORD_TIMEOUT)
            if resp.ok:
                log("info", "Embed an Discord gesendet.")
                status = "sent"
                break
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 5))
                log("warn", f"Rate-Limit – warte {wait}s")
                time.sleep(wait); continue
            log("warn", f"Discord-Fehler {resp.status_code}: {resp.text[:200]}")
        except requests.exceptions.Timeout:
            log("warn", f"Timeout ({Config.DISCORD_TIMEOUT}s) – Versuch {attempt}")
        except Exception as e:
            log("warn", f"Discord-POST Fehler: {e}")
        if attempt < Config.RETRY_TOTAL:
            time.sleep(attempt * 2)

    # ---- Status aktualisieren -------------------------------
    with locked_posted_keys() as posted:
        for d in posted:
            if d.get("rating_key") == str(rk) or d.get("signature") == sig:
                d["status"] = status; break

    if status != "sent":
        log("error", "Discord-POST dauerhaft fehlgeschlagen")

if __name__ == "__main__":
    main()
