#!/usr/bin/env python3
"""Search and download video wallpapers for the tinyworkshop.livewall plugin.

Providers: Pixabay (default) and Pexels (only with a key you already have:
Pexels has paused issuing new keys).

Usage (every answer is one JSON object per line on stdout):
  livewall.py key-status
  livewall.py key-set <pixabay|pexels>   reads the key from stdin (one line)
  livewall.py search <text> [page] [lang] [quality]
  livewall.py download <id> [--apply] [--quality <q>]
  livewall.py set-quality <auto|720|1080|1440|2160>
  livewall.py library                    videos already in the backgrounds folders, every theme
  livewall.py apply <path>               set one of those videos as the background
  livewall.py delete <path>              delete one of them (never the current background)
  livewall.py add [--apply] <path>...    copy your own videos into the current theme's folder

lang is an ISO 639-1 code for the search terms. quality is the video
height class to download; "auto" covers every connected monitor (read
from `hyprctl monitors -j`). The choice is remembered in settings.json.

The first provider with a key is used, in the order pixabay, pexels.
Keys live in ~/.local/state/livewall/<provider>.key (mode 0600).
Videos land in ~/.config/omarchy/backgrounds/<theme>/<provider>-<id>[-<name>].mp4,
where Omarchy's background engine (owe) and picker find them.

Errors are reported as {"ok": false, "code": "<code>", "detail": "..."};
the panel translates the code. Standard library only; fixed HTTPS hosts only.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".local/state/livewall"
THEME_NAME_FILE = HOME / ".local/state/omarchy/current/theme.name"
BG_ROOT = HOME / ".config/omarchy/backgrounds"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "livewall"
THUMB_DIR = CACHE_DIR / "thumbs"
SEARCH_DIR = CACHE_DIR / "search"

VERSION = "1.1.1"
UA = f"omarchy-livewall/{VERSION}"
TIMEOUT = 20
PER_PAGE = 24
SEARCH_TTL = 24 * 3600            # Pixabay asks API clients to cache results for 24 hours
MAX_JSON = 4 * 1024 * 1024
MAX_THUMB = 2 * 1024 * 1024
MAX_VIDEO = 400 * 1024 * 1024
QUALITIES = (720, 1080, 1440, 2160)
UPSCALE_OK = 1.12                 # how much a video may be enlarged to fill a monitor


class LWError(Exception):
    def __init__(self, code, detail=""):
        super().__init__(code)
        self.code = code
        self.detail = str(detail)[:200]


def out(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def fail(code, detail=""):
    out({"ok": False, "code": code, "detail": str(detail)[:200]})
    sys.exit(1)


# ------------------------------------------------------- private storage
# Thumbnails, caches and copies can show what is in the user's own videos,
# so they are only readable by the user, whatever the umask.
def private_dir(path):
    """Create path as a 0700 directory, or tighten it if it already exists."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_private(path, data):
    """Write data atomically to path as a 0600 file."""
    if isinstance(data, str):
        data = data.encode()
    tmp = path.with_name(path.name + ".part")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def prepare_cache():
    """Make the cache private, including files left readable by versions before 1.1.1."""
    private_dir(CACHE_DIR)
    for base, dirs, files in os.walk(CACHE_DIR):
        for names, mode in ((dirs, 0o700), (files, 0o600)):
            for n in names:
                p = os.path.join(base, n)
                if not os.path.islink(p):
                    try:
                        os.chmod(p, mode)
                    except OSError:
                        pass
    for d in (THUMB_DIR, SEARCH_DIR, LIB_THUMB_DIR):
        private_dir(d)


