#!/usr/bin/env python3
"""
plexnote.py – Discord-Webhook für neu hinzugefügte Plex-Medien (Tautulli-Trigger)
Überarbeitet 31-05-2025
  • exklusiver File-Lock (pending → sent)
  • Rate-Limit-Retry
  • gemeinsame HTTP-Session
  • Placeholder-Bild
  • kompaktes Logging
  • Python ≤ 3.6 kompatibel (kein Walrus-Operator)
"""

import os, re, sys, html, json, time, argparse, urllib.parse, contextlib, fcntl, unicodedata, requests
from datetime import datetime
from typing import Any, Dict, Optional, Tuple, List


# ═════ Konfiguration ══════════════════════════════════════════
WEBHOOK_URL      = "<YOUR_DISCORD_WEBHOOK_URL>"
TAUTULLI_URL     = "http://<YOUR_TAUTULLI_SERVER>:<PORT>"
TAUTULLI_API_KEY = "<YOUR_TAUTULLI_API_KEY>"
PLEX_BASE_URL    = "https://app.plex.tv"
PLEX_SERVER_ID   = "<YOUR_PLEX_SERVER_ID>"
TMDB_API_KEY     = "<YOUR_TMDB_API_KEY>"

PLACEHOLDER_IMG  = "https://cdn.discordapp.com/attachments/****.jpg"

COLOR_MOVIE, COLOR_SEASON, COLOR_SHOW = 0x1abc9c, 0x3498db, 0xe67e22
MAX_LINE_LEN, MAX_LINES, PLOT_LIMIT   = 45, 4, 150  #40, 4, 150
MAX_WORD_SPLIT_LEN, SINGLE_LINE_LIMIT = 60, 45      #60, 35 
HTTP_TIMEOUT, TMDB_TIMEOUT            = 20, 4
RETRY_ATTEMPTS                        = 3

INDENT = " " * 6
NBSP_INDENT = INDENT.replace(" ", "\u00A0")
ZWS = "\u200B"

POSTED_KEYS_FILE = "posted.json"
POSTED_KEYS_MAX  = 200
EMBED_STYLE      = "boxed"                       # boxed | telegram | klassisch

# ═════ Logging & Vorab-Checks ═════════════════════════════════
def log(level: str, msg: str):
    print(f"{level.upper():7} {msg}")

required = [("WEBHOOK_URL", WEBHOOK_URL), ("TAUTULLI_URL", TAUTULLI_URL),
            ("TAUTULLI_API_KEY", TAUTULLI_API_KEY), ("PLEX_SERVER_ID", PLEX_SERVER_ID)]
missing = [n for n, v in required if not v]
if missing:
    log("error", f"Fehlende Konfiguration: {', '.join(missing)}"); sys.exit(1)

# ═════ Gemeinsame HTTP-Session ═══════════════════════════════
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=2, pool_maxsize=4)
session.mount("http://", adapter); session.mount("https://", adapter)
tget  = lambda url, **kw:  session.get(url,  timeout=kw.pop("timeout", HTTP_TIMEOUT), **kw)
tpost = lambda url, **kw: session.post(url, timeout=kw.pop("timeout", HTTP_TIMEOUT), **kw)

