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

VERSION = "1.0.1"
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".part")
    tmp.write_text(json.dumps({"quality": q}))
    tmp.replace(SETTINGS_FILE)
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
    STATE_DIR.mkdir(parents=True, exist_ok=True)
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
        tmp = dest.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(dest)
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
        SEARCH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".part")
        tmp.write_text(json.dumps({"raw": raw, "total": total, "next": has_next}))
        tmp.replace(cache)
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
        raw, total, has_next = cached_search(prov, key, query, page, lang)
    except LWError as e:
        fail(e.code, prov.label if e.code == "key_rejected" else e.detail)

    q = resolve_quality(quality)
    for r in raw:
        r["file"] = pick(r.get("files") or [], q)
    items = [r for r in raw if r["id"] > 0 and r["file"]]
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
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
    elif cmd in ("--version", "version"):
        out({"ok": True, "version": VERSION})
    else:
        fail("usage")


if __name__ == "__main__":
    main(sys.argv[1:])