# ---------------------------------------------------------------- network
def _open(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _read_limited(resp, limit):
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise LWError("too_big")
    return data


def get_json(url, headers=None):
    try:
        with _open(url, headers) as r:
            return json.loads(_read_limited(r, MAX_JSON))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            # Pixabay answers 400 "[ERROR 400] Invalid or missing API key".
            raise LWError("key_rejected")
        if e.code == 429:
            raise LWError("rate_limited")
        if e.code == 404:
            raise LWError("not_found")
        raise LWError("server_error", e.code)
    except urllib.error.URLError as e:
        raise LWError("network", e.reason)
    except ValueError:
        raise LWError("server_error", "invalid JSON")


def host_ok(url, hosts):
    p = urllib.parse.urlparse(url or "")
    return p.scheme == "https" and p.hostname in hosts


def slug_from_url(url, section):
    m = re.search(rf"/{section}/([a-z0-9-]+?)-\d+/?$", url or "")
    slug = re.sub(r"[^a-z0-9-]", "", m.group(1))[:50] if m else ""
    return "" if slug == "id" else slug


def vclass(w, h):
    """16:9-equivalent height: 1920x1080 -> 1080, 4096x2160 -> 2160, 1920x1012 -> 1012."""
    return min(h, round(w * 9 / 16))


def pick(files, quality):
    """files = [[w, h, link]]: the largest whose class is within quality, else the smallest."""
    files = [f for f in files if f[0] > 0 and f[1] > 0]
    if not files:
        return None
    area = lambda f: f[0] * f[1]
    fits = [f for f in files if vclass(f[0], f[1]) <= quality * 1.1]
    return max(fits, key=area) if fits else min(files, key=area)


def _int(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


# -------------------------------------------------------------- providers
class Pixabay:
    name = "pixabay"
    # Languages Pixabay understands for search terms (API docs, "lang").
    langs = {"cs", "da", "de", "en", "es", "fr", "id", "it", "hu", "nl", "no", "pl", "pt", "ro",
             "sk", "fi", "sv", "tr", "vi", "th", "bg", "ru", "el", "ja", "ko", "zh"}
    label = "Pixabay"
    api = "https://pixabay.com/api/videos/"
    media_hosts = {"cdn.pixabay.com"}
    thumb_hosts = {"cdn.pixabay.com", "i.vimeocdn.com"}

    @staticmethod
    def valid_key(k):
        return bool(re.fullmatch(r"[A-Za-z0-9-]{20,100}", k))

    def _get(self, key, params):
        return get_json(self.api + "?" + urllib.parse.urlencode({"key": key, **params}))

    def test(self, key):
        self._get(key, {"q": "ocean", "per_page": 3})

    def _norm(self, h):
        vids = h.get("videos") or {}
        files, thumb = [], ""
        for size in ("large", "medium", "small", "tiny"):
            v = vids.get(size) or {}
            if host_ok(v.get("url"), self.media_hosts):
                files.append([_int(v.get("width")), _int(v.get("height")), v["url"]])
        for size in ("tiny", "small", "medium"):
            t = (vids.get(size) or {}).get("thumbnail") or ""
            if host_ok(t, self.thumb_hosts):
                thumb = t
                break
        tags = str(h.get("tags") or "")
        return {
            "id": _int(h.get("id")),
            "files": files[:20],
            "thumbUrl": thumb,
            "duration": _int(h.get("duration")),
            "author": str(h.get("user") or "")[:60],
            "title": ", ".join(t.strip() for t in tags.split(",")[:3])[:60],
            "slug": slug_from_url(h.get("pageURL"), "videos"),
        }

    def search(self, key, q, page, lang="en"):
        d = self._get(key, {"q": q, "page": page, "per_page": PER_PAGE, "lang": lang if lang in self.langs else "en",
                            "safesearch": "true", "order": "popular"})
        hits = d.get("hits") or []
        total = _int(d.get("totalHits"))
        return [self._norm(h) for h in hits[:PER_PAGE]], total, page * PER_PAGE < total

    def get(self, key, vid):
        hits = self._get(key, {"id": vid}).get("hits") or []
        if not hits:
            raise LWError("not_found")
        return self._norm(hits[0])


class Pexels:
    name = "pexels"
    label = "Pexels"
    api = "https://api.pexels.com/videos"
    media_hosts = {"videos.pexels.com", "player.vimeo.com"}
    thumb_hosts = {"images.pexels.com"}

    @staticmethod
    def valid_key(k):
        return bool(re.fullmatch(r"[A-Za-z0-9]{20,100}", k))

    def _get(self, key, path, params=None):
        url = self.api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        return get_json(url, {"Authorization": key})

    def test(self, key):
        self._get(key, "/search", {"query": "ocean", "per_page": 1})

    def _norm(self, v):
        files = []
        for f in v.get("video_files") or []:
            if f.get("file_type") == "video/mp4" and host_ok(f.get("link"), self.media_hosts):
                files.append([_int(f.get("width")), _int(f.get("height")), f["link"]])
        thumb = ""
        src = v.get("image") or ""
        if host_ok(src, self.thumb_hosts):
            p = urllib.parse.urlparse(src)
            q = urllib.parse.urlencode({"auto": "compress", "cs": "tinysrgb", "fit": "crop", "w": 480, "h": 270})
            thumb = urllib.parse.urlunparse(p._replace(query=q))
        slug = slug_from_url(v.get("url"), "video")
        return {
            "id": _int(v.get("id")),
            "files": files[:20],
            "thumbUrl": thumb,
            "duration": _int(v.get("duration")),
            "author": str((v.get("user") or {}).get("name") or "")[:60],
            "title": slug.replace("-", " ")[:60],
            "slug": slug,
        }

    def search(self, key, q, page, lang="en"):
        # Pexels takes locales such as it-IT; English terms work everywhere.
        d = self._get(key, "/search", {"query": q, "orientation": "landscape",
                                       "per_page": PER_PAGE, "page": page})
        vids = d.get("videos") or []
        return [self._norm(v) for v in vids[:PER_PAGE]], _int(d.get("total_results")), bool(d.get("next_page"))

    def get(self, key, vid):
        return self._norm(self._get(key, f"/videos/{vid}"))


PROVIDERS = {p.name: p for p in (Pixabay(), Pexels())}
ORDER = ("pixabay", "pexels")


# ------------------------------------------------------------------- keys
def key_file(name):
    return STATE_DIR / f"{name}.key"


def read_key(name):
    env = os.environ.get(f"{name.upper()}_API_KEY", "").strip()
    if env:
        return env
    try:
        return key_file(name).read_text().strip()
    except OSError:
        return ""


def active():
    """First provider with a valid key: (provider, key) or (None, "")."""
    for n in ORDER:
        k = read_key(n)
        if PROVIDERS[n].valid_key(k):
            return PROVIDERS[n], k
    return None, ""


def theme_slug():
    try:
        name = THEME_NAME_FILE.read_text().strip()
    except OSError:
        name = ""
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return slug or "default"


# ------------------------------------------------- monitors and quality
SETTINGS_FILE = STATE_DIR / "settings.json"


def needed(w, h):
    """Smallest 16:9 class that fills a w x h monitor without enlarging it more than UPSCALE_OK."""
    for q in QUALITIES:
        if max(w / (q * 16 / 9), h / q) <= UPSCALE_OK:
            return q
    return QUALITIES[-1]


def monitors():
    try:
        r = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=5)
        data = json.loads(r.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    res = []
    for m in data[:16] if isinstance(data, list) else []:
        if not isinstance(m, dict) or m.get("disabled"):
            continue
        w, h = _int(m.get("width")), _int(m.get("height"))
        if _int(m.get("transform")) % 2:       # rotated 90 or 270 degrees
            w, h = h, w
        if w <= 0 or h <= 0:
            continue
        name = re.sub(r"[^A-Za-z0-9_.-]", "", str(m.get("name") or ""))[:20]
        res.append({"name": name, "width": w, "height": h, "need": needed(w, h)})
    return res


def auto_quality(mons):
    return max((m["need"] for m in mons), default=1440)


def read_quality():
    try:
        q = str(json.loads(SETTINGS_FILE.read_text()).get("quality", "auto"))
    except (OSError, ValueError, AttributeError):
        q = "auto"
    return q if q == "auto" or (q.isdigit() and int(q) in QUALITIES) else "auto"


def resolve_quality(q, mons=None):
    q = str(q or "")
    if q.isdigit() and int(q) in QUALITIES:
        return int(q)
    return auto_quality(monitors() if mons is None else mons)


def cmd_set_quality(q):
    if not (q == "auto" or (q.isdigit() and int(q) in QUALITIES)):
        fail("usage")
    private_dir(STATE_DIR)
    write_private(SETTINGS_FILE, json.dumps({"quality": q}))
    out({"ok": True, "quality": q})


def cmd_key_status():
    prov, _ = active()
    mons = monitors()
    out({"ok": True, "hasKey": prov is not None,
         "provider": prov.name if prov else "", "providerLabel": prov.label if prov else "",
         "theme": theme_slug(), "quality": read_quality(), "auto": auto_quality(mons),
         "qualities": list(QUALITIES), "monitors": mons})


def cmd_key_set(name):
    prov = PROVIDERS.get(name)
    if not prov:
        fail("unknown_provider", name)
    key = sys.stdin.readline().strip()
    if not prov.valid_key(key):
        fail("bad_key_format")
    try:
        prov.test(key)
    except LWError as e:
        fail(e.code, prov.label if e.code == "key_rejected" else e.detail)
    private_dir(STATE_DIR)
    fd = os.open(key_file(name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    os.chmod(key_file(name), 0o600)
    out({"ok": True, "hasKey": True, "provider": prov.name, "providerLabel": prov.label})


# ----------------------------------------------------------------- search
def fetch_thumb(prefix, vid, url):
    dest = THUMB_DIR / f"{prefix}-{vid}.jpg"
    if dest.is_file() and dest.stat().st_size > 0:
        return str(dest)
    if not url:
        return ""
    try:
        with _open(url) as r:
            data = _read_limited(r, MAX_THUMB)
        write_private(dest, data)
        return str(dest)
    except Exception:
        return ""


def downloaded_ids(prov):
    have = set()
    d = BG_ROOT / theme_slug()
    if d.is_dir():
        for f in d.glob(f"{prov.name}-*.mp4"):
            m = re.match(rf"{prov.name}-(\d+)", f.name)
            if m:
                have.add(int(m.group(1)))
    return have


def cached_search(prov, key, query, page, lang):
    cache = SEARCH_DIR / (hashlib.sha256(f"v2\0{prov.name}\0{lang}\0{query.lower()}\0{page}".encode()).hexdigest()[:32] + ".json")
    try:
        if time.time() - cache.stat().st_mtime < SEARCH_TTL:
            c = json.loads(cache.read_text())
            return c["raw"], c["total"], c["next"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raw, total, has_next = prov.search(key, query, page, lang)
    try:
        write_private(cache, json.dumps({"raw": raw, "total": total, "next": has_next}))
    except OSError:
        pass
    return raw, total, has_next


def cmd_search(query, page, lang="en", quality="auto"):
    prov, key = active()
    if not prov:
        fail("no_key")
    query = query.strip()[:80]
    if not query:
        fail("empty_query")
    try:
        page = max(1, min(int(page), 100))
    except ValueError:
        page = 1
    lang = (lang or "en").split("-")[0].lower()
    if not re.fullmatch(r"[a-z]{2,3}", lang):
        lang = "en"
    try:
        prepare_cache()
    except OSError as e:
        fail("io", e.strerror or e)
    try:
        raw, total, has_next = cached_search(prov, key, query, page, lang)
    except LWError as e:
        fail(e.code, prov.label if e.code == "key_rejected" else e.detail)

    q = resolve_quality(quality)
    for r in raw:
        r["file"] = pick(r.get("files") or [], q)
    items = [r for r in raw if r["id"] > 0 and r["file"]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        thumbs = list(pool.map(lambda it: fetch_thumb(prov.name, it["id"], it["thumbUrl"]), items))
    have = downloaded_ids(prov)
    result = []
    for it, t in zip(items, thumbs):
        w, h, _ = it["file"]
        result.append({"id": it["id"], "thumb": t, "duration": it["duration"],
                       "width": w, "height": h, "author": it["author"],
                       "title": it["title"], "downloaded": it["id"] in have})
    # quality: the class actually used, so the panel can show it
    out({"ok": True, "query": query, "page": page, "total": total, "next": has_next,
         "provider": prov.name, "providerLabel": prov.label, "theme": theme_slug(),
         "quality": q, "items": result})


# --------------------------------------------------------------- download
def fetch_video(link, dest):
    part = dest.with_name(dest.name + ".part")
    try:
        with _open(link) as r:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype and ctype not in ("video/mp4", "application/octet-stream", "binary/octet-stream"):
                raise LWError("bad_type", ctype)
            total = _int(r.headers.get("Content-Length"))
            if total > MAX_VIDEO:
                raise LWError("too_big")
            done, last = 0, -1
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_VIDEO:
                        raise LWError("too_big")
                    f.write(chunk)
                    if total:
                        pct = done * 100 // total
                        if pct >= last + 5:
                            last = pct
                            out({"progress": pct})
        with open(part, "rb") as f:
            head = f.read(12)
        if head[4:8] != b"ftyp":
            raise LWError("not_mp4")
        part.replace(dest)
    except LWError:
        part.unlink(missing_ok=True)
        raise
    except urllib.error.URLError as e:
        part.unlink(missing_ok=True)
        raise LWError("network", getattr(e, "reason", e))
    except OSError as e:
        part.unlink(missing_ok=True)
        raise LWError("io", e.strerror or e)


def cmd_download(vid, apply, quality="auto"):
    prov, key = active()
    if not prov:
        fail("no_key")
    if not re.fullmatch(r"\d{1,12}", vid):
        fail("invalid_id")
    try:
        it = prov.get(key, vid)
        f = pick(it.get("files") or [], resolve_quality(quality))
        if not f:
            raise LWError("no_mp4")
        w, h, link = f
        dest_dir = BG_ROOT / theme_slug()
        dest_dir.mkdir(parents=True, exist_ok=True)
        base = f"{prov.name}-{vid}-{vclass(w, h)}p" + (f"-{it['slug']}" if it["slug"] else "")
        dest = dest_dir / f"{base}.mp4"
        if not dest.is_file():
            fetch_video(link, dest)
    except LWError as e:
        fail(e.code, prov.label if e.code == "key_rejected" else e.detail)

    applied = False
    if apply:
        try:
            r = subprocess.run(["omarchy", "theme", "bg", "set", str(dest)],
                               capture_output=True, text=True, timeout=30)
            applied = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            applied = False
    out({"ok": True, "path": str(dest), "width": w, "height": h, "applied": applied})


# ---------------------------------------------------------------- library
# Videos already on disk, in every theme's backgrounds folder. Thumbnails
# come from the search cache when the video was found through Livewall,
# otherwise one frame is taken with ffmpeg (if installed) and cached.
CURRENT_BG = HOME / ".local/state/omarchy/current/background"
LIB_THUMB_DIR = CACHE_DIR / "library"
LIB_META = CACHE_DIR / "library.json"
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
MAX_LIBRARY = 500
MAX_ADD = 50
NAME_RE = re.compile(r"^(pixabay|pexels)-(\d{1,12})(?:-(\d{3,4})p)?(?:-([a-z0-9-]+))?$")


def _resolve(p):
    try:
        return Path(p).resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def current_background():
    r = _resolve(CURRENT_BG)
    return r if r and CURRENT_BG.is_symlink() else None


def is_video_name(p):
    return p.suffix.lower() in VIDEO_EXT and not p.name.startswith(".")


def library_files():
    """[(shown_path, real_path, stat)] for every finished video under BG_ROOT, newest first."""
    res = []
    if not BG_ROOT.is_dir():
        return res
    for d in sorted(BG_ROOT.iterdir()):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not is_video_name(f) or not f.is_file():
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            res.append((f, _resolve(f), st))
    res.sort(key=lambda x: x[2].st_mtime, reverse=True)
    return res[:MAX_LIBRARY]


def known_titles():
    """id -> (title, author) from cached searches, for videos found with Livewall."""
    titles = {}
    if not SEARCH_DIR.is_dir():
        return titles
    for c in SEARCH_DIR.glob("*.json"):
        try:
            for r in json.loads(c.read_text()).get("raw") or []:
                vid = _int(r.get("id"))
                if vid > 0 and r.get("title"):
                    titles[vid] = (str(r["title"])[:60], str(r.get("author") or "")[:60])
        except (OSError, ValueError, AttributeError, TypeError):
            continue
    return titles


def probe(path):
    """(width, height, seconds) of the first video stream; zeros if there is none or ffprobe is missing."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=15)
        d = json.loads(r.stdout or "{}")
        s = (d.get("streams") or [{}])[0]
        return _int(s.get("width")), _int(s.get("height")), round(float((d.get("format") or {}).get("duration") or 0))
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, IndexError):
        return 0, 0, 0


def frame_thumb(path, dest, seconds):
    """One frame of the video as a 480 px JPEG; "" on failure."""
    if dest.is_file() and dest.stat().st_size > 0:
        return str(dest)
    tmp = dest.with_name(dest.stem + ".tmp.jpg")
    at = str(min(2.0, seconds / 3)) if seconds > 0 else "0"
    try:
        # Local files only: a file that is really a playlist cannot make
        # ffmpeg fetch anything from the network. umask 077 keeps the frame private.
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-protocol_whitelist", "file",
                        "-ss", at, "-i", str(path),
                        "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "4", str(tmp)],
                       capture_output=True, timeout=30, umask=0o077)
        if tmp.is_file() and tmp.stat().st_size > 0:
            os.chmod(tmp, 0o600)
            tmp.replace(dest)
            return str(dest)
    except (OSError, subprocess.TimeoutExpired):
        pass
    tmp.unlink(missing_ok=True)
    return ""


def _sig(real, st):
    return hashlib.sha256(f"{real}\0{st.st_size}\0{int(st.st_mtime)}".encode()).hexdigest()[:24]


def library_item(shown, real, st, meta, titles, current, theme):
    sig = _sig(real, st)
    m = meta.get(sig)
    if not (isinstance(m, list) and len(m) == 3):
        m = list(probe(real))
    w, h, dur = (_int(x) for x in m)
    stem = shown.stem
    nm = NAME_RE.match(stem)
    provider, vid, title, author = "", 0, "", ""
    if nm:
        provider, vid = nm.group(1), int(nm.group(2))
        title, author = titles.get(vid, ("", ""))
        if not title and nm.group(4):
            title = nm.group(4).replace("-", " ")[:60]
    if not title:
        title = re.sub(r"[-_]+", " ", stem).strip()[:60]
    thumb = ""
    if provider:
        t = THUMB_DIR / f"{provider}-{vid}.jpg"
        if t.is_file() and t.stat().st_size > 0:
            thumb = str(t)
    if not thumb:
        thumb = frame_thumb(real, LIB_THUMB_DIR / f"{sig}.jpg", dur)
    return sig, [w, h, dur], {
        "path": str(real), "name": shown.name[:120], "theme": shown.parent.name[:60],
        "size": st.st_size, "mtime": int(st.st_mtime), "width": w, "height": h, "duration": dur,
        "thumb": thumb, "title": title, "author": author, "provider": provider, "id": vid,
        "current": current is not None and real == current, "inTheme": shown.parent.name == theme,
    }


def cmd_library():
    theme = theme_slug()
    current = current_background()
    files = library_files()
    try:
        meta = json.loads(LIB_META.read_text())
        meta = meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        meta = {}
    titles = known_titles()
    try:
        prepare_cache()
    except OSError as e:
        fail("io", e.strerror or e)
    with ThreadPoolExecutor(max_workers=4) as pool:
        done = list(pool.map(lambda x: library_item(*x, meta, titles, current, theme), files))
    new_meta = {sig: m for sig, m, _ in done}
    if new_meta != meta:
        try:
            write_private(LIB_META, json.dumps(new_meta))
        except OSError:
            pass
    items = [it for _, _, it in done]
    out({"ok": True, "theme": theme, "dir": str(BG_ROOT / theme), "current": str(current or ""),
         "totalSize": sum(it["size"] for it in items), "items": items})


def library_path(arg):
    """The video named by arg, only if it is a finished video inside BG_ROOT."""
    real, root = _resolve(arg), _resolve(BG_ROOT)
    if not real or not root or root not in real.parents or not is_video_name(real) or not real.is_file():
        fail("bad_path")
    return real


def set_background(path):
    try:
        r = subprocess.run(["omarchy", "theme", "bg", "set", str(path)], capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def cmd_apply(arg):
    path = library_path(arg)
    if not set_background(path):
        fail("apply_failed")
    out({"ok": True, "path": str(path), "applied": True})


def cmd_delete(arg):
    path = library_path(arg)
    if path == current_background():
        fail("in_use")
    try:
        st = path.stat()
        path.unlink()
    except OSError as e:
        fail("io", e.strerror or e)
    (LIB_THUMB_DIR / f"{_sig(path, st)}.jpg").unlink(missing_ok=True)
    out({"ok": True, "path": str(path), "deleted": True, "size": st.st_size})


def safe_name(name):
    """A plain file name for a copied video: no path parts, no leading dot, known extension."""
    p = Path(name)
    stem = re.sub(r"[^\w.-]+", "-", p.stem, flags=re.UNICODE).strip(".-")[:80] or "video"
    return stem + p.suffix.lower()


def add_one(src_arg, dest_dir):
    """Copy one video into dest_dir. Returns (status, path): added, exists, or an error code."""
    src = Path(src_arg)
    if src_arg.startswith("file://"):
        src = Path(urllib.parse.unquote(urllib.parse.urlparse(src_arg).path))
    src = _resolve(src)
    if not src or not src.is_file():
        return "not_found", src_arg
    if not is_video_name(src):
        return "not_video", src.name
    root = _resolve(BG_ROOT)
    if root in src.parents:
        return "exists", str(src)
    w, h, _ = probe(src)
    if (w <= 0 or h <= 0) and shutil.which("ffprobe"):
        return "not_video", src.name
    size = src.stat().st_size
    name = safe_name(src.name)
    dest = dest_dir / name
    n = 2
    while dest.exists():
        if dest.is_file() and dest.stat().st_size == size:
            return "exists", str(dest)
        dest = dest_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    tmp = dest_dir / f".{dest.name}.part"
    try:
        # The copy is private (0600) whatever the source mode or umask:
        # the source may be a private video, and the backgrounds folder
        # is usually readable by other accounts.
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as dst, open(src, "rb") as s:
            shutil.copyfileobj(s, dst, 1024 * 1024)
        tmp.replace(dest)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return "io", e.strerror or str(e)
    return "added", str(dest)


def cmd_add(args):
    """Copy videos the user picked into the current theme's backgrounds folder."""
    apply = "--apply" in args
    srcs = [a for a in args if a != "--apply"][:MAX_ADD]
    if not srcs:
        fail("usage")
    dest_dir = BG_ROOT / theme_slug()
    try:
        if not dest_dir.is_dir():
            # A theme folder made here holds only your own videos: keep it private.
            # An existing folder belongs to Omarchy and is left as it is.
            dest_dir.mkdir(parents=True, mode=0o700)
    except OSError as e:
        fail("io", e.strerror or e)
    added, existing, errors = [], [], []
    for s in srcs:
        status, info = add_one(s, dest_dir)
        if status == "added":
            added.append(info)
        elif status == "exists":
            existing.append(info)
        else:
            errors.append({"code": status, "detail": str(info)[:200]})
    if not added and not existing:
        e = errors[0] if errors else {"code": "generic", "detail": ""}
        fail(e["code"], e["detail"])
    applied = False
    if apply and len(srcs) == 1:
        applied = set_background((added or existing)[0])
    out({"ok": True, "added": added, "existing": existing, "errors": errors, "applied": applied})


# ------------------------------------------------------------------- main
def main(argv):
    if not argv:
        fail("usage")
    cmd, args = argv[0], argv[1:]
    if cmd == "key-status":
        cmd_key_status()
    elif cmd == "key-set" and args:
        cmd_key_set(args[0])
    elif cmd == "search" and args:
        cmd_search(args[0], args[1] if len(args) > 1 else "1", args[2] if len(args) > 2 else "en",
                   args[3] if len(args) > 3 else read_quality())
    elif cmd == "download" and args:
        rest = args[1:]
        quality = rest[rest.index("--quality") + 1] if "--quality" in rest[:-1] else read_quality()
        cmd_download(args[0], "--apply" in rest, quality)
    elif cmd == "set-quality" and args:
        cmd_set_quality(args[0])
    elif cmd == "library":
        cmd_library()
    elif cmd == "apply" and args:
        cmd_apply(args[0])
    elif cmd == "delete" and args:
        cmd_delete(args[0])
    elif cmd == "add" and args:
        cmd_add(args)
    elif cmd in ("--version", "version"):
        out({"ok": True, "version": VERSION})
    else:
        fail("usage")


if __name__ == "__main__":
    main(sys.argv[1:])