# ═════ File-Lock + Duplikate ═════════════════════════════════
@contextlib.contextmanager
def locked_posted_keys():
    fd = os.open(POSTED_KEYS_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    with open(fd, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = []
            yield data
            f.seek(0); json.dump(data[-POSTED_KEYS_MAX:], f, indent=2); f.truncate()
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def build_dupe_signature(item: Dict) -> str:
    mt  = item.get("media_type", "").lower()
    tit = (item.get("title") or item.get("parent_title") or
           item.get("grandparent_title") or "").strip().lower()
    year   = str(item.get("year") or item.get("originally_available_at") or "")
    season = str(get_season_number(item))
    epi    = str(item.get("media_index") or "")
    if   mt == "movie":   return f"movie::{tit}::{year}"
    if   mt == "season":  return f"season::{tit}::s{season}"
    if   mt == "episode": return f"episode::{tit}::s{season}::e{epi}"
    if   mt == "show":    return f"show::{tit}::{year}"
    return f"unknown::{tit}::{year}"

# ═════ API-Hilfen ════════════════════════════════════════════
def tautulli_api(cmd: str, **params) -> dict:
    params.update({"apikey": TAUTULLI_API_KEY, "cmd": cmd})
    try:
        r = tget(f"{TAUTULLI_URL}/api/v2", params=params)
        return r.json().get("response", {}).get("data", {}) if r.ok else {}
    except Exception as e:
        log("error", f"API {cmd}: {e}"); return {}

def fetch_metadata(rating_key: str, include_children: int = 0) -> dict:
    return tautulli_api("get_metadata", rating_key=rating_key, include_children=include_children)

def guess_latest_rating_key() -> Optional[str]:
    ra = tautulli_api("get_recently_added", count=1).get("recently_added", [])
    return str(ra[0]["rating_key"]) if ra else None

# ═════ Sprache / Subs / Trailer ══════════════════════════════
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

def get_tmdb_trailer_url(tmdb_id: str, is_movie: bool) -> Optional[str]:
    if not tmdb_id: return None
    mtype = "movie" if is_movie else "tv"
    try:
        r = tget(f"https://api.themoviedb.org/3/{mtype}/{tmdb_id}/videos",
                 params={"api_key": TMDB_API_KEY}, timeout=TMDB_TIMEOUT)
        if not r.ok: return None
        vids = r.json().get("results", [])
        for pref in ("de", "en", None):
            for v in vids:
                if v["site"].lower()=="youtube" and v["type"].lower()=="trailer":
                    if pref is None or v.get("iso_639_1","").lower()==pref:
                        return f"https://www.youtube.com/watch?v={v['key']}"
    except Exception as e:
        log("warn", f"TMDB-Trailer: {e}")
    return None

def get_plex_trailer_url(item: dict) -> Optional[str]:
    try:
        media = item.get("Media", [])
        if media and isinstance(media, list):
            part = media[0].get("Part", [])
            if part and isinstance(part, list):
                key = part[0].get("key")
                if key: return f"{PLEX_BASE_URL}{key}"
    except Exception as e:
        log("warn", f"Plex-Trailer: {e}")
    return None

# ═════ Medien-Typ ════════════════════════════════════════════
def detect_media_type(item: dict) -> str:
    mt = item.get("media_type", "").lower()
    if mt in {"movie", "season", "episode"}: return mt
    if mt == "show":
        if item.get("media_index") or item.get("parent_media_index"):
            return "episode"
        return "show"
    return "show"

# ═════ Text-Hilfen ═══════════════════════════════════════════
def normalize_plot_text(txt: str) -> str:
    txt = html.unescape(txt or "")
    txt = re.sub(r"(<br\s*/?>|\|)", " ", txt, flags=re.I)
    txt = re.sub(r"[\s\u00A0\u2000-\u200B\u202F\u205F\u3000]+", " ", txt)
    return txt.strip()

def insert_line_breaks(txt: str, max_len=MAX_LINE_LEN, max_lines=MAX_LINES) -> str:
    words, lines, cur = txt.split(), [], ""
    for w in words:
        if len(w) > MAX_WORD_SPLIT_LEN:
            while len(w) > MAX_LINE_LEN:
                if len(lines) >= max_lines:
                    return "\n".join(lines)
                lines.append(w[:MAX_LINE_LEN - 1] + "-"); w = w[MAX_LINE_LEN - 1:]
            cur += w + " "
        elif len(cur) + len(w) + 1 > max_len:
            if len(lines) >= max_lines:
                return "\n".join(lines)
            lines.append(cur.rstrip()); cur = w + " "
        else:
            cur += w + " "
        if len(lines) >= max_lines:
            return "\n".join(lines)
    if cur and len(lines) < max_lines:
        lines.append(cur.rstrip())
    return "\n".join(lines)

def strip_year_codes(t: str) -> str:
    t = re.sub(r"\s*\(\d{4}\)", "", t)
    t = re.sub(r"S\d{1,2}E\d{1,2}", "", t, flags=re.I)
    return t.strip(" -–:|")

def clean_generic_phrases(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"(S\d{1,2}E\d{1,2}|S\d{1,2}|E\d{1,2})", "", t, flags=re.I)
    t = re.sub(r"(?i)\b(staffel|season|folge|episode|ep|teil|volume|chapter|tba|tbd)\b", "", t)
    t = re.sub(r"(?i)[#:–\-|•]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" -–:|")


# ═════ TMDB-Fallbacks für Plot, Cast, Crew ═════════════════════════════════════
def tmdb_fetch_overview(tmdb_id: str, is_movie: bool) -> Optional[str]:
    if not tmdb_id: return None
    mtype = "movie" if is_movie else "tv"
    try:
        r = tget(f"https://api.themoviedb.org/3/{mtype}/{tmdb_id}",
                 params={"api_key": TMDB_API_KEY, "language": "de-DE"}, timeout=TMDB_TIMEOUT)
        if r.ok:
            return r.json().get("overview")
    except Exception as e:
        log("warn", f"TMDB-Overview-Fallback: {e}")
    return None

def tmdb_fetch_credits(tmdb_id: str, is_movie: bool) -> dict:
    if not tmdb_id: return {}
    mtype = "movie" if is_movie else "tv"
    try:
        r = tget(f"https://api.themoviedb.org/3/{mtype}/{tmdb_id}/credits",
                 params={"api_key": TMDB_API_KEY, "language": "de-DE"}, timeout=TMDB_TIMEOUT)
        if r.ok:
            return r.json()
    except Exception as e:
        log("warn", f"TMDB-Credits-Fallback: {e}")
    return {}

def tmdb_fetch_episode_plot(tmdb_id, season_num, episode_num, lang="de-DE"):
    if not tmdb_id or not season_num or not episode_num:
        return None
    try:
        r = tget(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}/episode/{episode_num}",
            params={"api_key": TMDB_API_KEY, "language": lang},
            timeout=TMDB_TIMEOUT
        )
        if r.ok:
            return r.json().get("overview")
    except Exception as e:
        log("warn", f"TMDB-Episode-Plot: {e}")
    return None


# ═════ Hilfs-Funktionen / Bilder & Links / TMDB-Resolver (vollständig & robust) ═════════════════════

EDITION_KEYWORDS = [
    "extended cut", "director's cut", "special edition", "unrated",
    "ultimate edition", "final cut", "collector's edition", "redux",
    "restored", "anniversary edition", "imax", "3d"
]

def tmdb_fetch_edition(tmdb_id: str) -> Optional[str]:
    """Versucht, aus den Alternative Titles eine Edition-Bezeichnung zu extrahieren."""
    if not tmdb_id:
        return None
    try:
        r = tget(f"https://api.themoviedb.org/3/movie/{tmdb_id}/alternative_titles",
                 params={"api_key": TMDB_API_KEY}, timeout=TMDB_TIMEOUT)
        if r.ok:
            for t in r.json().get("titles", []):
                title_low = t.get("title", "").lower()
                for kw in EDITION_KEYWORDS:
                    if kw in title_low:
                        m = re.search(rf"\b({kw})\b", title_low, flags=re.I)
                        return (m.group(1) if m else t["title"]).title()
    except Exception as e:
        log("warn", f"TMDB-Edition-Fallback: {e}")
    return None

def is_non_latin(text):
    """Erkennt, ob der Text hauptsächlich nicht-lateinische Schriftzeichen enthält."""
    if not text: return False
    # Zähle nicht-lateinische Zeichen (Kanji, Kana, etc.)
    count = sum(1 for c in text if unicodedata.name(c, "").startswith(("CJK", "HIRAGANA", "KATAKANA")))
    return count > 3  # Ab 4 Zeichen als „nicht-lateinisch“ werten

def get_tmdb_episode_title(tmdb_id, season_num, episode_num):
    # Versucht erst deutsch, dann englisch
    for lang in ("de-DE", "en-US"):
        try:
            r = tget(f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}/episode/{episode_num}",
                     params={"api_key": TMDB_API_KEY, "language": lang}, timeout=TMDB_TIMEOUT)
            if r.ok:
                name = r.json().get("name")
                if name and not is_non_latin(name):
                    return name
        except Exception as e:
            log("warn", f"TMDB-Episode-Title ({lang}): {e}")
    return None

def get_season_number(item) -> int:
    """Ermittle die Staffelnummer (Plex liefert manchmal unterschiedliche Felder)."""
    # Zuerst parent_media_index (Staffel), dann index, dann media_index (Episode)
    return int(item.get("parent_media_index") or item.get("index") or item.get("media_index") or 0)


def resolve_tmdb_from_tvdb(tvdb_id: str) -> Optional[str]:
    """Liefert eine TMDB-ID für eine TVDB-ID (sofern gemappt)."""
    if not tvdb_id:
        return None
    try:
        r = tget(f"https://api.themoviedb.org/3/find/{tvdb_id}",
                 params={"api_key": TMDB_API_KEY, "external_source": "tvdb_id"},
                 timeout=TMDB_TIMEOUT)
        if r.ok and r.json().get("tv_results"):
            return str(r.json()["tv_results"][0]["id"])
    except Exception as e:
        log("warn", f"TMDB-Find-Fehler: {e}")
    return None

def search_tmdb_tv_by_name(name: str, year: Optional[str] = None) -> Optional[str]:
    """Sucht per Serientitel nach einer TMDB-TV-ID (deutsch, optional mit Jahr)."""
    if not name:
        return None
    try:
        params = {"api_key": TMDB_API_KEY, "query": name, "language": "de-DE"}
        if year:
            params["first_air_date_year"] = year
        r = tget("https://api.themoviedb.org/3/search/tv", params=params, timeout=TMDB_TIMEOUT)
        if r.ok and r.json().get("results"):
            return str(r.json()["results"][0]["id"])
    except Exception as e:
        log("warn", f"TMDB-Search-Fehler: {e}")
    return None

def collect_guids(meta: dict) -> List[str]:
    """Sammelt alle bekannten GUIDs aus Meta-/Parent-/Grandparent-Daten."""
    return (meta.get("guids") or []) + (meta.get("parent_guids") or []) + (meta.get("grandparent_guids") or [])

def get_tmdb_backdrop(tmdb_id: str, is_movie: bool) -> Optional[str]:
    mtype = "movie" if is_movie else "tv"
    try:
        r = tget(f"https://api.themoviedb.org/3/{mtype}/{tmdb_id}/images",
                 params={"api_key": TMDB_API_KEY, "include_image_language": "de,null,en"},
                 timeout=TMDB_TIMEOUT)
        if r.ok and r.json().get("backdrops"):
            return "https://image.tmdb.org/t/p/w780" + r.json()["backdrops"][0]["file_path"]
    except Exception as e:
        log("warn", f"TMDB-Backdrop-Fehler: {e}")
    return None

def get_tmdb_poster(tmdb_id: str, is_movie: bool) -> Optional[str]:
    mtype = "movie" if is_movie else "tv"
    try:
        r = tget(f"https://api.themoviedb.org/3/{mtype}/{tmdb_id}/images",
                 params={"api_key": TMDB_API_KEY, "include_image_language": "de,null,en"},
                 timeout=TMDB_TIMEOUT)
        if r.ok and r.json().get("posters"):
            return "https://image.tmdb.org/t/p/w500" + r.json()["posters"][0]["file_path"]
    except Exception as e:
        log("warn", f"TMDB-Poster-Fehler: {e}")
    return None

def _extract_guid(guids: List[str], prefix: str) -> Optional[str]:
    """Extrahiert eine GUID bestimmter Quelle (tmdb, tvdb, imdb) aus der GUID-Liste."""
    for g in guids:
        m = re.match(rf"{prefix}://(\d+)", g)
        if m:
            return m.group(1)
    return None

# --- Existenz-Prüfungen für TMDB-Links (robust für alle Medienarten) ---
def tmdb_movie_exists(tmdb_id: str) -> bool:
    try:
        r = tget(f"https://api.themoviedb.org/3/movie/{tmdb_id}", params={"api_key": TMDB_API_KEY}, timeout=TMDB_TIMEOUT)
        return r.ok and r.json().get("id") is not None
    except Exception:
        return False

def tmdb_show_exists(tmdb_id: str) -> bool:
    try:
        r = tget(f"https://api.themoviedb.org/3/tv/{tmdb_id}", params={"api_key": TMDB_API_KEY}, timeout=TMDB_TIMEOUT)
        return r.ok and r.json().get("id") is not None
    except Exception:
        return False

def tmdb_season_exists(tmdb_id: str, season_num: int) -> bool:
    try:
        r = tget(f"https://api.themoviedb.org/3/tv/{tmdb_id}", params={"api_key": TMDB_API_KEY}, timeout=TMDB_TIMEOUT)
        if r.ok:
            for s in r.json().get("seasons", []):
                if int(s.get("season_number", -1)) == int(season_num):
                    return True
    except Exception:
        pass
    return False

def tmdb_episode_exists(tmdb_id: str, season_num: int, episode_num: int) -> bool:
    try:
        r = tget(f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}/episode/{episode_num}",
                 params={"api_key": TMDB_API_KEY}, timeout=TMDB_TIMEOUT)
        return r.ok and r.json().get("id") is not None
    except Exception:
        return False

# --- TMDB/TVDB/IMDb-Link-Generator (universell & robust) ---
def get_tmdb_link(item: dict, series_meta: dict = {}, season_meta: dict = {}) -> str:
    guids = collect_guids(series_meta) + collect_guids(season_meta) + collect_guids(item)
    tmdb = _extract_guid(guids, "tmdb")
    imdb = _extract_guid(guids, "imdb")
    tvdb = _extract_guid(guids, "tvdb")
    mt   = item.get("media_type", "").lower()

    # TMDB-ID ggf. via TVDB abgleichen
    if not tmdb and tvdb:
        alt = resolve_tmdb_from_tvdb(tvdb)
        if alt:
            tmdb = alt

    # Titelsuche, falls weiterhin keine TMDB-ID
    if not tmdb:
        name = (series_meta.get("title") or item.get("parent_title") or item.get("title") or "").strip()
        year = (series_meta.get("parent_year") or item.get("parent_year") or
                series_meta.get("year") or item.get("year") or "")
        alt = search_tmdb_tv_by_name(name, year)
        if alt:
            tmdb = alt

    # Filme
    if mt == "movie":
        if tmdb and tmdb_movie_exists(tmdb):
            return f"https://www.themoviedb.org/movie/{tmdb}?language=de-DE"
        if imdb:
            return f"https://www.imdb.com/title/tt{imdb}"
        return "https://www.themoviedb.org"

    # Serien/Shows
    if tmdb:
        if mt == "season":
            s = get_season_number(item)
            if tmdb_season_exists(tmdb, s):
                return f"https://www.themoviedb.org/tv/{tmdb}/season/{s}?language=de-DE"
            return f"https://www.themoviedb.org/tv/{tmdb}?language=de-DE"
        if mt == "episode":
            s = get_season_number(item)
            e = int(item.get("media_index") or 0)
            if tmdb_episode_exists(tmdb, s, e):
                return f"https://www.themoviedb.org/tv/{tmdb}/season/{s}/episode/{e}?language=de-DE"
            if tmdb_season_exists(tmdb, s):
                return f"https://www.themoviedb.org/tv/{tmdb}/season/{s}?language=de-DE"
            return f"https://www.themoviedb.org/tv/{tmdb}?language=de-DE"
        if mt in {"show", "series", "tvshow", "talkshow"}:
            if tmdb_show_exists(tmdb):
                return f"https://www.themoviedb.org/tv/{tmdb}?language=de-DE"
            if imdb: return f"https://www.imdb.com/title/tt{imdb}"
            if tvdb: return f"https://thetvdb.com/series/{tvdb}"
            return "https://www.themoviedb.org"
        # Fallback für alles andere (z.B. Specials, Minis, etc.)
        if tmdb_show_exists(tmdb):
            return f"https://www.themoviedb.org/tv/{tmdb}?language=de-DE"

    # Fallbacks (immer IMDb vor TVDB)
    if imdb: return f"https://www.imdb.com/title/tt{imdb}"
    if tvdb and mt in {"show", "season", "episode"}:
        return f"https://thetvdb.com/series/{tvdb}"
    return "https://www.themoviedb.org"

def get_plex_link(item: dict) -> str:
    rk  = item["rating_key"]
    key = urllib.parse.quote(f"/library/metadata/{rk}", safe="")
    return f"{PLEX_BASE_URL}/desktop/#!/server/{PLEX_SERVER_ID}/details?key={key}"

# ─── Produktions-Status von TMDB ─────────────────────────────
def get_tmdb_status(item: dict, series_meta: dict = {}, season_meta: dict = {}) -> Optional[str]:
    guids = collect_guids(series_meta) + collect_guids(season_meta) + collect_guids(item)
    tmdb = _extract_guid(guids, "tmdb")
    tvdb = _extract_guid(guids, "tvdb")
    if not tmdb and tvdb:
        alt = resolve_tmdb_from_tvdb(tvdb)
        if alt:
            tmdb = alt
    if not tmdb:
        name = (series_meta.get("title") or item.get("parent_title") or item.get("title", "")).strip()
        alt = search_tmdb_tv_by_name(name)
        if alt:
            tmdb = alt
    if not tmdb:
        return None
    try:
        r = tget(f"https://api.themoviedb.org/3/tv/{tmdb}",
                 params={"api_key": TMDB_API_KEY, "language": "de-DE"},
                 timeout=TMDB_TIMEOUT)
        if r.ok:
            return {
                "Returning Series": "Laufend",
                "Ended":            "Beendet",
                "Canceled":         "Abgesetzt",
                "In Production":    "In Produktion",
                "Planned":          "Geplant",
                "Pilot":            "Pilotfolge",
            }.get(r.json().get("status"))
    except Exception as e:
        log("warn", f"TMDB-Status-Fehler: {e}")
    return None

# ═════ Codec / Auflösung / Studio ═════════════════════════════
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

# ═════ Einrück- / Int-Helper ═════════════════════════════════
indent_block = lambda txt: ZWS + "\n".join(f"{NBSP_INDENT}{l}" for l in txt.splitlines())
safe_int = lambda v, d=0: int(v) if str(v).isdigit() else d

# ═════ Embed-Generator ═══════════════════════════════════════

def build_title(item: Dict, season_meta: dict = {}, series_meta: dict = {}) -> str:
    """
    Erstellt den Titelblock für den Discord-Embed.
    Nutzt Emojis für Medienart, kürzt lange Titel,
    ersetzt unbrauchbare Titel durch Seriennamen (z. B. bei Staffeln),
    und zeigt bei Episoden die Show nur, wenn sie nicht redundant ist.
    """
    mt   = item.get("media_type", "").lower()
    tit  = (item.get("title") or "").strip()
    ptit = (item.get("parent_title") or "").strip()
    gpt  = (item.get("grandparent_title") or "").strip()
    pslug = (item.get("parent_slug") or "").strip()
    clean_title = strip_year_codes(tit)
    maxlen = SINGLE_LINE_LIMIT  # z. B. 45 Zeichen

    # 🔹 Episodentitel ersetzen bei nicht-lateinischer Schrift (z. B. Japanisch)
    if mt == "episode":
        tmdb_id = None
        for g in (series_meta.get("guids") or []) + (item.get("guids") or []):
            m = re.match(r"tmdb://(\d+)", g)
            if m:
                tmdb_id = m.group(1)
                break
        s_idx = get_season_number(item)
        e_idx = safe_int(item.get("media_index"))
        if is_non_latin(clean_title) and tmdb_id and s_idx and e_idx:
            tmdb_title = get_tmdb_episode_title(tmdb_id, s_idx, e_idx)
            if tmdb_title:
                clean_title = tmdb_title

    # 🔹 Kürzen bei Überlänge
    if len(clean_title) > maxlen:
        cut = clean_title[:maxlen].rsplit(" ", 1)[0]
        clean_title = cut + " …"

    # 🔹 Formatierung nach Medientyp
    if mt == "movie":
        return f"🎬 {clean_title}"

    elif mt == "episode":
        # Vermeide doppelte Show-Nennung (wenn bereits im Titel enthalten)
        if gpt and gpt.lower() not in clean_title.lower():
            return f"🍿 {clean_title}\n📺 Aus: {gpt}"
        else:
            return f"🍿 {clean_title}"

    elif mt == "season":
        # Staffel: Wenn Titel fremdsprachig oder nur Zahl, nutze Fallback
        if is_non_latin(tit) or re.search(r"\d", tit):
            fallback = gpt or ptit or pslug.replace("-", " ").title()
            return f"📦 {fallback}"
        else:
            return f"📦 {strip_year_codes(tit)}"

    else:
        return f"📺 {clean_title}"


# -------------- Build Embed ----------------------------------
def build_embed(item: dict, season_meta: dict = {}, series_meta: dict = {}) -> Dict:
    style = EMBED_STYLE.lower()
    mtype = detect_media_type(item)
    color = COLOR_MOVIE if mtype == "movie" else COLOR_SEASON if mtype == "season" else COLOR_SHOW
    embed: Dict = {"title": build_title(item, season_meta, series_meta), "color": color, "fields": []}

    lib = item.get("library_name") or season_meta.get("library_name") or series_meta.get("library_name")
    rel = (item.get("originally_available_at") or season_meta.get("originally_available_at") or
           series_meta.get("originally_available_at"))
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

    # ---------- TMDB-ID extrahieren (wird mehrfach benötigt) ----------
    tmdb_id = None
    for g in (series_meta.get("guids") or []) + (item.get("guids") or []):
        m = re.match(r"tmdb://(\d+)", g)
        if m:
            tmdb_id = m.group(1)
            break

    # ---------- TMDB-Credits ggf. für Fallback laden ----------
    actors    = item.get("actors") or season_meta.get("actors") or series_meta.get("actors") or []
    writers   = item.get("writers")   or series_meta.get("writers")   or []
    producers = item.get("producers") or season_meta.get("producers") or series_meta.get("producers") or []
    directors = item.get("directors") or season_meta.get("directors") or series_meta.get("directors") or []

    tmdb_credits = {}
    if tmdb_id and (not actors or not writers or not producers or not directors):
        tmdb_credits = tmdb_fetch_credits(tmdb_id, mtype == "movie") or {}

    # ---------- Hauptdarsteller (Starring) mit Fallback ----------
    actor = actors[0] if mtype in {"movie", "episode"} and actors else None
    if not actor and tmdb_credits.get("cast"):
        actor = tmdb_credits["cast"][0]["name"]

    # ---------- Autoren / Producer / Regie mit Fallback ----------
    def tmdb_get_crew(job: str) -> Optional[str]:
        if tmdb_credits.get("crew"):
            for p in tmdb_credits["crew"]:
                if p.get("job", "").lower() == job.lower():
                    return p.get("name")
        return None

    writer    = writers[0] if writers else tmdb_get_crew("Writer")
    producer  = producers[0] if producers else tmdb_get_crew("Producer")
    director  = directors[0] if directors else tmdb_get_crew("Director")

    main_info = "Autor: " + writer       if writer   else \
                "Produzent: " + producer if producer else \
                "Regie: " + director     if director else ""

    # ----- Media-Info-Block -----------------------------------
    if style == "boxed":
        mi = []
        if genre: mi.append(f"[**Genre**]  {genre}")
        if rel_fmt: mi.append(f"[**Jahr**]  {rel_fmt}")
        if mtype in {"season", "show", "episode"} and tmdb_status: mi.append(f"[**Status**]  {tmdb_status}")
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
        if lib: embed["fields"].append({"name": "Library", "value": lib, "inline": True})
        if rel_fmt: embed["fields"].append({"name": "Veröffentlicht", "value": rel_fmt, "inline": True})
        if fsk or rating_str:
            embed["fields"].append({"name": "Bewertung", "value": ", ".join(filter(None, [fsk, rating_str])), "inline": True})
        if dauer_str: embed["fields"].append({"name": "Dauer", "value": dauer_str, "inline": True})
        if genre: embed["fields"].append({"name": "Genre", "value": genre, "inline": True})
        if mtype in {"season", "show", "episode"} and tmdb_status:
            embed["fields"].append({"name": "Status", "value": tmdb_status, "inline": True})
        if mtype == "movie" and actor:
            embed["fields"].append({"name": "Starring", "value": actor, "inline": True})

    # ----- Handlung (Plot) mit TMDB-Fallback ----------------------------------
    plot = (item.get("summary") or item.get("plot") or season_meta.get("summary") or
            season_meta.get("plot") or series_meta.get("summary") or series_meta.get("plot"))

    # === NEU: Hole Episoden-Plot direkt von TMDB, falls Episode und kein brauchbarer Plot ===
    if mtype == "episode" and tmdb_id:
        s_idx = get_season_number(item)
        e_idx = safe_int(item.get("media_index"))
        if not plot:
            plot = tmdb_fetch_episode_plot(tmdb_id, s_idx, e_idx, lang="de-DE")

    # Fallback auf TMDB-Overview (Serienbeschreibung oder Film)
    if not plot and tmdb_id:
        plot = tmdb_fetch_overview(tmdb_id, mtype == "movie")


    # ----- Handlung (Plot) mit TMDB-Fallback ----------------------------------
    plot = (item.get("summary") or item.get("plot") or season_meta.get("summary") or
            season_meta.get("plot") or series_meta.get("summary") or series_meta.get("plot"))

    # === NEU: Hole Episoden-Plot direkt von TMDB, falls Episode und kein brauchbarer Plot ===
    if mtype == "episode" and tmdb_id:
        s_idx = get_season_number(item)
        e_idx = safe_int(item.get("media_index"))
        if not plot:
            plot = tmdb_fetch_episode_plot(tmdb_id, s_idx, e_idx, lang="de-DE")

    # Fallback auf TMDB-Overview (Serienbeschreibung oder Film)
    if not plot and tmdb_id:
        plot = tmdb_fetch_overview(tmdb_id, mtype == "movie")

    # --- Bulletproof "…"-Logik ---
    if plot:
        norm = normalize_plot_text(plot)
        too_long = len(norm) > PLOT_LIMIT
        if too_long:
            norm = norm[:PLOT_LIMIT].rstrip()
        norm_wrapped = insert_line_breaks(norm)
        lines = norm_wrapped.splitlines()
        text_nach_zeilenumbruch = " ".join(l.strip() for l in lines)
        plot_abgeschnitten = (too_long or len(text_nach_zeilenumbruch) < len(norm))
        if plot_abgeschnitten and lines:
            # " …" ans letzte sichtbare Wort der letzten Zeile anhängen, falls nicht schon da
            if not lines[-1].endswith("…") and not lines[-1].endswith(" ..."):
                lines[-1] = lines[-1].rstrip(" .") + " …"
        plot_txt = indent_block("\n".join(lines))

    else:
        # zweizeiliger Platzhalter, beide Zeilen kursiv
        placeholder = (
            "_Leider liegen zu diesem Titel noch_\n"
            "_keine weiteren Informationen vor._"
        )
        # identisch eingerückt wie regulärer Plot-Text
        plot_txt = indent_block(placeholder)

    # --- NEU: Titel-Logik NUR für boxed ---
    if style == "boxed" and actor:
        h_title = f"📝 Handlung – Starring ▸ {actor}"
    else:
        h_title = "📝 Handlung"

    embed["fields"].append({
        "name": h_title,
        "value": plot_txt,
        "inline": False
    })

    # ----- Details-Block --------------------------------------
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

    # ----- Edition aus Plex oder TMDB ----------------------------------
    edition = item.get("edition_title") or item.get("edition") or ""
    if not edition and mtype == "movie" and tmdb_id:
        edition = tmdb_fetch_edition(tmdb_id)

    edition_line = f"Edition: {edition}" if edition else ""

    trailer       = get_tmdb_trailer_url(tmdb_id, mtype == "movie")
    plex_trailer  = get_plex_trailer_url(item)
    links         = [f"[TMDB]({get_tmdb_link(item, series_meta, season_meta)})"] if tmdb_id else []
    links.append(f"[PLEX]({get_plex_link(item)})")
    if trailer:         links.append(f"▶️ [Trailer]({trailer})")
    elif plex_trailer:  links.append(f"▶️ [Plex Trailer]({plex_trailer})")
    links_str = " | ".join(links)

    subs_line = ""
    if sub_langs:
        shown = sub_langs[:4]; rem = len(sub_langs) - len(shown)
        subs_line = "Untertitel: " + ", ".join(shown) + (f" + {rem} weitere" if rem > 0 else "")

    details_parts = []
    if subs_line:    details_parts.append(subs_line)
    if edition_line: details_parts.append(edition_line)
    if main_info:
        details_parts.append(f"{main_info} • {links_str}")
    else:
        details_parts.append(links_str)

    details_val = "\n".join(details_parts)

    if style in {"boxed", "telegram"}: details_val = indent_block(details_val)
    embed["fields"].append({"name": details_label, "value": details_val, "inline": False})

    # ----- Bild ------------------------------------------------
    img_url = None
    if tmdb_id:
        if style == "telegram":
            img_url = get_tmdb_poster(tmdb_id, mtype == "movie")
            if not img_url:
                img_url = PLACEHOLDER_IMG
        else:  # boxed oder klassisch
            img_url = get_tmdb_backdrop(tmdb_id, mtype == "movie")
            if not img_url:
                img_url = get_tmdb_poster(tmdb_id, mtype == "movie")
            if not img_url:
                img_url = PLACEHOLDER_IMG
    else:
        img_url = PLACEHOLDER_IMG

    embed["image"] = {"url": img_url}

    codec, res = find_codec_res(item)
    if not codec or not res:
        codec, res = fetch_codec_res(item["rating_key"])
    studio = (item.get("studio") or season_meta.get("studio") or
              series_meta.get("studio") or fetch_studio(item["rating_key"]))
    footer = " • ".join(p for p in (studio, codec, res, datetime.now().strftime("%d.%m.%Y, %H:%M")) if p)
    embed["footer"] = {"text": footer}

    return embed


# ═════ Main-Routine ══════════════════════════════════════════
def get_rating_key():

    # 1. CLI-Argument prüfen
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--rating_key")
    args, _ = ap.parse_known_args()
    if args.rating_key:
        return args.rating_key

    # 2. Alle üblichen ENV-Namen abfragen
    env_names = ["rating_key", "TAUTULLI_RATING_KEY", "RATING_KEY", "ratingKey"]
    for name in env_names:
        rk = os.environ.get(name)
        if rk:
            return rk

    # 3. STDIN als letzte Option (falls z.B. Tautulli ein JSON pusht)
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
        print("FEHLER: rating_key fehlt – Abbruch.", file=sys.stderr)
        print("sys.argv:", sys.argv, file=sys.stderr)
        print("os.environ:", {k: v for k, v in os.environ.items() if 'KEY' in k.upper()}, file=sys.stderr)
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
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = tpost(WEBHOOK_URL, json={"embeds": [embed]})
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
            log("warn", f"Timeout ({HTTP_TIMEOUT}s) – Versuch {attempt}")
        except Exception as e:
            log("warn", f"Discord-POST Fehler: {e}")
        if attempt < RETRY_ATTEMPTS:
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
