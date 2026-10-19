#!/usr/bin/env python3
"""
Afterhours 4.1: a small, private podcast player.

Run it:    python3 afterhours.py        (Windows: py afterhours.py)
It opens in its own window, made with the web view built into your system (pywebview).
Install that once from Settings > Window, or with --setup-window. Until then, Afterhours
opens in your browser.

Needs only Python 3.8 or newer. Optional: to caption episodes that have no
captions, install the free speech-recognition add-on once with
    python3 afterhours.py --setup-captions

Why a script and not just a web page: podcast sites don't let a web page read
their feeds directly (browsers block it, a rule called CORS). This script serves
the player and fetches feeds, websites, cover art and Apple Podcasts directory
results on its behalf. It only listens on your own computer.

Options:
  --port 8765        use a different port (each port keeps its own library)
  --window           open in its own window (the default)
  --browser          open in a normal browser tab instead
  --no-browser       don't open anything
  --setup-window     install the window component (pywebview), then exit
  --setup-captions   install the caption recognition add-on, then exit
  --verbose          log every request
See README.md for details.
"""

import argparse
import base64
import collections
import concurrent.futures
import email.utils
import gzip
import hashlib
import html
import http.server
import importlib.util
import inspect
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

APP_NAME = "Afterhours"
APP_ID = "afterhours"
VERSION = "4.1.0"
DEFAULT_PORT = 8765
MAX_BYTES = 25 * 1024 * 1024
MAX_AUDIO = 1536 * 1024 * 1024
TIMEOUT = 25
DATA_DIR = Path(os.environ.get("AFTERHOURS_DATA") or (Path.home() / ".afterhours"))
APP_UA = "Mozilla/5.0 (compatible; Afterhours/%s; personal podcast player)" % VERSION
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
ACCEPT = ("application/rss+xml, application/xml;q=0.9, text/xml;q=0.9, application/json;q=0.8, "
          "text/vtt;q=0.8, text/plain;q=0.6, */*;q=0.5")
HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
RETRY_STATUS = (403, 405, 406, 429, 451, 503)
# Apple's public podcast directory allows roughly 20 requests a minute, so requests are paced.
APPLE_BASE = os.environ.get("AFTERHOURS_APPLE_BASE", "https://itunes.apple.com").rstrip("/")
APPLE_INTERVAL = float(os.environ.get("AFTERHOURS_APPLE_INTERVAL", "3.1"))

CERT_HELP = ("A secure connection couldn't be checked. On a Mac with Python from python.org, open "
             "Applications > Python and run “Install Certificates.command”, then restart Afterhours.")
RATE_HELP = "Apple’s podcast directory asked Afterhours to slow down. Try again in a minute."
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src * data: blob:; media-src * data: blob:; font-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def make_ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi  # optional; helps on some Python installs
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


SSL_CTX = make_ssl_context()


class FetchError(Exception):
    def __init__(self, code, message, upstream=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.upstream = upstream


def _as_fetch_error(e):
    if isinstance(e, FetchError):
        return e
    if isinstance(e, urllib.error.HTTPError):
        return FetchError(502, "The site answered with an error (%s)." % e.code, upstream=e.code)
    if isinstance(e, urllib.error.URLError):
        reason = e.reason
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            return FetchError(502, CERT_HELP)
        if isinstance(reason, socket.timeout) or "timed out" in str(reason):
            return FetchError(504, "The site took too long to answer.")
        return FetchError(502, "Couldn't reach the site (%s)." % reason)
    if isinstance(e, (socket.timeout, TimeoutError)):
        return FetchError(504, "The site took too long to answer.")
    return FetchError(502, "Couldn't load that address (%s)." % e.__class__.__name__)


def open_url(url, accept=ACCEPT, timeout=TIMEOUT):
    """Open a URL. If the site turns Afterhours away, try once more the way a browser would."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise FetchError(400, "That isn't a web address Afterhours can open.")

    def attempt(browser):
        headers = {"User-Agent": BROWSER_UA if browser else APP_UA, "Accept": accept, "Accept-Encoding": "gzip, deflate"}
        if browser:
            headers["Accept-Language"] = "en-GB,en;q=0.9,ja;q=0.8,zh;q=0.7"
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout, context=SSL_CTX)

    try:
        return attempt(False)
    except urllib.error.HTTPError as e:
        if e.code not in RETRY_STATUS:
            raise _as_fetch_error(e)
        try:
            return attempt(True)
        except Exception as e2:
            raise _as_fetch_error(e2)
    except Exception as e:
        raise _as_fetch_error(e)


def fetch_url(url, max_bytes=MAX_BYTES, accept=ACCEPT, partial=False, timeout=TIMEOUT):
    """Fetch a URL and return (bytes, content_type, final_url). With partial=True, keep the first max_bytes."""
    try:
        with open_url(url, accept, timeout) as resp:
            data = resp.read(max_bytes + 1)
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            ctype = resp.headers.get("Content-Type") or "application/octet-stream"
            final_url = resp.geturl()
    except Exception as e:
        raise _as_fetch_error(e)
    if len(data) > max_bytes:
        if not partial:
            raise FetchError(413, "That file is too large to load.")
        data = data[:max_bytes]
    try:
        if encoding == "gzip" or data[:2] == b"\x1f\x8b":
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data) if partial else gzip.decompress(data)
        elif encoding == "deflate":
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompressobj(-zlib.MAX_WBITS).decompress(data)
    except Exception:
        raise FetchError(502, "The site sent data Afterhours couldn't unpack.")
    if len(data) > MAX_BYTES * 4:
        raise FetchError(413, "That file is too large to load.")
    return data, ctype, final_url


def decode_text(data, ctype):
    head = data[:300].decode("latin-1")
    m = re.search(r"<\?xml[^>]*encoding=[\"']([^\"']+)[\"']", head, re.I) or re.search(r"charset=[\"']?([^;\"']+)", ctype, re.I)
    for enc in ((m.group(1).strip() if m else "utf-8"), "utf-8"):
        try:
            return data.decode(enc, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def clean_text(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def html_to_text(s):
    s = re.sub(r"(?i)<br\s*/?>", "\n", s or "")
    s = re.sub(r"(?i)</(p|div|li|h[1-6])>", "\n\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", s)).strip()


# ---------- Apple Podcasts directory, paced and cached ----------
_apple_lock = threading.Lock()
_apple_last = [0.0]
_apple_cache = {}


def apple_get(path, params):
    url = "%s%s?%s" % (APPLE_BASE, path, urllib.parse.urlencode(params))
    hit = _apple_cache.get(url)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    with _apple_lock:
        hit = _apple_cache.get(url)
        if hit and time.time() - hit[0] < 6 * 3600:
            return hit[1]
        wait = APPLE_INTERVAL - (time.monotonic() - _apple_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            data, _, _ = fetch_url(url, max_bytes=4 * 1024 * 1024, accept="application/json")
        except FetchError as e:
            if e.upstream in (403, 429):
                raise FetchError(429, RATE_HELP)
            raise
        finally:
            _apple_last[0] = time.monotonic()
    _apple_cache[url] = (time.time(), data)
    return data


# ---------- feed header, read from the start of a feed only ----------
def feed_info(url):
    data, ctype, final_url = fetch_url(url, max_bytes=768 * 1024, partial=True)
    text = decode_text(data, ctype)
    cut = re.search(r"<item[\s>]", text, re.I)
    head = text[:cut.start()] if cut else text
    no_image = re.sub(r"<image\b.*?</image>", "", head, flags=re.I | re.S)

    def first(pattern, source):
        m = re.search(pattern, source, re.I | re.S)
        return clean_text(m.group(1)) if m else ""

    image = (first(r"<itunes:image\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"']", head)
             or first(r"<image\b[^>]*>.*?<url[^>]*>(.*?)</url>", head))
    return {"title": first(r"<title[^>]*>(.*?)</title>", no_image),
            "author": first(r"<itunes:author[^>]*>(.*?)</itunes:author>", head),
            "image": image, "link": first(r"<link[^>]*>(.*?)</link>", no_image), "finalUrl": final_url}


# ---------- website crawler: used when a show's RSS feed doesn't work ----------
AUDIO_RE = re.compile(r"https?://[^\s\"'<>\\]+?\.(?:mp3|m4a|m4b|aac|ogg|oga|opus|wav|flac)(?:\?[^\s\"'<>\\]*)?", re.I)
AUDIO_KEYS = {"asseturl", "enclosureurl", "audiourl", "audio_url", "streamurl", "stream_url", "mediaurl",
              "media_url", "playurl", "play_url", "mp3url", "downloadurl"}
NEST_KEYS = {"enclosure", "audio", "media", "associatedmedia", "source", "file", "attachment", "mediasource"}
TITLE_KEYS = ("title", "name", "headline", "episodeTitle")
DATE_KEYS = ("datePublished", "pubDate", "releaseDate", "publishedAt", "published_at", "pub_date",
             "publishDate", "date", "createdAt", "uploadDate")
DUR_KEYS = ("duration", "durationInMilliseconds", "duration_ms", "durationMs", "length")
DESC_KEYS = ("description", "summary", "shownotes", "abstract")
IMG_KEYS = ("image", "artwork", "thumbnailUrl", "thumbnail", "cover", "picture")
EPISODE_HINT = re.compile(r"(episode|/ep[-_/]?\d|/e/\d|podcast|/show|/listen|/audio|/radio|/archives?/|/\d{4}/\d{2}/|/p/|/post/|/posts/|/blog/)", re.I)
SKIP_LINK = re.compile(r"(about|contact|privacy|terms|login|signin|sign-in|signup|register|cart|shop|store|/tag/|/tags/|"
                       r"category|categories|/author/|search|subscribe|donate|\.(?:jpe?g|png|gif|svg|webp|pdf|zip)(?:\?|$))", re.I)
FEED_GUESSES = ("feed", "feed/podcast", "podcast/feed", "rss", "rss.xml", "feed.xml", "podcast.xml",
                "index.xml", "?format=rss", "podcast?format=rss", "episodes?format=rss")


class PageScan(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.feeds, self.audio, self.links, self.json_blocks, self.times = [], [], [], [], []
        self.meta, self.title = {}, ""
        self._in_title, self._script, self._a = False, None, None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        join = lambda u: urllib.parse.urljoin(self.base, u.strip())
        if tag == "link":
            rel, typ = a.get("rel", "").lower(), a.get("type", "").lower()
            if "alternate" in rel and ("rss" in typ or "atom" in typ) and a.get("href"):
                self.feeds.append(join(a["href"]))
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "title":
            self._in_title = True
        elif tag in ("audio", "source", "video"):
            src = a.get("src") or a.get("data-src")
            if src:
                self.audio.append((join(src), a.get("title", "")))
        elif tag == "a" and a.get("href"):
            self._a = [join(a["href"]), []]
        elif tag == "script":
            typ, sid = a.get("type", "").lower(), a.get("id", "")
            if "json" in typ or sid == "__NEXT_DATA__":
                self._script = []
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        for k, v in a.items():
            if k.startswith("data-") and v and AUDIO_RE.fullmatch(v.strip()):
                self.audio.append((join(v), ""))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._a:
            self.links.append((self._a[0], " ".join("".join(self._a[1]).split())))
            self._a = None
        elif tag == "script" and self._script is not None:
            self.json_blocks.append("".join(self._script))
            self._script = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._a is not None:
            self._a[1].append(data)
        if self._script is not None:
            self._script.append(data)


def scan_page(text, base):
    scan = PageScan(base)
    try:
        scan.feed(text)
        scan.close()
    except Exception:
        pass
    return scan


def looks_like_rss(text):
    head = text[:5000].lower()
    return "<rss" in head or ("<channel" in head and "<item" in text.lower())


def to_epoch_ms(v):
    if v is None or v == "" or isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        x = float(v)
        return int(x if x > 1e12 else x * 1000) if x > 1e8 else 0
    s = str(v).strip()
    if re.fullmatch(r"\d{10,13}", s):
        return to_epoch_ms(int(s))
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d:
            return int(d.timestamp() * 1000)
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except Exception:
        pass
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return 0
    return 0


def to_seconds(v, key=""):
    if v is None or v == "" or isinstance(v, bool):
        return 0
    k = key.lower()
    if isinstance(v, (int, float)):
        x = float(v)
        if "milli" in k or k.endswith("ms") or x > 100000:
            x /= 1000.0
        return int(x)
    s = str(v).strip()
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", s, re.I)
    if m and any(m.groups()):
        return int(int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0))
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}", s):
        total = 0
        for p in s.split(":"):
            total = total * 60 + int(p)
        return total
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return to_seconds(float(s), key)
    return 0


def pick(d, keys):
    lower = {str(k).lower(): v for k, v in d.items()}
    for key in keys:
        v = lower.get(key.lower())
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        if isinstance(v, dict):
            for kk in ("url", "src", "href", "contentUrl", "@id", "text", "value"):
                if isinstance(v.get(kk), str) and v[kk].strip():
                    return v[kk].strip()
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
            if isinstance(first, dict):
                for kk in ("url", "src", "href"):
                    if isinstance(first.get(kk), str):
                        return first[kk]
    return ""


def episode_from(d, audio, base):
    duration = 0
    for key in DUR_KEYS:
        for k, v in d.items():
            if str(k).lower() == key.lower() and v not in (None, ""):
                duration = to_seconds(v, str(k))
                break
        if duration:
            break
    img = pick(d, IMG_KEYS)
    return {"url": urllib.parse.urljoin(base, audio), "title": clean_text(str(pick(d, TITLE_KEYS)))[:300],
            "date": to_epoch_ms(pick(d, DATE_KEYS)), "duration": duration,
            "image": urllib.parse.urljoin(base, img) if isinstance(img, str) and img.startswith(("http", "/")) else "",
            "desc": html_to_text(str(pick(d, DESC_KEYS)))[:1600],
            "guid": str(d.get("guid") or d.get("eid") or d.get("id") or "")[:200]}


def walk_json(obj, out, base, depth=0):
    """Find episode-like objects (anything carrying an audio file) in a page's embedded JSON."""
    if depth > 14 or len(out) > 600:
        return
    if isinstance(obj, dict):
        audio = None
        for k, v in obj.items():
            key = str(k).lower()
            if isinstance(v, str):
                s = v.strip()
                if AUDIO_RE.fullmatch(s) or (key in AUDIO_KEYS and s.startswith("http")):
                    audio = s
                    break
            elif isinstance(v, dict) and key in NEST_KEYS:
                inner = v.get("source")
                u = v.get("url") or v.get("src") or v.get("contentUrl") or (inner.get("url") if isinstance(inner, dict) else None)
                if isinstance(u, str) and (AUDIO_RE.fullmatch(u.strip()) or key in ("enclosure", "associatedmedia")):
                    audio = u.strip()
                    break
        if audio:
            out.append(episode_from(obj, audio, base))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                walk_json(v, out, base, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            walk_json(v, out, base, depth + 1)


def page_info(scan, base):
    meta = scan.meta
    title = meta.get("og:site_name") or meta.get("og:title") or clean_text(scan.title)
    return {"title": clean_text(title)[:200], "image": urllib.parse.urljoin(base, meta.get("og:image", "")) if meta.get("og:image") else "",
            "description": html_to_text(meta.get("og:description") or meta.get("description") or "")[:1200], "link": base}


def page_episodes(scan, text, base):
    out = []
    for block in scan.json_blocks:
        block = block.strip()
        if block[:1] not in ("{", "["):
            continue
        try:
            walk_json(json.loads(block), out, base)
        except Exception:
            continue
    meta = scan.meta
    page_title = clean_text(meta.get("og:title") or scan.title)
    page_date = to_epoch_ms(meta.get("article:published_time") or meta.get("datepublished") or (scan.times[0] if scan.times else ""))
    page_img = urllib.parse.urljoin(base, meta["og:image"]) if meta.get("og:image") else ""
    page_desc = html_to_text(meta.get("og:description") or meta.get("description") or "")[:1600]
    extra = []
    for key in ("og:audio", "og:audio:url", "og:audio:secure_url", "twitter:player:stream"):
        u = meta.get(key, "")
        if u and AUDIO_RE.fullmatch(urllib.parse.urljoin(base, u)):
            extra.append((urllib.parse.urljoin(base, u), page_title))
    extra += [(src, t) for src, t in scan.audio if AUDIO_RE.fullmatch(src)]
    extra += [(href, label) for href, label in scan.links if AUDIO_RE.fullmatch(href)]
    for u, t in extra:
        out.append({"url": u, "title": clean_text(t) or page_title, "date": page_date, "duration": 0,
                    "image": page_img, "desc": page_desc, "guid": ""})
    if not out:  # last resort: audio addresses written anywhere in the page, such as inside a player's script
        for i, m in enumerate(AUDIO_RE.finditer(text.replace("\\/", "/"))):
            if i >= 60:
                break
            out.append({"url": html.unescape(m.group(0)), "title": "", "date": 0, "duration": 0,
                        "image": page_img, "desc": "", "guid": ""})
    for e in out:
        e["title"] = e["title"] or page_title
        e["image"] = e["image"] or page_img
    return out


def episode_links(scan, base):
    def host_of(u):
        h = urllib.parse.urlsplit(u).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    host, here, seen, out = host_of(base), base.split("#")[0].rstrip("/"), set(), []
    for href, _label in scan.links:
        u = urllib.parse.urlsplit(href)
        if u.scheme not in ("http", "https"):
            continue
        h = host_of(href)
        if h != host and not h.endswith("." + host):
            continue
        clean = urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, u.query, ""))
        path = u.path + ("?" + u.query if u.query else "")
        if clean.rstrip("/") == here or clean in seen or SKIP_LINK.search(path) or not EPISODE_HINT.search(path):
            continue
        seen.add(clean)
        out.append(clean)
        if len(out) >= 16:
            break
    return out


def episode_page(url):
    try:
        data, ctype, final = fetch_url(url, max_bytes=2 * 1024 * 1024, accept=HTML_ACCEPT, partial=True, timeout=12)
    except FetchError:
        return []
    text = decode_text(data, ctype)
    scan = scan_page(text, final)
    eps = page_episodes(scan, text, final)
    if not eps:
        return []
    e = eps[0]  # on an episode's own page, the main audio is the episode
    meta = scan.meta
    e["title"] = clean_text(meta.get("og:title") or scan.title) or e["title"]
    e["date"] = e["date"] or to_epoch_ms(meta.get("article:published_time") or (scan.times[0] if scan.times else ""))
    e["desc"] = e["desc"] or html_to_text(meta.get("og:description") or meta.get("description") or "")[:1600]
    e["link"] = final
    return [e]


STOP_WORDS = {"the", "and", "with", "from", "podcast", "podcasts", "show", "radio", "official", "episodes", "episode",
              "home", "feed", "rss", "channel", "new", "site", "page", "listen"}


def titles_related(a, b):
    """Loose check that two titles name the same show (works for spaced and CJK titles)."""
    na, nb = re.sub(r"[\W_]+", "", (a or "").lower()), re.sub(r"[\W_]+", "", (b or "").lower())
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    words = lambda s: {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 3 and w not in STOP_WORDS}
    if words(a) & words(b):
        return True
    bigrams = lambda s: {s[i:i + 2] for i in range(len(s) - 1)}
    cjk = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
    return bool(cjk.search(na) and cjk.search(nb) and len(bigrams(na) & bigrams(nb)) >= 2)


def feed_title_of(text):
    cut = re.search(r"<item[\s>]", text, re.I)
    head = re.sub(r"<image\b.*?</image>", "", text[:cut.start()] if cut else text[:20000], flags=re.I | re.S)
    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    return clean_text(m.group(1)) if m else ""


def first_working_feed(candidates, wants=None):
    """Check candidate feed addresses in parallel; return the first (in priority order) with audio episodes.
    Guessed addresses (wants given) must also carry a title that matches the show."""
    def check(url):
        try:
            data, ctype, final = fetch_url(url, max_bytes=2 * 1024 * 1024, partial=True, timeout=10)
        except FetchError:
            return ""
        text = decode_text(data, ctype)
        if not (looks_like_rss(text) and re.search(r"<enclosure\b", text, re.I)):
            return ""
        if wants is not None and not any(titles_related(feed_title_of(text), w) for w in wants if w):
            return ""
        return final
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(check, candidates):
            if result:
                return result
    return ""


_crawl_cache = {}


def crawl(url, hint=""):
    hit = _crawl_cache.get(url + "|" + hint)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    data, ctype, final = fetch_url(url, max_bytes=4 * 1024 * 1024, accept=HTML_ACCEPT + ",application/rss+xml;q=0.9", partial=True, timeout=15)
    text = decode_text(data, ctype)
    if looks_like_rss(text):
        if not re.search(r"<enclosure\b", text, re.I):
            raise FetchError(422, "That feed has no audio episodes.")
        result = {"source": "rss", "feedUrl": final, "pageUrl": final, "show": {}, "episodes": []}
    else:
        scan = scan_page(text, final)
        explicit = list(scan.feeds)
        explicit += [h for h, _ in scan.links if re.search(r"(/feed\b|/rss\b|rss\.xml|feed\.xml|podcast\.xml|format=rss)", h, re.I)]
        for apple_id in list(dict.fromkeys(re.findall(r"podcasts\.apple\.com/[^\"'\s<>]*?/id(\d{5,12})", text)))[:2]:
            try:
                for item in json.loads(apple_get("/lookup", {"id": apple_id, "country": "gb"}).decode("utf-8")).get("results", []):
                    if item.get("feedUrl"):
                        explicit.append(item["feedUrl"])
            except Exception:
                pass
        root = "{0.scheme}://{0.netloc}/".format(urllib.parse.urlsplit(final))
        page = final.split("?")[0].split("#")[0]
        page = page if page.endswith("/") else page + "/"
        guesses = []
        for guess in FEED_GUESSES:
            guesses += [urllib.parse.urljoin(page, guess), urllib.parse.urljoin(root, guess)]
        tidy = lambda xs: [c for c in dict.fromkeys(xs) if c.startswith("http") and c.rstrip("/") != final.rstrip("/")]
        explicit = tidy(explicit)[:10]
        guesses = [g for g in tidy(guesses) if g not in explicit][:14]
        show = page_info(scan, final)
        feed = first_working_feed(explicit) or first_working_feed(guesses, wants=[hint, show["title"], clean_text(scan.title)])
        if feed:
            result = {"source": "rss", "feedUrl": feed, "pageUrl": final, "show": show, "episodes": []}
        else:
            eps = page_episodes(scan, text, final)
            if len(eps) < 3:
                links = episode_links(scan, final)
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    for got in pool.map(episode_page, links):
                        eps.extend(got)
            seen, unique = set(), []
            for e in eps:
                if e["url"] not in seen:
                    seen.add(e["url"])
                    unique.append(e)
            if sum(1 for e in unique if e["date"]) >= max(1, len(unique) // 2):
                unique.sort(key=lambda e: e["date"], reverse=True)
            if not unique:
                raise FetchError(404, "Afterhours couldn’t find a feed or any episodes on that page.")
            result = {"source": "web", "feedUrl": "", "pageUrl": final, "show": show, "episodes": unique[:200]}
    _crawl_cache[url + "|" + hint] = (time.time(), result)
    return result


# ---------- cover art through this origin, so the page can read its colours ----------
_img_cache = collections.OrderedDict()
_img_lock = threading.Lock()


def get_image(url):
    with _img_lock:
        if url in _img_cache:
            _img_cache.move_to_end(url)
            return _img_cache[url]
    data, ctype, _ = fetch_url(url, max_bytes=8 * 1024 * 1024, accept="image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8")
    if not ctype.lower().startswith("image/"):
        sniff = data[:12]
        if sniff.startswith(b"\x89PNG"):
            ctype = "image/png"
        elif sniff[:3] == b"\xff\xd8\xff":
            ctype = "image/jpeg"
        elif sniff[:4] == b"RIFF" and sniff[8:12] == b"WEBP":
            ctype = "image/webp"
        elif sniff[:3] == b"GIF":
            ctype = "image/gif"
        else:
            raise FetchError(415, "That address isn't an image.")
    with _img_lock:
        _img_cache[url] = (data, ctype)
        while len(_img_cache) > 80:
            _img_cache.popitem(last=False)
    return data, ctype


# ---------- caption recognition (runs on this computer with faster-whisper) ----------
MODELS = ("tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en", "large-v3", "large-v3-turbo", "distil-large-v3")
MODEL_SIZES = {"tiny": "75 MB", "tiny.en": "75 MB", "base": "145 MB", "base.en": "145 MB", "small": "480 MB", "small.en": "480 MB",
               "medium": "1.5 GB", "medium.en": "1.5 GB", "large-v3": "3 GB", "large-v3-turbo": "1.6 GB", "distil-large-v3": "1.5 GB"}
# Pro-mode options: name -> (kind, default, limits). Anything else sent by the page is ignored.
PRO_SPEC = {
    "language": ("lang", ""), "task": ("choice", "transcribe", ("transcribe", "translate")),
    "device": ("choice", "auto", ("auto", "cpu", "cuda")), "compute": ("choice", "auto", ("auto", "int8", "int8_float16", "int16", "float16", "float32")),
    "threads": ("int", 0, 0, 64), "beam": ("int", 1, 1, 10), "temperature": ("float", 0.0, 0.0, 1.0),
    "condition": ("bool", False), "vad": ("bool", True), "vadThreshold": ("float", 0.5, 0.05, 0.95), "minSilence": ("int", 500, 50, 5000),
    "noSpeech": ("float", 0.6, 0.0, 1.0), "compression": ("float", 2.4, 1.0, 10.0), "logProb": ("float", -1.0, -10.0, 0.0),
    "words": ("bool", True), "maxChars": ("int", 84, 20, 300), "maxSeconds": ("float", 6.5, 1.0, 30.0),
    "prompt": ("text", "", 400), "hotwords": ("text", "", 400),
}
PRO_DEFAULTS = {k: v[1] for k, v in PRO_SPEC.items()}


def clean_opts(raw):
    """Validate pro-mode options from the page, falling back to defaults."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    out = dict(PRO_DEFAULTS)
    for key, spec in PRO_SPEC.items():
        if key not in data:
            continue
        v, kind = data[key], spec[0]
        try:
            if kind == "bool":
                out[key] = bool(v)
            elif kind == "int":
                out[key] = max(spec[2], min(spec[3], int(v)))
            elif kind == "float":
                out[key] = max(spec[2], min(spec[3], float(v)))
            elif kind == "choice":
                out[key] = v if v in spec[2] else spec[1]
            elif kind == "lang":
                out[key] = v if isinstance(v, str) and re.fullmatch(r"[a-z]{2,3}", v) else ""
            elif kind == "text":
                out[key] = str(v)[: spec[2]].strip()
        except (TypeError, ValueError):
            pass
    return out


def supported(fn, kwargs):
    """Keep only the keyword arguments this faster-whisper version understands."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return kwargs
    if any(p.kind == p.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}
INSTALL_HINT = "python3 afterhours.py --setup-captions"


class Cancelled(Exception):
    pass


def split_words(seg, offset, max_chars=84, max_secs=6.5):
    """Turn one recognised segment into short caption lines using its word timings."""
    words = list(getattr(seg, "words", None) or [])
    text = (getattr(seg, "text", "") or "").strip()
    if not words:
        if text:
            yield {"start": round(seg.start + offset, 2), "end": round(seg.end + offset, 2), "text": text}
        return
    cur, start, end = "", None, None
    for w in words:
        if start is None:
            start = w.start
        cur += w.word or ""
        end = w.end
        t = cur.strip()
        if len(t) >= max_chars or end - start >= max_secs or re.search(r"[.!?。！？…]$", t):
            if t:
                yield {"start": round(start + offset, 2), "end": round(end + offset, 2), "text": t}
            cur, start = "", None
    t = cur.strip()
    if t and start is not None:
        yield {"start": round(start + offset, 2), "end": round(end + offset, 2), "text": t}


def _frames(result):
    """PyAV 9+ resamplers return a list of frames; older ones return one frame or None."""
    if result is None:
        return []
    return [f for f in (result if isinstance(result, (list, tuple)) else [result]) if f is not None]


def decode_with_av(path, rate=16000):
    """Decode any audio file to 16 kHz mono floats with PyAV.
    Unlike faster-whisper's own decoder, this never passes options that newer PyAV removed
    (PyAV 14 dropped metadata_errors), and it skips damaged packets instead of giving up."""
    import av
    import numpy as np
    chunks, bad = [], 0
    state = {"resampler": None}

    def take(frame):
        frame.pts = None  # some PyAV versions refuse frames whose timestamps have gaps
        for _attempt in (1, 2):
            if state["resampler"] is None:
                state["resampler"] = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=rate)
            try:
                for out in _frames(state["resampler"].resample(frame)):
                    chunks.append(out.to_ndarray().reshape(-1))
                return
            except ValueError:
                state["resampler"] = None  # the audio format changed partway through: start a fresh resampler

    with av.open(str(path)) as container:
        streams = getattr(container.streams, "audio", None) or [s for s in container.streams if getattr(s, "type", "") == "audio"]
        if not streams:
            raise RuntimeError("the file has no audio track")
        for packet in container.demux(streams[0]):
            try:
                frames = packet.decode()
            except Exception:
                bad += 1
                if bad > 500:
                    break
                continue
            for frame in frames:
                try:
                    take(frame)
                except Exception:
                    bad += 1
        if state["resampler"] is not None:
            try:
                for out in _frames(state["resampler"].resample(None)):
                    chunks.append(out.to_ndarray().reshape(-1))
            except Exception:
                pass
    if not chunks:
        raise RuntimeError("no audio could be decoded")
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def decode_with_ffmpeg(path, rate=16000):
    import numpy as np
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg isn't installed")
    proc = subprocess.run([exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
                           "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    data = proc.stdout[: len(proc.stdout) // 2 * 2]
    if proc.returncode != 0 or not data:
        lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError((lines[-1] if lines else "ffmpeg couldn't read the file")[:200])
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def caption_versions():
    found = {}
    try:
        from importlib import metadata
        for name in ("faster-whisper", "av", "ctranslate2"):
            try:
                found[name] = metadata.version(name)
            except Exception:
                pass
    except Exception:
        pass
    found["ffmpeg"] = bool(shutil.which("ffmpeg"))
    return found


class WhisperEngine:
    name = "faster-whisper"

    def __init__(self):
        import faster_whisper
        self.fw = faster_whisper
        self.loaded = {}

    def model(self, size, job, opts):
        try:
            import ctranslate2
            cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            cuda = False
        device = opts["device"] if opts["device"] != "auto" else ("cuda" if cuda else "cpu")
        if device == "cuda" and not cuda:
            raise FetchError(422, "No compatible NVIDIA graphics card was found. In Pro mode, choose Automatic or Processor.")
        compute = opts["compute"] if opts["compute"] != "auto" else ("float16" if device == "cuda" else "int8")
        key = (size, device, compute, opts["threads"])
        if key not in self.loaded:
            job["message"] = "Loading the %s speech model. The first time, it downloads once (about %s)." % (size, MODEL_SIZES.get(size, ""))
            kwargs = {"device": device, "compute_type": compute}
            if opts["threads"]:
                kwargs["cpu_threads"] = opts["threads"]
            self.loaded[key] = self.fw.WhisperModel(size, **supported(self.fw.WhisperModel.__init__, kwargs))
            job["message"] = ""
        job["engineInfo"] = "%s · %s · %s" % (size, "graphics card" if device == "cuda" else "processor", compute)
        return self.loaded[key]

    def decode(self, path):
        problems = []
        for decoder in (decode_with_av, decode_with_ffmpeg):
            try:
                audio = decoder(path)
                if len(audio) >= 16000:
                    return audio
                problems.append(decoder.__name__[12:] + ": the audio was empty")
            except Exception as e:
                problems.append("%s: %s" % (decoder.__name__[12:], str(e)[:160] or e.__class__.__name__))
        try:  # last resort: faster-whisper's own decoder, which works when its PyAV version matches
            return self.fw.decode_audio(str(path), sampling_rate=16000)
        except Exception as e:
            problems.append("faster-whisper: %s" % (str(e)[:160] or e.__class__.__name__))
        raise FetchError(422, "Afterhours couldn’t read this episode’s audio (%s)." % "; ".join(problems))

    def segments(self, model, audio, a, b, lang, on_language, opts):
        chunk = audio[int(a * 16000):int(b * 16000)]
        if len(chunk) < 16000:
            return
        kwargs = {"language": opts["language"] or lang or None, "task": opts["task"], "beam_size": opts["beam"],
                  "temperature": opts["temperature"], "condition_on_previous_text": opts["condition"],
                  "vad_filter": opts["vad"], "word_timestamps": opts["words"],
                  "no_speech_threshold": opts["noSpeech"], "compression_ratio_threshold": opts["compression"],
                  "log_prob_threshold": opts["logProb"], "initial_prompt": opts["prompt"] or None, "hotwords": opts["hotwords"] or None}
        if opts["vad"]:
            kwargs["vad_parameters"] = {"threshold": opts["vadThreshold"], "min_silence_duration_ms": opts["minSilence"]}
        segs, info = model.transcribe(chunk, **supported(model.transcribe, kwargs))
        on_language(getattr(info, "language", "") or "")
        for seg in segs:
            for cue in split_words(seg, a, opts["maxChars"], opts["maxSeconds"]):
                yield cue


class MockAudio:
    def __init__(self, seconds):
        self.seconds = seconds

    def __len__(self):
        return int(self.seconds * 16000)


class MockEngine:
    """Stand-in engine for testing the caption pipeline without the speech model."""
    name = "mock"

    def model(self, size, job, opts):
        job["engineInfo"] = "%s · mock" % size
        return None

    def decode(self, path):
        try:
            import wave
            with wave.open(str(path)) as w:
                return MockAudio(w.getnframes() / float(w.getframerate()))
        except Exception:
            return MockAudio(120.0)

    def segments(self, model, audio, a, b, lang, on_language, opts):
        on_language(opts["language"] or lang or "en")
        t = a
        while t < b - 0.5:
            time.sleep(0.04)
            yield {"start": round(t, 2), "end": round(min(b, t + 3.6), 2),
                   "text": ("Translated speech at %d:%02d." if opts["task"] == "translate" else "Recognised speech at %d:%02d.") % (int(t) // 60, int(t) % 60)}
            t += 4.0


def caption_engine_name():
    if os.environ.get("AFTERHOURS_ASR_ENGINE") == "mock":
        return "mock"
    return "faster-whisper" if importlib.util.find_spec("faster_whisper") else ""


class CaptionJobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}
        self.queue = queue.Queue()
        self.engine = None
        self.worker = None

    @staticmethod
    def key(url, model, opts=None):
        base = url + "|" + model
        if opts and opts != PRO_DEFAULTS:   # captions made with the standard settings keep their old key
            base += "|" + json.dumps(opts, sort_keys=True)
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:24]

    def cache_path(self, key):
        return DATA_DIR / "captions" / (key + ".json")

    def start(self, url, model, start, lang, opts=None):
        opts = opts or dict(PRO_DEFAULTS)
        key = self.key(url, model, opts)
        with self.lock:
            job = self.jobs.get(key)
            if job and job["state"] not in ("error", "cancelled"):
                return self._status(job, 0)
            job = {"key": key, "url": url, "model": model, "start": start, "lang": lang, "opts": opts, "state": "queued",
                   "progress": 0.0, "message": "", "language": "", "segments": [], "error": "", "cancel": False, "engineInfo": ""}
            try:
                cached = json.loads(self.cache_path(key).read_text(encoding="utf-8"))
                job.update(state="done", progress=1.0, language=cached.get("language", ""), segments=cached.get("segments", []))
            except Exception:
                pass
            self.jobs[key] = job
            if job["state"] != "done":
                if not self.worker:
                    self.worker = threading.Thread(target=self._work, daemon=True)
                    self.worker.start()
                self.queue.put(key)
            return self._status(job, 0)

    def status(self, url, model, since, job_id=""):
        with self.lock:
            job = self.jobs.get(job_id) if job_id else self.jobs.get(self.key(url, model))
            return self._status(job, since) if job else {"state": "none"}

    def cancel(self, url, model, job_id=""):
        with self.lock:
            job = self.jobs.get(job_id) if job_id else self.jobs.get(self.key(url, model))
            if job and job["state"] not in ("done", "error"):
                job["cancel"] = True
                job["state"] = "cancelled"
        return {"state": "cancelled"}

    @staticmethod
    def _status(job, since):
        return {"id": job["key"], "options": job.get("opts", PRO_DEFAULTS), "engineInfo": job.get("engineInfo", ""),
                "state": job["state"], "progress": round(job["progress"], 4), "message": job["message"],
                "language": job["language"], "model": job["model"], "error": job["error"],
                "total": len(job["segments"]), "segments": job["segments"][max(0, since):]}

    def _work(self):
        while True:
            key = self.queue.get()
            job = self.jobs.get(key)
            if not job or job["cancel"]:
                continue
            try:
                self._run(job)
            except Cancelled:
                pass
            except FetchError as e:
                job.update(state="error", error=e.message)
            except Exception as e:
                text = "%s: %s" % (e.__class__.__name__, str(e)[:200])
                if re.search(r"(huggingface|hf_hub|LocalEntryNotFound|Max retries|ConnectionError|getaddrinfo|Name or service|Temporary failure)", text, re.I):
                    message = "Couldn’t download the speech model. Check your internet connection, then try again."
                else:
                    message = "Caption recognition stopped (%s). Updating the add-on often helps: %s" % (text, INSTALL_HINT)
                job.update(state="error", error=message)

    def _run(self, job):
        if self.engine is None:
            self.engine = MockEngine() if caption_engine_name() == "mock" else WhisperEngine()
        tmp_dir = DATA_DIR / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        ext = os.path.splitext(urllib.parse.urlsplit(job["url"]).path)[1][:6] or ".audio"
        path = tmp_dir / (job["key"] + ext)
        try:
            job.update(state="downloading", progress=0.0)
            with open_url(job["url"], accept="audio/*,*/*;q=0.8", timeout=30) as resp, open(path, "wb") as f:
                total, got = int(resp.headers.get("Content-Length") or 0), 0
                while True:
                    if job["cancel"]:
                        raise Cancelled()
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if got > MAX_AUDIO:
                        raise FetchError(413, "That episode is too large to caption.")
                    job["progress"] = got / total if total else 0.0
            job.update(state="loading", progress=0.0)
            model = self.engine.model(job["model"], job, job["opts"])
            audio = self.engine.decode(path)
            duration = len(audio) / 16000.0
            start = min(max(0.0, job["start"] - 2.0), duration)
            passes = [(start, duration), (0.0, start)] if start > 5 else [(0.0, duration)]
            job.update(state="transcribing", progress=0.0)
            done = 0.0

            def on_language(code):
                if code and not job["language"]:
                    job["language"] = code
            for a, b in passes:
                for cue in self.engine.segments(model, audio, a, b, job["lang"], on_language, job["opts"]):
                    if job["cancel"]:
                        raise Cancelled()
                    with self.lock:
                        job["segments"].append(cue)
                        job["progress"] = min(0.999, (done + max(0.0, cue["end"] - a)) / max(1.0, duration))
                done += b - a
            self.cache_path(job["key"]).parent.mkdir(parents=True, exist_ok=True)
            self.cache_path(job["key"]).write_text(json.dumps({"url": job["url"], "model": job["model"], "language": job["language"],
                                                              "segments": job["segments"], "created": time.time()}), encoding="utf-8")
            job.update(state="done", progress=1.0)
        finally:
            try:
                path.unlink()
            except Exception:
                pass


CAPTIONS = CaptionJobs()


def load_prefs():
    try:
        return json.loads((DATA_DIR / "prefs.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_prefs():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "prefs.json").write_text(json.dumps(PREFS), encoding="utf-8")
    except Exception:
        pass


PREFS = load_prefs()
ICON_PNG = base64.b64decode("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")
MANIFEST = json.dumps({"name": "Afterhours", "short_name": "Afterhours", "description": "A small, private podcast player.",
                       "start_url": "/", "scope": "/", "display": "standalone", "background_color": "#18181A", "theme_color": "#18181A",
                       "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]})
SERVICE_WORKER = ("const C='afterhours-4';self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(C).then(c=>c.add('/')))});"
                  "self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));"
                  "self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(e.request.method!=='GET'||u.origin!==location.origin||u.pathname!=='/')return;"
                  "e.respondWith(fetch(e.request).then(r=>{const k=r.clone();caches.open(C).then(c=>c.put('/',k));return r}).catch(()=>caches.match('/')))});")
LAN_DENIED = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Afterhours</title>"
              "<body style='font:17px/1.5 system-ui,sans-serif;padding:32px;max-width:32em;margin:auto'><h1>Afterhours</h1>"
              "<p>To use Afterhours on this phone, open the phone link from <b>Settings › Use on your phone</b> on your computer.</p>")


class PhoneAccess:
    """Lets phones on the same Wi-Fi use this Afterhours, with a private key."""

    def __init__(self):
        self.server, self.ip, self.port = None, "", 0
        self.lock = threading.Lock()

    def code(self, renew=False):
        if renew or not PREFS.get("phoneCode"):
            PREFS["phoneCode"] = secrets.token_hex(5)
            save_prefs()
        return PREFS["phoneCode"]

    @staticmethod
    def lan_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 80))   # no packets are sent; this only picks the outgoing interface
            return s.getsockname()[0]
        except OSError:
            return ""
        finally:
            s.close()

    def info(self):
        on = self.server is not None
        return {"on": on, "url": "http://%s:%d/?key=%s" % (self.ip, self.port, self.code()) if on else "", "address": "%s:%d" % (self.ip, self.port) if on else ""}

    def start(self, port, remember=True):
        with self.lock:
            if self.server is None:
                ip = self.lan_ip()
                if not ip or ip.startswith("127."):
                    raise FetchError(409, "This computer doesn’t seem to be on a network your phone can reach.")
                try:
                    srv = Server((ip, port), Handler)
                except OSError as e:
                    raise FetchError(409, "Couldn’t open Afterhours to your network (%s)." % e.strerror)
                srv.lan, srv.verbose = True, False
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                self.server, self.ip, self.port = srv, ip, port
            if remember:
                PREFS["phone"] = True
                save_prefs()
            return self.info()

    def stop(self, remember=True):
        with self.lock:
            if self.server is not None:
                srv, self.server = self.server, None
                threading.Thread(target=srv.shutdown, daemon=True).start()
            if remember:
                PREFS["phone"] = False
                save_prefs()
            return self.info()


PHONE = PhoneAccess()
MOVE = {"data": None, "at": 0.0}   # a library on its way from the browser tab to the app window
RUNNING = {"mode": "none"}   # how this copy of Afterhours is shown: "window", "browser" or "none"
RESTART = {}                 # set when Afterhours should start again in its own window
WINDOW = {}                  # the open window, if there is one


def venv_python():
    return str(DATA_DIR / "venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python"))


def can_import(python, code):
    try:
        return subprocess.call([python, "-c", code], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=60, **NO_CONSOLE) == 0
    except (OSError, subprocess.SubprocessError):
        return False


NO_CONSOLE = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW: no console flashes on Windows
_WINDOW_PY = {}


def window_python(refresh=False):
    """The Python that can open Afterhours' own window, or '' if the window component isn't installed."""
    if refresh or "py" not in _WINDOW_PY:
        found = ""
        if importlib.util.find_spec("webview"):
            found = sys.executable
        elif os.path.exists(venv_python()) and can_import(venv_python(), "import webview"):
            found = venv_python()
        _WINDOW_PY["py"] = found
    return _WINDOW_PY["py"]


def install_window_component(say=print):
    """Install pywebview: into this Python if it allows that, otherwise into Afterhours' private environment."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = open(DATA_DIR / "setup-window.log", "a", encoding="utf-8")

    def run(cmd):
        try:
            return subprocess.call(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=1800, **NO_CONSOLE)
        except (OSError, subprocess.SubprocessError):
            return 1

    custom = os.environ.get("AFTERHOURS_WINDOW_PACKAGE")
    package = custom or "pywebview"
    pip = ["-m", "pip", "install", "--upgrade", "--disable-pip-version-check", package]
    say("Installing the window component…")
    target = ""
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if run([sys.executable] + pip) == 0 or (not in_venv and run([sys.executable] + pip + ["--user"]) == 0):
        if can_import(sys.executable, "import webview"):
            target = sys.executable
    if not target:
        if not os.path.exists(venv_python()):
            say("Making a private Python environment for Afterhours…")
            # system packages stay visible, so an existing caption add-on keeps working
            if run([sys.executable, "-m", "venv", "--system-site-packages", str(DATA_DIR / "venv")]) != 0:
                raise FetchError(500, "Couldn’t make a private Python environment. Run: python3 afterhours.py --setup-window")
            say("Installing the window component…")
        if run([venv_python()] + pip) != 0 or not can_import(venv_python(), "import webview"):
            raise FetchError(500, "The window component couldn’t be installed. Check your internet connection, then try again.")
        target = venv_python()
    if sys.platform.startswith("linux") and not custom:
        # Linux: use the desktop's WebKitGTK when it's there; otherwise add Qt's web view
        gtk = "import gi\ntry:\n    gi.require_version('WebKit2', '4.1')\nexcept ValueError:\n    gi.require_version('WebKit2', '4.0')\nfrom gi.repository import WebKit2"
        if not can_import(target, gtk):
            say("Installing the window component…")
            if run([target, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "pywebview[qt]"]) != 0:
                raise FetchError(500, "The window component couldn’t be installed. Check your internet connection, then try again.")
    log.close()
    window_python(refresh=True)
    return target


class WindowSetup:
    """Installs the window component in the background, for Settings."""

    def __init__(self):
        self.state, self.message, self.python = "idle", "", ""

    def info(self):
        return {"state": self.state, "message": self.message}

    def start(self):
        if self.state != "working":
            self.state, self.message = "working", "Installing the window component…"
            threading.Thread(target=self._run, daemon=True).start()
        return self.info()

    def _run(self):
        try:
            self.python = install_window_component(lambda m: setattr(self, "message", m))
            self.state, self.message = "done", ""
        except FetchError as e:
            self.state, self.message = "error", e.message
        except Exception as e:  # never leave Settings waiting
            self.state, self.message = "error", "The window component couldn’t be installed (%s)." % e.__class__.__name__


WINDOW_SETUP = WindowSetup()


def png_to_ico(png):
    """An .ico that holds the PNG as it is (Windows Vista and later read these)."""
    import struct
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22) + png


def run_window(server, url):
    """Show Afterhours in its own window, using the system's web view. Returns when the window is closed."""
    import webview
    try:
        webview.settings.update({"ALLOW_DOWNLOADS": True, "OPEN_EXTERNAL_LINKS_IN_BROWSER": True})
    except Exception:
        pass
    store = DATA_DIR / "window-storage"
    store.mkdir(parents=True, exist_ok=True)
    icon = DATA_DIR / "icon.png"
    try:
        icon.write_bytes(ICON_PNG)
        (DATA_DIR / "icon.ico").write_bytes(png_to_ico(ICON_PNG))
    except OSError:
        pass
    if os.name == "nt":
        try:
            import ctypes  # its own taskbar button, not Python's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Afterhours")
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            from AppKit import NSApplication, NSImage  # installed with pywebview on a Mac
            app = NSApplication.sharedApplication()
            app.setActivationPolicy_(0)  # a regular app with a Dock icon
            image = NSImage.alloc().initWithContentsOfFile_(str(icon))
            if image is not None:
                app.setApplicationIconImage_(image)
        except Exception:
            pass
    threading.Thread(target=server.serve_forever, daemon=True).start()
    RUNNING["mode"] = "window"
    win = webview.create_window("Afterhours", url + "?window=1", **supported(webview.create_window, {
        "width": 1280, "height": 860, "min_size": (360, 560), "background_color": "#18181A", "text_select": True}))
    WINDOW["win"] = win

    def windows_icon():
        try:
            import clr  # noqa: F401  (pythonnet, installed with pywebview on Windows)
            from System import Action
            from System.Drawing import Icon
            form = getattr(win, "native", None)
            if form is not None:
                form.Invoke(Action(lambda: setattr(form, "Icon", Icon(str(DATA_DIR / "icon.ico")))))
        except Exception:
            pass

    if os.name == "nt":
        try:
            win.events.shown += windows_icon
        except Exception:
            pass
    # private_mode=False keeps the library and settings between launches
    webview.start(**supported(webview.start, {"private_mode": False, "storage_path": str(store), "icon": str(icon)}))
    WINDOW.pop("win", None)
    server.shutdown()


def focus_running(port):
    """Ask a copy that's already running to bring its window forward."""
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/focus" % port, data=b"", method="POST", headers={"X-Afterhours": "1"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("focused"))
    except Exception:
        return False


def setup_window_cli():
    try:
        python = install_window_component()
    except FetchError as e:
        print(e.message, file=sys.stderr)
        return 1
    print("\nDone. Afterhours now opens in its own window.%s" % ("" if python == sys.executable else
          "\nIt uses its private environment: %s" % python))
    return 0


def clear_saved_captions():
    removed = 0
    for f in (DATA_DIR / "captions").glob("*.json"):
        try:
            f.unlink()
            removed += 1
        except Exception:
            pass
    with CAPTIONS.lock:
        CAPTIONS.jobs = {k: j for k, j in CAPTIONS.jobs.items() if j["state"] != "done"}
    return removed


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Afterhours/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _host_ok(self):
        # Refuse requests that arrive under another host name (guards against DNS rebinding).
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        if getattr(self.server, "lan", False):
            return name == self.server.server_address[0]
        return name in ("127.0.0.1", "localhost", "[::1]")

    def _lan_gate(self):
        """Phones on the Wi-Fi need the private key: from the phone link once, then from a cookie."""
        if not getattr(self.server, "lan", False):
            return True
        code = PHONE.code()
        path, _, query = self.path.partition("?")
        key = (urllib.parse.parse_qs(query).get("key") or [""])[0]
        if key and secrets.compare_digest(key, code):
            self.send_response(303)
            self.send_header("Location", path or "/")
            self.send_header("Set-Cookie", "ah_key=%s; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax" % code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        m = re.search(r"(?:^|;\s*)ah_key=([0-9a-f]+)", self.headers.get("Cookie") or "")
        if m and secrets.compare_digest(m.group(1), code):
            return True
        self._send(401, LAN_DENIED, "text/html; charset=utf-8")
        return False

    def _send(self, code, body, ctype, extra=None, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, code, message):
        self._send(code, json.dumps({"error": message}), "application/json; charset=utf-8",
                   {"Content-Security-Policy": "default-src 'none'"})

    def _json(self, obj):
        self._send(200, json.dumps(obj), "application/json; charset=utf-8")

    def do_POST(self):
        # Anything that changes state is POST-only and needs a custom header. Other websites can't send
        # that header to this helper (the browser would ask first, and the helper never agrees).
        if not self._host_ok() or self.headers.get("X-Afterhours") != "1":
            return self._error(403, "Not allowed.")
        if not self._lan_gate():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(min(length, 8 << 20)) if length > 0 else b""
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        lan = getattr(self.server, "lan", False)
        if lan and path in ("/api/quit", "/api/prefs", "/api/phone", "/api/move", "/api/move/open", "/api/window/setup", "/api/window/switch", "/api/focus"):
            return self._error(403, "Only the computer running Afterhours can change that.")
        if path == "/api/move":
            if not body or length > 8 << 20:
                return self._error(413, "That’s too much to move at once.")
            try:
                data = json.loads(body.decode("utf-8"))
            except ValueError:
                data = None
            if not isinstance(data, dict):
                return self._error(400, "That isn’t a library.")
            MOVE["data"], MOVE["at"] = {k: v for k, v in data.items() if isinstance(k, str) and k.startswith("ah.") and isinstance(v, str)}, time.time()
            return self._json({"ok": True})
        if path == "/api/move/open":
            MOVE["data"] = None
            if not os.environ.get("AFTERHOURS_NO_OPEN"):
                webbrowser.open("http://127.0.0.1:%d/#/move" % self.server.server_address[1])
            return self._json({"ok": True})
        if path == "/api/window/setup":
            return self._json(WINDOW_SETUP.start())
        if path == "/api/window/switch":
            python = window_python(refresh=True)
            if not python:
                return self._error(409, "The window component isn’t installed yet.")
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except ValueError:
                data = {}
            if isinstance(data, dict) and data:   # the browser's library goes with it
                try:
                    (DATA_DIR / "move.json").write_text(json.dumps({"at": time.time(), "data": {
                        k: v for k, v in data.items() if isinstance(k, str) and k.startswith("ah.") and isinstance(v, str)}}), encoding="utf-8")
                except OSError:
                    pass
            PREFS["window"] = True
            save_prefs()
            RESTART["python"] = python
            threading.Timer(0.4, self.server.shutdown).start()
            return self._json({"ok": True})
        if path == "/api/focus":
            win = WINDOW.get("win")
            for name in ("restore", "show"):
                try:
                    getattr(win, name)()
                except Exception:
                    pass
            return self._json({"focused": win is not None})
        if path == "/api/prefs":
            if "window" in params:
                PREFS["window"] = params["window"][0] == "1"
                save_prefs()
            return self._json({"window": PREFS.get("window", True)})
        if path == "/api/phone":
            try:
                if "renew" in params:
                    PHONE.code(renew=True)
                    return self._json(PHONE.info())
                if params.get("on", [""])[0] == "1":
                    return self._json(PHONE.start(self.server.server_address[1]))
                return self._json(PHONE.stop())
            except FetchError as e:
                return self._error(e.code, e.message)
        if path == "/api/quit":
            self._json({"ok": True})
            win = WINDOW.get("win")
            if win is not None:
                threading.Timer(1.2, win.destroy).start()   # the window closes, and Afterhours stops after it
            else:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path == "/api/captions/clear":
            return self._json({"removed": clear_saved_captions()})
        return self._error(404, "Not found.")

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "Afterhours only answers on 127.0.0.1.")
        if not self._lan_gate():
            return
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        arg = lambda name, default="": (params.get(name) or [default])[0].strip()
        try:
            if path in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP})
            if path == "/api/ping":
                return self._json({"app": APP_ID, "name": APP_NAME, "version": VERSION, "platform": sys.platform,
                                   "client": "lan" if getattr(self.server, "lan", False) else "local"})
            if path == "/manifest.webmanifest":
                return self._send(200, MANIFEST, "application/manifest+json", cache="no-cache")
            if path == "/icon.png":
                return self._send(200, ICON_PNG, "image/png", cache="public, max-age=86400")
            if path == "/sw.js":
                return self._send(200, SERVICE_WORKER, "text/javascript; charset=utf-8", cache="no-cache")
            if path == "/api/prefs":
                return self._json({"window": PREFS.get("window", True), "native": bool(window_python()),
                                   "running": RUNNING["mode"], "setup": WINDOW_SETUP.info()})
            if path == "/api/phone":
                return self._json(PHONE.info())
            if path == "/api/move":
                if getattr(self.server, "lan", False):
                    return self._error(403, "Only the computer running Afterhours can change that.")
                fresh = MOVE["data"] is not None and time.time() - MOVE["at"] < 600
                data, MOVE["data"] = (MOVE["data"] if fresh else None), None if fresh else MOVE["data"]
                return self._json({"data": data})
            if path == "/api/fetch":
                data, ctype, final_url = fetch_url(arg("url"))
                return self._send(200, data, ctype, {
                    "X-Final-URL": urllib.parse.quote(final_url, safe=":/?#[]@!$&'()*+,;=%~"),
                    "Content-Security-Policy": "default-src 'none'; sandbox"})  # never runs as a page here
            if path == "/api/feedinfo":
                return self._json(feed_info(arg("url")))
            if path == "/api/crawl":
                return self._json(crawl(arg("url"), arg("title")))
            if path == "/api/image":
                data, ctype = get_image(arg("url"))
                return self._send(200, data, ctype, {"Content-Security-Policy": "default-src 'none'; sandbox"},
                                  cache="private, max-age=604800")
            if path in ("/api/search", "/api/lookup"):
                country = arg("country", "gb").lower()
                if not re.fullmatch(r"[a-z]{2}", country):
                    country = "gb"
                if path == "/api/search":
                    term = arg("term")[:120]
                    if not term:
                        return self._error(400, "Type something to search for.")
                    try:
                        limit = max(1, min(50, int(arg("limit", "20"))))
                    except ValueError:
                        limit = 20
                    data = apple_get("/search", {"media": "podcast", "entity": "podcast", "term": term, "country": country, "limit": limit})
                else:
                    ids = re.findall(r"\d{3,12}", arg("id"))[:50]
                    if not ids:
                        return self._error(400, "No podcast IDs given.")
                    data = apple_get("/lookup", {"id": ",".join(ids), "country": country})
                return self._send(200, data, "application/json; charset=utf-8")
            if path.startswith("/api/captions"):
                engine = caption_engine_name()
                if path == "/api/captions":
                    return self._json({"available": bool(engine), "engine": engine, "models": list(MODELS),
                                       "sizes": MODEL_SIZES, "install": INSTALL_HINT, "versions": caption_versions()})

                if not engine:
                    return self._error(501, "Caption recognition isn’t installed. Run: " + INSTALL_HINT)
                url, model, job_id = arg("url"), arg("model", "base"), arg("id")
                if model not in MODELS:
                    model = "base"
                if not job_id and not url.startswith(("http://", "https://")):
                    return self._error(400, "That episode has no audio address.")
                if path == "/api/captions/start":
                    lang = arg("lang").lower()
                    try:
                        start = max(0.0, float(arg("start", "0")))
                    except ValueError:
                        start = 0.0
                    return self._json(CAPTIONS.start(url, model, start, lang if re.fullmatch(r"[a-z]{2}", lang) else "", clean_opts(arg("opts")[:4000])))
                if path == "/api/captions/status":
                    try:
                        since = max(0, int(arg("since", "0")))
                    except ValueError:
                        since = 0
                    return self._json(CAPTIONS.status(url, model, since, job_id))
                if path == "/api/captions/cancel":
                    return self._json(CAPTIONS.cancel(url, model, job_id))
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            return self._error(404, "Not found.")
        except FetchError as e:
            return self._error(e.code, e.message)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # On Windows, address reuse would let two copies share a port, so turn it off there.
    allow_reuse_address = os.name != "nt"


def already_running(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == APP_ID
    except Exception:
        return False


def setup_captions():
    print("Installing the caption recognition add-on (faster-whisper). This takes a minute or two…")
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "faster-whisper"]
    if subprocess.call(cmd) != 0 and subprocess.call(cmd + ["--user"]) != 0:
        venv = DATA_DIR / "venv"
        print("\nThis Python doesn’t allow installing add-ons directly. Use a small private environment instead:\n"
              "  python3 -m venv \"%s\"\n  \"%s\" -m pip install faster-whisper\n  \"%s\" afterhours.py\n"
              % (venv, venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"), venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
        return 1
    print("\nDone. Start Afterhours again, and episodes without captions will be captioned on this computer.\n"
          "The speech model downloads once, the first time it’s needed.")
    return 0


def main():
    if sys.stdout is None or sys.stderr is None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            log = open(DATA_DIR / "afterhours.log", "a", encoding="utf-8", buffering=1)
            sys.stdout, sys.stderr = sys.stdout or log, sys.stderr or log
        except OSError:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Afterhours: a small, private podcast player.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to use (default %d)" % DEFAULT_PORT)
    parser.add_argument("--window", action="store_true", help="open in its own window (the default)")
    parser.add_argument("--browser", action="store_true", help="open in a normal browser tab instead")
    parser.add_argument("--no-browser", action="store_true", help="don't open anything")
    parser.add_argument("--setup-captions", action="store_true", help="install the caption recognition add-on, then exit")
    parser.add_argument("--setup-window", action="store_true", help="install the window component (pywebview), then exit")
    parser.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args()
    if args.setup_captions:
        sys.exit(setup_captions())
    if args.setup_window:
        sys.exit(setup_window_cli())
    url = "http://127.0.0.1:%d/" % args.port
    mode = "none" if args.no_browser else "browser" if args.browser else "window" if (args.window or PREFS.get("window", True)) else "browser"
    if mode == "window" and not args.restarted and importlib.util.find_spec("webview") is None             and not already_running(args.port) and window_python():
        # the window component lives in Afterhours' private environment: carry on there
        argv = [window_python(), os.path.abspath(sys.argv[0])] + sys.argv[1:]
        if os.name == "nt":
            subprocess.Popen(argv, close_fds=True, creationflags=0x00000008 | 0x00000200)
            return
        os.execv(argv[0], argv)
    server = None
    for attempt in range(40 if args.restarted else 1):   # after a restart, wait for the old copy to let go of the port
        try:
            server = Server(("127.0.0.1", args.port), Handler)
            break
        except OSError:
            if args.restarted:
                time.sleep(0.25)
    if server is None:
        if already_running(args.port):
            print("Afterhours is already running at %s" % url)
            if mode == "window" and focus_running(args.port):
                return
            if mode != "none":
                webbrowser.open(url)
            return
        print("Port %d is in use by another program. Try:  python3 afterhours.py --port %d\n"
              "Each port keeps its own library, so pick one and stick with it." % (args.port, args.port + 1), file=sys.stderr)
        sys.exit(1)
    server.verbose, server.lan = args.verbose, False
    try:   # a library on its way from the browser, saved by "Switch now"
        moving = json.loads((DATA_DIR / "move.json").read_text(encoding="utf-8"))
        (DATA_DIR / "move.json").unlink()
        if time.time() - float(moving.get("at", 0)) < 600 and isinstance(moving.get("data"), dict):
            MOVE["data"], MOVE["at"] = moving["data"], time.time()
    except (OSError, ValueError, AttributeError):
        pass
    if PREFS.get("phone"):
        try:
            PHONE.start(args.port, remember=False)
        except FetchError:
            pass
    engine = caption_engine_name()
    native = mode == "window" and importlib.util.find_spec("webview") is not None
    print("Afterhours %s is running at %s\n%s%s%s\nTo stop, close the Afterhours window, press Ctrl+C, or use Settings > Quit Afterhours."
          % (VERSION, url, "Caption recognition: on (%s)." % engine if engine else
             "Caption recognition: off. To caption episodes that have none, run:  " + INSTALL_HINT,
             "" if native or mode != "window" else "\nTo open Afterhours in its own window instead of your browser, use Settings > Window, or run:  python3 afterhours.py --setup-window",
             "\nPhones on your Wi-Fi can use: " + PHONE.info()["url"] if PHONE.server else ""))
    try:
        if native:
            run_window(server, url)
        else:
            RUNNING["mode"] = "browser" if mode == "window" else mode
            if RUNNING["mode"] == "browser":
                threading.Timer(0.6, lambda: webbrowser.open(url)).start()
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        PHONE.stop(remember=False)
        print("\nStopped. Good night." if not RESTART else "\nOpening Afterhours in its own window…")
    if RESTART:   # "Switch now": start again in the window, with the same Python that has the window component
        python = RESTART["python"]
        argv = [python, os.path.abspath(sys.argv[0]), "--port", str(args.port), "--window", "--restarted"]
        if os.name == "nt":
            quiet = os.path.join(os.path.dirname(python), "pythonw.exe")
            if os.path.exists(quiet):
                argv[0] = quiet
            subprocess.Popen(argv, close_fds=True, creationflags=0x00000008 | 0x00000200)   # detached, its own process group
            os._exit(0)
        sys.stdout.flush()
        os.execv(python, argv)


PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="referrer" content="no-referrer">
<title>Afterhours</title>
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#18181A">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Afterhours">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="apple-touch-icon" href="/icon.png">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23FFC15E'/%3E%3Cstop offset='.6' stop-color='%23F26B7A'/%3E%3Cstop offset='1' stop-color='%23B2418F'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='64' height='64' rx='14' fill='%2321184F'/%3E%3Ccircle cx='32' cy='32' r='20' fill='url(%23g)'/%3E%3C/svg%3E">
<style>
:root {
  --bg: #FFFFFF; --bg-2: #F5F5F7; --side: #F2F2F5; --bar: rgba(250,250,252,.82);
  --fill: rgba(118,118,128,.1); --fill-2: rgba(118,118,128,.18);
  --text: #1D1D1F; --text-2: #6E6E73; --text-3: #8A8A8F; --sep: rgba(60,60,67,.12);
  --accent: #C8326C; --accent-ink: #FFFFFF; --accent-soft: rgba(200,50,108,.1); --on: #34C759; --reveal: rgba(200,50,108,.075);
  --shadow-art: 0 10px 24px -12px rgba(28,18,58,.45), 0 0 0 .5px rgba(0,0,0,.05);
  --shadow-float: 0 14px 44px -12px rgba(0,0,0,.26), 0 0 0 .5px rgba(0,0,0,.08);
  --pop-bg: #FFFFFF;
  --font: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable Text", "Segoe UI", system-ui, Roboto, "Helvetica Neue", Arial, "Hiragino Sans", "Hiragino Kaku Gothic ProN", "PingFang SC", "Noto Sans JP", "Noto Sans CJK JP", "Microsoft YaHei", sans-serif;
  --font-display: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI Variable Display", "Segoe UI", system-ui, Roboto, "Helvetica Neue", Arial, "Hiragino Sans", "PingFang SC", "Noto Sans JP", "Noto Sans CJK JP", sans-serif;
  --side-w: 248px; --bar-h: 72px; --gutter: clamp(20px, 4vw, 48px);
  --th: 335; --ts: 62%; --tl: 52%;
  --ease-out: cubic-bezier(0.1, 0.9, 0.2, 1); --ease-mid: cubic-bezier(0, 0, 0, 1); --ease-in: cubic-bezier(0.9, 0.1, 1, 0.2); --ease-std: cubic-bezier(0.33, 0, 0.67, 1);
  color-scheme: light;
  box-sizing: border-box;
  padding-top: env(safe-area-inset-top, 0px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #18181A; --bg-2: #222225; --side: #1F1F22; --bar: rgba(44,44,48,.8);
    --fill: rgba(118,118,128,.2); --fill-2: rgba(118,118,128,.3);
    --text: #F5F5F7; --text-2: #A1A1A6; --text-3: #7C7C82; --sep: rgba(255,255,255,.09);
    --accent: #FF6E9F; --accent-ink: #2B0715; --accent-soft: rgba(255,110,159,.15); --on: #30D158; --reveal: rgba(255,255,255,.075);
    --shadow-art: 0 12px 28px -12px rgba(0,0,0,.75), 0 0 0 .5px rgba(255,255,255,.07);
    --shadow-float: 0 16px 48px -10px rgba(0,0,0,.65), 0 0 0 .5px rgba(255,255,255,.1);
    --pop-bg: #2C2C30;
    color-scheme: dark;
  }
  :root:not([data-theme="light"]) .show-author { color: hsl(var(--th) 82% 74%); }
}
:root[data-theme="dark"] {
  --bg: #18181A; --bg-2: #222225; --side: #1F1F22; --bar: rgba(44,44,48,.8);
  --fill: rgba(118,118,128,.2); --fill-2: rgba(118,118,128,.3);
  --text: #F5F5F7; --text-2: #A1A1A6; --text-3: #7C7C82; --sep: rgba(255,255,255,.09);
  --accent: #FF6E9F; --accent-ink: #2B0715; --accent-soft: rgba(255,110,159,.15); --on: #30D158; --reveal: rgba(255,255,255,.075);
  --shadow-art: 0 12px 28px -12px rgba(0,0,0,.75), 0 0 0 .5px rgba(255,255,255,.07);
  --shadow-float: 0 16px 48px -10px rgba(0,0,0,.65), 0 0 0 .5px rgba(255,255,255,.1);
  --pop-bg: #2C2C30;
  color-scheme: dark;
}
:root[data-theme="dark"] .show-author { color: hsl(var(--th) 82% 74%); }

*, *::before, *::after { box-sizing: inherit; }
html { height: 100%; background: var(--bg); }
body { height: 100%; margin: 0; overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--font); font-size: 15px; line-height: 1.45; -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; }
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 90; background: var(--bg); }
button, input { font: inherit; color: inherit; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: .4; }
a { color: inherit; }
:focus-visible { outline: 2.5px solid color-mix(in srgb, var(--accent) 75%, transparent); outline-offset: 2px; border-radius: 8px; }
[hidden] { display: none !important; }
svg { width: 20px; height: 20px; flex: none; }
.sprite { position: absolute; width: 0; height: 0; overflow: hidden; }
input[type=search]::-webkit-search-decoration, input[type=search]::-webkit-search-cancel-button { -webkit-appearance: none; }

/* Shell */
.app { display: grid; grid-template-columns: var(--side-w) minmax(0, 1fr); height: 100%; }
.main { position: relative; height: 100%; min-height: 0; overflow-y: auto; overflow-x: hidden; outline: none; }
.view { container: view / inline-size; max-width: 1240px; margin: 0 auto; padding: 36px var(--gutter) 150px; animation: pageIn 460ms var(--ease-out) both; }
.view.drill { animation-name: drillIn; animation-duration: 380ms; transform-origin: 50% 0; }
@keyframes pageIn { from { opacity: 0; transform: translateY(22px); } to { opacity: 1; transform: none; } }
@keyframes drillIn { from { opacity: 0; transform: scale(.965); } to { opacity: 1; transform: none; } }

/* Sidebar */
.sidebar { position: relative; z-index: 5; display: flex; flex-direction: column; gap: 4px; min-height: 0; padding: 18px 12px 12px; background: var(--side); border-right: 1px solid var(--sep); overflow: hidden; }
.brand { display: flex; align-items: center; gap: 10px; padding: 2px 10px 14px; text-decoration: none; }
.brand-sun { flex: none; width: 24px; height: 24px; border-radius: 50%; background: linear-gradient(#FFC15E, #F26B7A 60%, #B2418F); box-shadow: 0 3px 10px -2px rgba(242,107,122,.55);
  -webkit-mask: linear-gradient(#000 0 56%, transparent 56% 61%, #000 61% 69%, transparent 69% 75%, #000 75% 83%, transparent 83% 90%, #000 90%);
          mask: linear-gradient(#000 0 56%, transparent 56% 61%, #000 61% 69%, transparent 69% 75%, #000 75% 83%, transparent 83% 90%, #000 90%); }
.brand-name { font-family: var(--font-display); font-weight: 700; font-size: 18px; letter-spacing: -.015em; white-space: nowrap; }
.side-search { display: flex; align-items: center; gap: 7px; height: 32px; margin: 0 2px 10px; padding: 0 10px; border-radius: 9px; background: var(--fill); }
.side-search svg { width: 15px; height: 15px; color: var(--text-3); }
.side-search input { flex: 1; min-width: 0; border: 0; background: none; outline: none; font-size: 13.5px; }
.side-search input::placeholder { color: var(--text-3); }
.side-search:focus-within { background: var(--bg); box-shadow: 0 0 0 3px var(--accent-soft), inset 0 0 0 1px color-mix(in srgb, var(--accent) 60%, transparent); }
.nav { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 1px; }
.nav a { display: flex; align-items: center; gap: 10px; height: 34px; padding: 0 10px; border-radius: 8px; text-decoration: none; font-size: 14px; font-weight: 500; }
.nav a svg { width: 19px; height: 19px; color: var(--accent); }
.nav a:hover { background: var(--fill); }
.nav a[aria-current="page"] { background: var(--fill-2); }
.rail-only { display: none !important; }
.nav-head { display: flex; justify-content: space-between; align-items: baseline; margin: 18px 10px 4px; font-size: 11.5px; font-weight: 600; color: var(--text-3); }
.side-progress { font-weight: 500; color: var(--accent); }
.side-shows { flex: 1; min-height: 0; list-style: none; margin: 0; padding: 0 0 8px; overflow-y: auto; scrollbar-width: thin; }
.side-shows a { display: flex; align-items: center; gap: 9px; height: 34px; padding: 0 8px; border-radius: 8px; text-decoration: none; font-size: 13.5px; }
.side-shows a:hover { background: var(--fill); }
.side-shows a[aria-current="page"] { background: var(--fill-2); }
.side-shows a.dim { color: var(--text-3); }
.side-shows .t { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.side-shows .eq { color: var(--accent); }
.side-art { width: 24px !important; border-radius: 5px !important; }
.side-art img { transition: none !important; opacity: 1 !important; }
.side-foot { margin-top: auto; padding-top: 8px; border-top: 1px solid var(--sep); display: flex; flex-direction: column; gap: 1px; }
.side-tool { display: flex; align-items: center; gap: 10px; height: 30px; padding: 0 10px; border: 0; border-radius: 8px; background: none; color: var(--text-2); font-size: 13px; text-align: left; }
.side-tool:hover { background: var(--fill); color: var(--text); }
.side-tool svg { width: 16px; height: 16px; }

/* Artwork */
.art { position: relative; display: block; flex: none; width: 100%; aspect-ratio: 1 / 1; overflow: hidden; border-radius: 12px; background: var(--fill); container-type: inline-size; }
.art .mono { position: absolute; inset: 0; display: grid; place-items: center; color: rgba(255,255,255,.95); font-weight: 700; font-size: 38cqw; line-height: 1; background: linear-gradient(145deg, hsl(var(--mh, 330) 62% 62%), hsl(calc(var(--mh, 330) + 38) 58% 40%)); }
.art img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity .35s; }
.art img.ok { opacity: 1; }
.shimmer::after { content: ""; position: absolute; inset: 0; background: linear-gradient(100deg, transparent 30%, rgba(255,255,255,.32) 50%, transparent 70%); background-size: 220% 100%; animation: shimmer 1.4s linear infinite; }
@keyframes shimmer { from { background-position: 120% 0; } to { background-position: -120% 0; } }

/* Page furniture */
.page-head { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px; margin: 0 0 22px; }
.page-title { margin: 0; font-family: var(--font-display); font-size: 34px; line-height: 1.1; font-weight: 700; letter-spacing: -.022em; }
.page-sub { margin: 6px 0 0; color: var(--text-2); font-size: 15px; }
.section { margin-top: 30px; }
.section-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin: 0 0 14px; padding-top: 18px; border-top: 1px solid var(--sep); }
.section-title { margin: 0; font-size: 21px; line-height: 1.2; font-weight: 700; letter-spacing: -.016em; }
.section-sub { margin: 3px 0 0; color: var(--text-2); font-size: 13.5px; }
.section-tools { display: flex; align-items: center; gap: 6px; flex: none; }
.see-all { margin-right: 6px; color: var(--accent); font-size: 14px; font-weight: 500; text-decoration: none; }
.see-all:hover { text-decoration: underline; }
.row-nav { width: 30px; height: 30px; border: 0; border-radius: 50%; display: grid; place-items: center; background: var(--fill); color: var(--text-2); }
.row-nav:hover { background: var(--fill-2); color: var(--text); }
.row-nav svg { width: 15px; height: 15px; }
.row { display: grid; grid-auto-flow: column; grid-auto-columns: var(--card-w, 168px); gap: 18px; overflow-x: auto; overscroll-behavior-x: contain; scroll-snap-type: x mandatory; padding: 4px 2px 12px; margin: 0 -2px; scrollbar-width: none; }
.row::-webkit-scrollbar { display: none; }
.row.wide { --card-w: 336px; }
.row:has(.row-empty) { display: block; }
.row-empty { margin: 4px 0 8px; color: var(--text-2); font-size: 14px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 26px 20px; }
.grid:has(.row-empty) { display: block; }
.chips { display: flex; gap: 8px; overflow-x: auto; scrollbar-width: none; padding: 2px; margin: 0 -2px 4px; }
.chips::-webkit-scrollbar { display: none; }
.chips.wrap { flex-wrap: wrap; }
.chip { flex: none; display: inline-flex; align-items: center; height: 32px; padding: 0 15px; border-radius: 999px; background: var(--fill); color: var(--text); font-size: 13.5px; font-weight: 600; text-decoration: none; white-space: nowrap; transition: background .15s; }
.chip:hover { background: var(--fill-2); }
.chip[aria-selected="true"] { background: var(--text); color: var(--bg); }
.btn { display: inline-flex; align-items: center; gap: 7px; height: 38px; padding: 0 18px; border: 0; border-radius: 999px; font-size: 14.5px; font-weight: 600; text-decoration: none; white-space: nowrap; transition: transform .12s, filter .15s, background .15s; }
.btn:active { transform: scale(.97); }
.btn svg { width: 17px; height: 17px; }
.btn.primary { background: var(--accent); color: var(--accent-ink); }
.btn.primary:hover { filter: brightness(1.07); }
.btn.tonal { background: var(--fill-2); color: var(--text); }
.btn.tonal:hover { background: color-mix(in srgb, var(--fill-2), var(--text) 7%); }
.btn.small { height: 32px; padding: 0 13px; font-size: 13px; }
.icon-round { width: 38px; height: 38px; border: 0; border-radius: 50%; display: grid; place-items: center; background: var(--fill-2); color: var(--text); text-decoration: none; }
.icon-round:hover { background: color-mix(in srgb, var(--fill-2), var(--text) 7%); }
.icon-round svg { width: 18px; height: 18px; }
.spin svg { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.link-btn { padding: 0; border: 0; background: none; color: var(--accent); font-size: inherit; font-weight: 500; text-decoration: none; }
.link-btn:hover { text-decoration: underline; }
.list-note { margin: 18px 0; color: var(--text-2); }
.banner { display: flex; align-items: center; gap: 18px; margin: 0 0 6px; padding: 16px 18px; border-radius: 16px; background: var(--bg-2); }
.banner-text { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; font-size: 14px; color: var(--text-2); }
.banner-text strong { font-size: 15px; color: var(--text); }
.meter { flex: none; width: 160px; height: 6px; border-radius: 3px; background: var(--fill-2); overflow: hidden; }
.meter i { display: block; height: 100%; border-radius: 3px; background: var(--accent); transition: width .4s; }
.feed-status { margin: -4px 0 0; font-size: 13px; color: var(--text-3); min-height: 1.2em; }
.empty { max-width: 30rem; margin: 10vh auto; text-align: center; }
.empty h2 { margin: 0 0 8px; font-size: 22px; letter-spacing: -.015em; }
.empty p { margin: 0 0 18px; color: var(--text-2); }

/* Cards */
.card { position: relative; min-width: 0; container-type: inline-size; scroll-snap-align: start; }
.card-body, .tile-link { display: flex; flex-direction: column; width: 100%; padding: 0; border: 0; background: none; text-align: left; text-decoration: none; color: inherit; }
.card-body .art, .tile-link .art { box-shadow: var(--shadow-art); transition: transform 320ms var(--ease-out), box-shadow 320ms var(--ease-out); }
.card-body:hover .art, .tile-link:hover .art { transform: translateY(-4px); box-shadow: 0 22px 36px -18px rgba(28,18,58,.55), 0 0 0 .5px rgba(0,0,0,.05); }
.card-body:active .art, .tile-link:active .art { transform: scale(.97); transition-duration: 120ms; }
.card-title { margin-top: 10px; font-size: 14px; font-weight: 600; line-height: 1.28; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.card-sub { margin-top: 2px; font-size: 13px; color: var(--text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-why { margin-top: 4px; font-size: 12.5px; line-height: 1.38; color: var(--text-3); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.add-btn { position: absolute; top: calc(100cqw - 44px); right: 8px; width: 34px; height: 34px; border: 0; border-radius: 50%; display: grid; place-items: center; background: rgba(255,255,255,.9); color: #1D1D1F; box-shadow: 0 3px 12px rgba(0,0,0,.28); -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px); transition: transform .15s, background .15s; }
.add-btn:hover { transform: scale(1.08); }
.add-btn svg { width: 17px; height: 17px; }
.add-btn.added { background: var(--accent); color: var(--accent-ink); }
.add-btn.busy { animation: pulse 1s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: .45; } }
.card.unavailable .art { opacity: .45; filter: grayscale(.5); }
.card.unavailable .add-btn { display: none; }
.skeleton .sk { display: block; height: 11px; margin-top: 11px; border-radius: 6px; background: var(--fill); width: 82%; }
.skeleton .sk.short { width: 52%; margin-top: 7px; }
.sk-row { height: 92px; margin: 8px 0; border-radius: 14px; background: var(--fill); opacity: .7; }
.tile { position: relative; min-width: 0; }
.tile.missing .art { opacity: .45; filter: grayscale(.6); }
.tile-eq { position: absolute; top: 8px; right: 8px; display: grid; place-items: center; width: 28px; height: 28px; border-radius: 50%; background: rgba(0,0,0,.55); color: #fff; }

/* Discover hero */
.hero { position: relative; display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(240px, .75fr); align-items: center; gap: 24px; min-height: 236px; margin: 4px 0 6px; padding: 32px 36px; border-radius: 22px; overflow: hidden; color: #fff;
  background: radial-gradient(120% 140% at 0% 0%, #3B2A86 0%, transparent 55%), radial-gradient(110% 120% at 100% 100%, #F26B7A 0%, transparent 58%), linear-gradient(135deg, #21184F, #7E3A8E 58%, #F08F6A);
  box-shadow: 0 22px 50px -26px rgba(60,20,90,.7); }
.hero-kicker { margin: 0; font-size: 13px; font-weight: 600; color: rgba(255,255,255,.75); }
.hero-title { margin: 6px 0 0; max-width: 20ch; font-family: var(--font-display); font-size: clamp(24px, 2.8vw, 34px); font-weight: 700; line-height: 1.12; letter-spacing: -.02em; }
.hero-sub { margin: 10px 0 0; max-width: 46ch; font-size: 15px; color: rgba(255,255,255,.84); }
.hero-stack { position: relative; height: 190px; }
.hero-stack .art { position: absolute; top: 22px; width: 148px; border-radius: 14px; box-shadow: 0 18px 40px -14px rgba(0,0,0,.6); }
.hero-stack .s0 { right: 148px; transform: rotate(-8deg) translateY(10px); }
.hero-stack .s1 { right: 74px; top: 4px; z-index: 2; }
.hero-stack .s2 { right: 0; transform: rotate(8deg) translateY(10px); }

/* Show page */
.show-hero { position: relative; isolation: isolate; display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: end; gap: clamp(20px, 3vw, 36px); margin: -36px calc(-1 * var(--gutter)) 0; padding: 44px var(--gutter) 30px; }
.show-hero::before { content: ""; position: absolute; inset: 0; z-index: -1; background: linear-gradient(180deg, hsl(var(--th) var(--ts) var(--tl) / .34), hsl(var(--th) var(--ts) var(--tl) / .1) 60%, transparent); transition: background .5s; }
.hero-art { width: clamp(150px, 20vw, 236px); border-radius: 14px; box-shadow: 0 26px 50px -22px hsl(var(--th) 50% 18% / .75), 0 0 0 .5px rgba(0,0,0,.06); }
.show-kicker { margin: 0; font-size: 13px; font-weight: 600; color: var(--text-2); }
.show-title { margin: 6px 0 0; font-family: var(--font-display); font-size: clamp(28px, 3.4vw, 42px); font-weight: 700; line-height: 1.08; letter-spacing: -.025em; }
.show-author { margin: 6px 0 0; font-size: 17px; font-weight: 600; color: hsl(var(--th) 52% 40%); }
.show-about { margin: 12px 0 0; max-width: 72ch; color: var(--text-2); font-size: 14.5px; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.show-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 18px; }
.eps-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 26px 0 2px; }

/* Episodes */
.eps { list-style: none; margin: 4px -12px 0; padding: 0; }
.ep { position: relative; display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px; padding: 16px 12px; border-radius: 14px; cursor: pointer; }
.ep.rich { grid-template-columns: 72px minmax(0, 1fr); align-items: start; }
.ep::after { content: ""; position: absolute; left: 12px; right: 12px; bottom: 0; border-bottom: 1px solid var(--sep); }
.ep:last-child::after, .ep:hover::after { opacity: 0; }
.ep:hover { background: var(--fill); }
.ep-art { width: 72px; border-radius: 10px; box-shadow: 0 6px 14px -8px rgba(0,0,0,.45); }
.ep-date { display: flex; flex-wrap: wrap; gap: 3px 10px; margin: 0; font-size: 12px; font-weight: 600; color: var(--text-3); letter-spacing: .01em; }
.ep-show { color: var(--accent); text-decoration: none; }
.ep-show:hover { text-decoration: underline; }
.ep-title { margin: 4px 0 0; font-size: 16.5px; font-weight: 600; line-height: 1.3; letter-spacing: -.01em; }
.ep-desc { margin: 6px 0 0; max-width: 78ch; color: var(--text-2); font-size: 14px; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.ep.open .ep-desc { display: block; white-space: pre-line; }
.ep-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 12px; }
.pill { display: inline-flex; align-items: center; gap: 7px; height: 30px; padding: 0 14px 0 11px; border: 0; border-radius: 999px; background: var(--accent-soft); color: var(--accent); font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; transition: transform .12s; }
.pill:active { transform: scale(.96); }
.pill svg { width: 13px; height: 13px; }
.pill.current { background: var(--accent); color: var(--accent-ink); }
.pill.done { background: var(--fill); color: var(--text-2); }
.pill-prog { display: inline-block; width: 28px; height: 4px; border-radius: 2px; background: color-mix(in srgb, currentColor 28%, transparent); overflow: hidden; }
.pill-prog i { display: block; height: 100%; background: currentColor; }
.badge { padding: 1px 6px; border: 1.5px solid var(--text-3); border-radius: 5px; color: var(--text-3); font-size: 10.5px; font-weight: 700; letter-spacing: .02em; }
.more-row { padding: 18px 0; text-align: center; list-style: none; }
.eq { display: inline-flex; align-items: flex-end; gap: 2px; width: 13px; height: 12px; }
.eq i { width: 3px; height: 40%; border-radius: 1px; background: currentColor; animation: eq 1s ease-in-out infinite; }
.eq i:nth-child(2) { animation-delay: -.45s; }
.eq i:nth-child(3) { animation-delay: -.75s; }
@keyframes eq { 0%, 100% { height: 30%; } 50% { height: 100%; } }
body:not(.is-playing) .eq i { animation-play-state: paused; }

/* Up next */
.up-card { display: grid; grid-template-columns: 84px minmax(0, 1fr); align-items: center; gap: 14px; width: 100%; padding: 12px; border: 0; border-radius: 16px; background: var(--bg-2); text-align: left; scroll-snap-align: start; transition: background .15s; }
.up-card:hover { background: var(--fill-2); }
.up-art { width: 84px; border-radius: 10px; box-shadow: 0 6px 16px -8px rgba(0,0,0,.45); }
.up-text { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.up-show { font-size: 12px; font-weight: 600; color: var(--text-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.up-title { font-size: 14.5px; font-weight: 600; line-height: 1.3; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.up-left { display: flex; align-items: center; gap: 8px; margin-top: 6px; font-size: 12.5px; color: var(--text-2); }
.up-left .pill-prog { width: 64px; color: var(--accent); }

/* Search and forms */
.big-search { display: flex; align-items: center; gap: 10px; max-width: 640px; height: 48px; margin: 0 0 6px; padding: 0 16px; border-radius: 14px; background: var(--fill); }
.big-search svg { color: var(--text-3); }
.big-search input { flex: 1; min-width: 0; border: 0; background: none; outline: none; font-size: 17px; }
.big-search:focus-within { background: var(--bg); box-shadow: 0 0 0 3px var(--accent-soft), inset 0 0 0 1px color-mix(in srgb, var(--accent) 60%, transparent); }
.inline-add { display: flex; align-items: center; gap: 8px; }
.inline-add input { width: min(300px, 60vw); height: 34px; padding: 0 12px; border: 0; border-radius: 10px; background: var(--fill); outline: none; font-size: 13.5px; }
.inline-add input:focus { box-shadow: 0 0 0 3px var(--accent-soft); }

/* Player bar */
.bar { position: fixed; z-index: 50; left: calc(var(--side-w) + 16px); right: 16px; bottom: calc(14px + env(safe-area-inset-bottom, 0px)); height: var(--bar-h); max-width: 1200px; margin-inline: auto; container-type: inline-size; display: grid; grid-template-columns: minmax(0, 1fr) minmax(240px, 1.35fr) auto; align-items: center; gap: 16px; padding: 0 12px 0 10px; border-radius: 18px; background: var(--bar); -webkit-backdrop-filter: saturate(180%) blur(28px); backdrop-filter: saturate(180%) blur(28px); box-shadow: var(--shadow-float); transition: transform .45s cubic-bezier(.2,.8,.2,1), opacity .3s; }
.bar.idle { transform: translateY(calc(100% + 40px)); opacity: 0; pointer-events: none; }
.bar-info { display: flex; align-items: center; gap: 12px; min-width: 0; }
.bar-art { flex: none; width: 52px; height: 52px; padding: 0; border: 0; border-radius: 10px; background: none; box-shadow: 0 6px 14px -6px rgba(0,0,0,.45); }
.bar-art .art { border-radius: 10px; }
.bar-meta { display: flex; flex-direction: column; min-width: 0; }
.bar-title { padding: 0; border: 0; background: none; text-align: left; font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bar-show { font-size: 12.5px; color: var(--text-2); text-decoration: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.bar-show:hover { text-decoration: underline; }
.bar-center { display: flex; flex-direction: column; align-items: center; min-width: 0; }
.bar-buttons { display: flex; align-items: center; gap: 12px; }
.ctl { width: 36px; height: 36px; border: 0; border-radius: 50%; display: grid; place-items: center; background: none; color: var(--text); transition: transform .12s, background .15s; }
.ctl:hover:not(:disabled) { background: var(--fill); }
.ctl:active:not(:disabled) { transform: scale(.92); }
.ctl svg { width: 22px; height: 22px; }
.ctl.skip svg { width: 25px; height: 25px; }
.bar .ctl.play { width: 40px; height: 40px; background: var(--text); color: var(--bg); }
.bar .ctl.play svg { width: 19px; height: 19px; }
.bar-scrub { display: grid; grid-template-columns: 46px minmax(0, 1fr) 50px; align-items: center; gap: 8px; width: 100%; font-size: 11px; color: var(--text-2); font-variant-numeric: tabular-nums; }
.bar-scrub span:first-child { text-align: right; }
.bar-tools { display: flex; align-items: center; justify-content: flex-end; gap: 2px; min-width: 0; }
.tool { height: 32px; min-width: 32px; padding: 0 7px; border: 0; border-radius: 8px; display: inline-flex; align-items: center; justify-content: center; gap: 5px; background: none; color: var(--text-2); font-size: 12.5px; font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.tool:hover { background: var(--fill); color: var(--text); }
.tool[aria-pressed="true"] { color: var(--accent); }
.tool svg { width: 19px; height: 19px; }
.tool.speed { min-width: 44px; }
.vol { display: flex; align-items: center; gap: 2px; }
.vol .seek { width: 84px; }
.bar-prog { display: none; position: absolute; left: 14px; right: 14px; bottom: 4px; height: 2.5px; border-radius: 2px; background: var(--fill-2); overflow: hidden; }
.bar-prog i { display: block; width: 0; height: 100%; background: hsl(var(--th) var(--ts) var(--tl)); }
.bar .seek { --played: hsl(var(--th) var(--ts) var(--tl)); }
/* the player bar sheds its least-used controls as it gets narrower, instead of overlapping */
@container (max-width: 980px) { .bar .vol .seek { display: none; } }
@container (max-width: 840px) { #barExpand { display: none; } }
@container (max-width: 720px) { #barLyrics, #barSleep { display: none; } .bar-scrub { grid-template-columns: 38px minmax(0, 1fr) 42px; } }
body.np-open .bar { opacity: 0; pointer-events: none; }

/* Sliders */
.seek { -webkit-appearance: none; appearance: none; width: 100%; height: 16px; margin: 0; background: transparent; cursor: pointer; --fill: 0%; --buf: 0%; --track: var(--fill-2); --bufc: color-mix(in srgb, var(--text) 16%, transparent); --played: var(--text-2); }
.seek:disabled { cursor: default; }
.seek::-webkit-slider-runnable-track { height: 4px; border-radius: 2px; background: linear-gradient(var(--played), var(--played)) 0 0 / var(--fill) 100% no-repeat, linear-gradient(var(--bufc), var(--bufc)) 0 0 / var(--buf) 100% no-repeat, var(--track); }
.seek::-moz-range-track { height: 4px; border-radius: 2px; background: linear-gradient(var(--played), var(--played)) 0 0 / var(--fill) 100% no-repeat, linear-gradient(var(--bufc), var(--bufc)) 0 0 / var(--buf) 100% no-repeat, var(--track); }
.seek::-webkit-slider-thumb { -webkit-appearance: none; width: 12px; height: 12px; margin-top: -4px; border: 0; border-radius: 50%; background: var(--played); opacity: 0; transition: opacity .15s; }
.seek::-moz-range-thumb { width: 12px; height: 12px; border: 0; border-radius: 50%; background: var(--played); opacity: 0; }
.seek:hover::-webkit-slider-thumb, .seek:focus-visible::-webkit-slider-thumb, .seek.dragging::-webkit-slider-thumb { opacity: 1; }
.seek:hover::-moz-range-thumb, .seek:focus-visible::-moz-range-thumb { opacity: 1; }
.seek:focus-visible { outline: none; }

/* Caption pill over the app */
.cap-pill { position: fixed; z-index: 45; left: calc(var(--side-w) + 24px); right: 24px; bottom: calc(14px + var(--bar-h) + 14px + env(safe-area-inset-bottom, 0px)); width: fit-content; max-width: 700px; margin-inline: auto; padding: 10px 18px 11px; border-radius: 14px; background: rgba(18,18,22,.8); color: #fff; -webkit-backdrop-filter: blur(20px) saturate(160%); backdrop-filter: blur(20px) saturate(160%); box-shadow: 0 10px 30px -12px rgba(0,0,0,.5); text-align: center; font-size: 16px; font-weight: 600; line-height: 1.4; pointer-events: none; }
.cap-pill .who { display: block; margin-bottom: 2px; font-size: 11.5px; font-weight: 600; color: hsl(var(--th) 85% 78%); }
body.np-open .cap-pill { display: none; }

/* Now Playing */
.np { position: fixed; inset: 0; z-index: 100; display: flex; flex-direction: column; overflow: hidden; color: #fff; background: #0E0E12; opacity: 0; transform: translateY(36px) scale(.985); transition: opacity 280ms var(--ease-out), transform 520ms var(--ease-out); }
.np.closing { transition: opacity 200ms var(--ease-in), transform 240ms var(--ease-in); }
.np.open { opacity: 1; transform: none; }
.np-bg { position: absolute; inset: -18%; z-index: 0; background: radial-gradient(60% 60% at 25% 25%, hsl(var(--th) 70% 45%), transparent 70%), radial-gradient(60% 60% at 80% 80%, hsl(calc(var(--th) + 40) 60% 32%), transparent 70%), #121018; }
.np-bg img { width: 100%; height: 100%; object-fit: cover; filter: blur(70px) saturate(1.7) brightness(.82); transform: scale(1.15); }
.np-scrim { position: absolute; inset: 0; z-index: 0; background: linear-gradient(180deg, rgba(0,0,0,.2), rgba(0,0,0,.52)); }
.np-top { position: relative; z-index: 1; display: flex; align-items: center; justify-content: space-between; padding: calc(14px + env(safe-area-inset-top, 0px)) 20px 0; }
.np-btn { width: 40px; height: 40px; border: 0; border-radius: 50%; display: grid; place-items: center; background: rgba(255,255,255,.13); color: #fff; }
.np-btn:hover { background: rgba(255,255,255,.22); }
.np-btn[aria-pressed="true"] { background: #fff; color: #15121c; }
.np-label { font-size: 13px; font-weight: 600; color: rgba(255,255,255,.72); }
.np-body { position: relative; z-index: 1; flex: 1; min-height: 0; width: 100%; max-width: 1360px; margin: 0 auto; overflow-y: auto; overflow-x: hidden; scrollbar-width: none; display: grid; grid-template-columns: minmax(300px, 430px) minmax(0, 1fr); grid-template-rows: minmax(0, 1fr); align-items: stretch; gap: clamp(32px, 6vw, 96px); padding: 8px clamp(24px, 6vw, 88px) calc(26px + env(safe-area-inset-bottom, 0px)); }
.np:not(.lyrics-on) .np-body { grid-template-columns: minmax(300px, 430px); justify-content: center; }
.np:not(.lyrics-on) .np-right { display: none; }
.np-left { display: flex; flex-direction: column; gap: 16px; min-height: 0; margin-block: auto; }
.np-art { flex: none; align-self: center; width: min(100%, max(170px, calc(100vh - 430px))); width: min(100%, max(170px, calc(100dvh - 430px))); border-radius: 14px; overflow: hidden; box-shadow: 0 30px 60px -20px rgba(0,0,0,.75); transition: transform .55s cubic-bezier(.2,.8,.2,1); }
.np-art .art { border-radius: 14px; }
.np.paused .np-art { transform: scale(.9); }
.np-title { margin: 0; font-size: 21px; font-weight: 700; line-height: 1.25; letter-spacing: -.015em; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.np-show { display: inline-block; margin-top: 3px; color: rgba(255,255,255,.72); font-size: 16px; text-decoration: none; }
.np-show:hover { color: #fff; text-decoration: underline; }
.np-caption { min-height: 2.9em; margin: 0; font-size: 15px; line-height: 1.45; color: rgba(255,255,255,.88); }
.np .seek { --track: rgba(255,255,255,.24); --bufc: rgba(255,255,255,.34); --played: #fff; }
.np-times { display: flex; justify-content: space-between; margin-top: 3px; font-size: 12px; color: rgba(255,255,255,.62); font-variant-numeric: tabular-nums; }
.np-transport { display: flex; align-items: center; justify-content: space-between; padding: 0 4px; }
.np .ctl { width: 50px; height: 50px; color: #fff; }
.np .ctl:hover:not(:disabled) { background: rgba(255,255,255,.12); }
.np .ctl svg { width: 30px; height: 30px; }
.np .ctl.skip svg { width: 34px; height: 34px; }
.np .ctl.play { width: 72px; height: 72px; background: #fff; color: #15121c; }
.np .ctl.play svg { width: 30px; height: 30px; }
.np-tools { display: flex; align-items: center; justify-content: space-between; gap: 4px; }
.np .tool { color: rgba(255,255,255,.75); }
.np .tool:hover { background: rgba(255,255,255,.12); color: #fff; }
.np .tool[aria-pressed="true"] { background: rgba(255,255,255,.2); color: #fff; }
.np .vol .seek { width: 90px; }
.np-right { position: relative; height: 100%; min-height: 0; align-self: stretch; }
.lyrics { position: relative; list-style: none; height: 100%; margin: 0; padding: 32vh 0 42vh; overflow-y: auto; scrollbar-width: none; -webkit-mask-image: linear-gradient(transparent, #000 14%, #000 78%, transparent); mask-image: linear-gradient(transparent, #000 14%, #000 78%, transparent); }
.lyrics::-webkit-scrollbar { display: none; }
.lyric { margin: 0 0 22px; max-width: 30ch; font-size: clamp(22px, 2.3vw, 32px); font-weight: 700; line-height: 1.26; letter-spacing: -.015em; color: rgba(255,255,255,.32); cursor: pointer; transform: scale(.975); transform-origin: 0 50%; transition: color 420ms var(--ease-std), transform 520ms var(--ease-out); }
.lyric:hover { color: rgba(255,255,255,.58); }
.lyric.active { color: #fff; transform: none; }
.lyric .who { display: block; margin-bottom: 4px; font-size: 13px; font-weight: 600; letter-spacing: .02em; color: rgba(255,255,255,.55); }
.np-notes { height: 100%; overflow-y: auto; padding: 8vh 0; max-width: 62ch; scrollbar-width: thin; }
.np-notes h3 { margin: 0 0 12px; font-size: 13px; font-weight: 600; color: rgba(255,255,255,.6); }
.notes-text { margin: 0; font-size: 16px; line-height: 1.65; color: rgba(255,255,255,.86); white-space: pre-line; }
.notes-actions { margin-top: 22px; }
.np-chip { display: inline-flex; align-items: center; gap: 8px; height: 36px; padding: 0 16px; border: 0; border-radius: 999px; background: rgba(255,255,255,.16); color: #fff; font-size: 14px; font-weight: 600; }
.np-chip:hover { background: rgba(255,255,255,.26); }
.np-chip svg { width: 17px; height: 17px; }

/* Speed popover */
.pop { position: fixed; z-index: 130; width: 272px; padding: 14px; border-radius: 16px; background: var(--pop-bg); color: var(--text); box-shadow: var(--shadow-float); }
.pop.on-dark { background: rgba(40,38,48,.94); color: #fff; -webkit-backdrop-filter: blur(30px); backdrop-filter: blur(30px); box-shadow: 0 18px 50px -12px rgba(0,0,0,.6), 0 0 0 .5px rgba(255,255,255,.12); }
.pop-row { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
.pop-val { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; letter-spacing: -.01em; }
.pop .seek { --played: var(--accent); }
.pop.on-dark .seek { --played: #fff; --track: rgba(255,255,255,.24); }
.pop .seek::-webkit-slider-thumb { opacity: 1; }
.pop .seek::-moz-range-thumb { opacity: 1; }
.presets { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 12px; }
.preset { height: 30px; border: 0; border-radius: 8px; background: var(--fill); font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }
.preset:hover { background: var(--fill-2); }
.preset[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); }
.pop.on-dark .preset { background: rgba(255,255,255,.12); color: #fff; }
.pop.on-dark .preset[aria-pressed="true"] { background: #fff; color: #15121c; }

/* Toast */
.toast { position: fixed; z-index: 200; top: calc(16px + env(safe-area-inset-top, 0px)); left: 50%; max-width: min(92vw, 520px); padding: 10px 16px; border-radius: 12px; background: var(--pop-bg); color: var(--text); font-size: 14px; font-weight: 500; box-shadow: var(--shadow-float); opacity: 0; pointer-events: none; transform: translate(-50%, -14px); transition: opacity .2s, transform .28s cubic-bezier(.2,.8,.2,1); }
.toast.show { opacity: 1; transform: translate(-50%, 0); }
.toast.error { background: var(--accent); color: var(--accent-ink); }

/* Phone tab bar */
.tabbar { display: none; }

/* Medium: navigation rail */
@media (max-width: 1099px) and (min-width: 760px) {
  :root { --side-w: 84px; }
  .sidebar { padding: 16px 8px 10px; }
  .brand { justify-content: center; padding: 2px 0 14px; }
  .brand-name, .side-search, .nav-head, .side-shows { display: none; }
  .nav a { flex-direction: column; justify-content: center; height: 60px; gap: 4px; padding: 0; font-size: 10.5px; font-weight: 600; text-align: center; white-space: nowrap; letter-spacing: -.01em; }
  .nav a svg { width: 22px; height: 22px; }
  .rail-only { display: block !important; }
  .side-foot { align-items: center; }
  .side-tool { justify-content: center; width: 44px; height: 36px; padding: 0; }
  .side-tool span { display: none; }
  .bar { gap: 12px; }
  .sidebar { overflow-y: auto; scrollbar-width: none; }
  .bar .vol .seek { display: none; }
}
/* Phones */
@media (max-width: 759px) {
  .app { grid-template-columns: minmax(0, 1fr); }
  .sidebar { display: none; }
  :root { --gutter: 18px; }
  .view { padding: 22px var(--gutter) calc(170px + env(safe-area-inset-bottom, 0px)); }
  .page-title { font-size: 30px; }
  .tabbar { position: fixed; z-index: 55; left: 0; right: 0; bottom: 0; display: grid; grid-template-columns: repeat(4, 1fr); padding: 6px 4px calc(4px + env(safe-area-inset-bottom, 0px)); background: var(--bar); -webkit-backdrop-filter: saturate(180%) blur(28px); backdrop-filter: saturate(180%) blur(28px); border-top: .5px solid var(--sep); }
  .tabbar a { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 4px 0; font-size: 10.5px; font-weight: 600; color: var(--text-3); text-decoration: none; }
  .tabbar a svg { width: 24px; height: 24px; }
  .tabbar a[aria-current="page"] { color: var(--accent); }
  .bar { left: 8px; right: 8px; bottom: calc(60px + env(safe-area-inset-bottom, 0px)); height: 60px; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; padding: 0 6px 0 6px; border-radius: 14px; }
  .bar-art { width: 46px; height: 46px; }
  .bar-scrub, .bar-tools, #barBack { display: none; }
  .bar-prog { display: block; }
  .cap-pill { left: 14px; right: 14px; bottom: calc(60px + 60px + 18px + env(safe-area-inset-bottom, 0px)); max-width: none; font-size: 15px; }
  .hero { grid-template-columns: minmax(0, 1fr); padding: 24px; min-height: 0; }
  .hero-stack { display: none; }
  .show-hero { grid-template-columns: minmax(0, 1fr); justify-items: start; margin-top: -22px; padding-top: 28px; }
  .hero-art { width: min(58vw, 230px); }
  .row { --card-w: 140px; gap: 14px; }
  .row.wide { --card-w: 290px; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(128px, 1fr)); gap: 22px 14px; }
  .row-nav { display: none; }
  .page-head .inline-add { width: 100%; }
  .inline-add input { flex: 1; width: auto; }
}
/* Now Playing on narrower screens */
@media (max-width: 899px) {
  .np-body, .np:not(.lyrics-on) .np-body { display: flex; flex-direction: column; align-items: stretch; gap: 14px; padding: 4px 24px calc(20px + env(safe-area-inset-bottom, 0px)); }
  .np-left { display: contents; }
  .np-art { order: 1; flex: none; width: min(100%, max(150px, calc(100vh - 410px))); width: min(100%, max(150px, calc(100dvh - 410px))); align-self: center; }
  .np:not(.lyrics-on) .np-art { margin-top: auto; }
  .np:not(.lyrics-on) .np-tools { margin-bottom: auto; }
  .np-meta { order: 2; }
  .np-caption { order: 3; }
  .np-right { order: 4; flex: 1 1 auto; min-height: 220px; display: none; }
  .np-scrub { order: 5; }
  .np-transport { order: 6; }
  .np-tools { order: 7; }
  .np.lyrics-on .np-art, .np.lyrics-on .np-caption { display: none; }
  .np.lyrics-on .np-right { display: block; }
  .np:not(.lyrics-on) .np-right { display: none; }
  .lyric { font-size: 24px; }
  .lyrics { padding: 10vh 0 34vh; }
  .np .vol { display: none; }
}
/* content-width rules: these follow the space the page actually has, not the window */
@container view (max-width: 760px) { .hero { grid-template-columns: minmax(0, 1fr); min-height: 0; padding: 26px; } .hero-stack { display: none; } }
@container view (max-width: 560px) { .show-hero { grid-template-columns: minmax(0, 1fr); justify-items: start; } .hero-art { width: min(58vw, 230px); } }
.source-box { margin-top: 16px; max-width: 580px; }
.source-row { display: flex; gap: 8px; }
.source-row input { flex: 1; min-width: 0; height: 36px; padding: 0 12px; border: 0; border-radius: 10px; background: var(--fill); outline: none; font-size: 14px; }
.source-row input:focus { background: var(--bg); box-shadow: 0 0 0 3px var(--accent-soft), inset 0 0 0 1px color-mix(in srgb, var(--accent) 60%, transparent); }
.source-note { margin: 8px 0 0; font-size: 13px; color: var(--text-2); }
.show-source { margin: 12px 0 0; font-size: 13px; color: var(--text-3); }
.np-status { position: absolute; z-index: 2; top: 6px; left: 0; right: 0; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; font-size: 13px; color: rgba(255,255,255,.75); }
.np-status .meter, .cap-card .meter { width: 120px; height: 4px; background: rgba(255,255,255,.2); }
.np-status .meter i, .cap-card .meter i { background: #fff; }
.link-btn.light { color: rgba(255,255,255,.9); text-decoration: underline; text-decoration-color: rgba(255,255,255,.35); text-underline-offset: 3px; }
.cap-card { max-width: 56ch; margin: 0 0 28px; padding: 18px 20px; border-radius: 16px; background: rgba(255,255,255,.11); }
.np-notes .cap-card h3 { margin: 0 0 6px; font-size: 17px; font-weight: 700; color: #fff; }
.cap-card p { margin: 0 0 12px; font-size: 14.5px; line-height: 1.55; color: rgba(255,255,255,.82); }
.cap-card code { display: block; margin: 0 0 12px; padding: 10px 12px; border-radius: 10px; background: rgba(0,0,0,.35); color: #fff; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: nowrap; overflow-x: auto; user-select: all; }
.cap-card .meter { width: 100%; height: 5px; margin: 6px 0 12px; }
.cap-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.cap-pill.hint { font-size: 13.5px; font-weight: 500; color: rgba(255,255,255,.85); }
@media (max-height: 520px) and (min-width: 600px) {
  .np-top { padding-top: calc(8px + env(safe-area-inset-top, 0px)); }
  .np-body, .np:not(.lyrics-on) .np-body { display: grid; grid-template-columns: auto minmax(0, 1fr); grid-template-rows: repeat(5, auto); grid-template-areas: "art meta" "art caption" "art scrub" "art transport" "art tools"; align-content: center; column-gap: 32px; row-gap: 10px; padding: 4px 32px calc(12px + env(safe-area-inset-bottom, 0px)); }
  .np-left { display: contents; }
  .np-art { grid-area: art; width: max(140px, calc(100vh - 130px)); width: max(140px, calc(100dvh - 130px)); margin: 0 !important; align-self: center; }
  .np-meta { grid-area: meta; }
  .np-caption { grid-area: caption; min-height: 0; }
  .np-scrub { grid-area: scrub; }
  .np-transport { grid-area: transport; }
  .np-tools { grid-area: tools; margin: 0 !important; }
  .np-right { grid-area: art; align-self: center; width: min(46vw, 440px); height: calc(100vh - 110px); height: calc(100dvh - 110px); min-height: 0; }
  .np.lyrics-on .np-right { display: block; }
  .np:not(.lyrics-on) .np-right { display: none; }
  .np.lyrics-on .np-art { display: none; }
  .lyrics { padding: 4vh 0 30vh; }
  .lyric { font-size: 20px; }
  .np .ctl.play { width: 60px; height: 60px; }
}
.switch { -webkit-appearance: none; appearance: none; flex: none; position: relative; width: 51px; height: 31px; margin: 0; border-radius: 999px; background: var(--fill-2); cursor: pointer; transition: background .2s; }
.switch::before { content: ""; position: absolute; top: 2px; left: 2px; width: 27px; height: 27px; border-radius: 50%; background: #fff; box-shadow: 0 2px 6px rgba(0,0,0,.25), 0 0 0 .5px rgba(0,0,0,.04); transition: transform .22s cubic-bezier(.2,.8,.2,1); }
.switch:checked { background: var(--on); }
.switch:checked::before { transform: translateX(20px); }
.switch:active::before { width: 32px; }
.switch:checked:active::before { transform: translateX(15px); }
.switch::before { transition: transform 260ms var(--ease-out), width 200ms var(--ease-out) !important; }
.switch.small { width: 40px; height: 24px; }
.switch.small::before { width: 20px; height: 20px; }
.switch.small:checked::before { transform: translateX(16px); }
.switch.small:active::before { width: 24px; }
.switch.small:checked:active::before { transform: translateX(12px); }
.switch.on-dark { background: rgba(255,255,255,.26); }
.switch.on-dark:checked { background: var(--on); }
.switch:focus-visible { outline: 2.5px solid color-mix(in srgb, var(--accent) 75%, transparent); outline-offset: 2px; }
.side-switch { display: flex; align-items: center; gap: 10px; min-height: 34px; padding: 0 8px 0 10px; border-radius: 8px; color: var(--text-2); font-size: 13px; cursor: pointer; }
.side-switch:hover { background: var(--fill); color: var(--text); }
.side-switch svg { width: 16px; height: 16px; }
.side-switch:has(input:checked) svg { color: var(--accent); }
.sw-label { flex: 1; min-width: 0; }
a.side-tool { text-decoration: none; }
.side-tool[aria-current="page"] { background: var(--fill-2); color: var(--text); }
.cap-switch { display: inline-flex; align-items: center; gap: 8px; font-size: 13.5px; color: rgba(255,255,255,.85); cursor: pointer; }
.np-status .cap-switch { margin-left: auto; }
.head-tools { display: flex; align-items: center; gap: 10px; }
.phone-only { display: none !important; }
.seg { position: relative; display: inline-flex; flex-wrap: wrap; gap: 2px; padding: 2px; border-radius: 10px; background: var(--fill); }
.seg-thumb { position: absolute; top: 0; left: 0; border-radius: 8px; background: var(--pop-bg); box-shadow: 0 1px 3px rgba(0,0,0,.14), 0 0 0 .5px rgba(0,0,0,.05); opacity: 0; transition: transform 380ms var(--ease-out), width 380ms var(--ease-out), height 380ms var(--ease-out); }
.seg-thumb.on { opacity: 1; }
.seg-thumb.instant { transition: none; }
.seg-btn { height: 30px; padding: 0 12px; border: 0; border-radius: 8px; background: none; font-size: 13px; font-weight: 600; color: var(--text); white-space: nowrap; }
.seg-btn { position: relative; z-index: 1; }
.seg:not(:has(.seg-thumb.on)) .seg-btn[aria-checked="true"] { background: var(--pop-bg); box-shadow: 0 1px 3px rgba(0,0,0,.14), 0 0 0 .5px rgba(0,0,0,.05); }
.set-group { max-width: 760px; margin-top: 28px; }
.set-head { margin: 0 0 8px 16px; font-size: 13px; font-weight: 600; color: var(--text-2); }
.set-card { border-radius: 14px; background: var(--bg-2); overflow: hidden; }
.set-row { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px 18px; min-height: 56px; padding: 13px 16px; }
.set-row + .set-row { border-top: 1px solid var(--sep); }
.set-text { flex: 1 1 260px; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.set-title { font-size: 15px; font-weight: 500; }
.set-sub { font-size: 13px; line-height: 1.45; color: var(--text-2); }
.set-sub code { display: block; margin-top: 8px; padding: 8px 10px; border-radius: 8px; background: var(--fill); color: var(--text); font: 12.5px/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; white-space: nowrap; overflow-x: auto; user-select: all; }
.set-ok { color: var(--on); font-weight: 600; }
.set-row > .seg, .set-row > .btn, .set-row > .switch { margin-left: auto; }
@media (max-width: 1099px) and (min-width: 760px) {
  .side-switch { justify-content: center; width: 44px; padding: 0; }
  .side-switch .sw-label, .side-switch input { display: none; }
}
@media (max-width: 759px) {
  .phone-only { display: grid !important; }
  .page-head .head-tools { width: 100%; }
  .head-tools .inline-add { flex: 1; width: auto; min-width: 0; }
}
.nav-ind { position: absolute; z-index: 1; left: 0; top: 0; width: 3px; height: 18px; border-radius: 3px; background: var(--accent); opacity: 0; pointer-events: none; transform: translate(calc(var(--x, 0px) + 4px), var(--y, 0px)); transition: transform 440ms var(--ease-out), opacity 150ms; }
.tab-ind { position: absolute; z-index: 1; top: 0; left: 0; width: 28px; height: 3px; border-radius: 0 0 3px 3px; background: var(--accent); opacity: 0; pointer-events: none; transform: translateX(var(--x, 0px)); transition: transform 440ms var(--ease-out), opacity 150ms; }
.nav-ind.on, .tab-ind.on { opacity: 1; }
.nav-ind.instant, .tab-ind.instant { transition: none; }
html:not(.no-glow) :is(.side-shows a, .nav a, .side-tool, .side-switch, .set-row, .ep, .up-card):hover {
  background-image: radial-gradient(180px circle at var(--rx, 50%) var(--ry, 50%), var(--reveal), transparent 72%); }
.icon-round, .chip, .tool, .np-btn, .np-chip, .seg-btn, .pill, .btn { transition: transform 140ms var(--ease-std), background-color 160ms, color 160ms, filter 160ms; }
.icon-round:active, .chip:active, .tool:active, .np-btn:active, .np-chip:active, .seg-btn:active { transform: scale(.94); }
.bar { transition: transform 520ms var(--ease-out), opacity 300ms var(--ease-out); }
.toast { transition: opacity 220ms var(--ease-out), transform 360ms var(--ease-out); }
::view-transition-old(root), ::view-transition-new(root) { animation-duration: 280ms; animation-timing-function: var(--ease-out); }
::view-transition-group(show-art) { animation-duration: 520ms; animation-timing-function: cubic-bezier(0.1, 0.9, 0.2, 1); }
::view-transition-old(show-art), ::view-transition-new(show-art) { height: 100%; mix-blend-mode: normal; }
.sel, .num, .txt { height: 32px; padding: 0 10px; border: 0; border-radius: 8px; background: var(--fill); color: var(--text); font-size: 13.5px; outline: none; }
.sel:focus-visible, .num:focus-visible, .txt:focus-visible { box-shadow: 0 0 0 3px var(--accent-soft); }
.num { width: 96px; text-align: right; font-variant-numeric: tabular-nums; }
.txt { width: min(340px, 100%); }
.set-row > .sel, .set-row > .num, .set-row > .txt, .set-row > .set-actions { margin-left: auto; }
.set-subhead { padding: 14px 16px 2px; border-top: 1px solid var(--sep); font-size: 12px; font-weight: 700; letter-spacing: .02em; color: var(--accent); }
.set-card > .set-subhead:first-child { border-top: 0; }
.set-subhead + .set-row { border-top: 0; }
.set-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.pro-box .set-group { margin-top: 14px; }
.phone-box { padding: 0 16px 16px; }
.move-card { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px 24px; margin: 4px 0 28px; padding: 18px 20px; border-radius: 16px; background: var(--accent-soft); }
.move-text h2 { margin: 0 0 4px; font-size: 17px; }
.move-text p, .move-note { margin: 0; max-width: 62ch; font-size: 14.5px; color: var(--text-2); }
.phone-link { display: block; margin: 8px 0 12px; padding: 10px 12px; border-radius: 10px; background: var(--fill); font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; user-select: all; overflow-wrap: anywhere; }
html.no-motion *, html.no-motion *::before, html.no-motion *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }
</style>
</head>
<body>
<svg class="sprite" aria-hidden="true" focusable="false"><defs>
  <symbol id="i-play" viewBox="0 0 24 24"><path fill="currentColor" d="M8 5.6v12.8a1 1 0 0 0 1.52.85l10.2-6.4a1 1 0 0 0 0-1.7L9.52 4.75A1 1 0 0 0 8 5.6z"/></symbol>
  <symbol id="i-pause" viewBox="0 0 24 24"><rect fill="currentColor" x="6.2" y="5" width="4.2" height="14" rx="1.3"/><rect fill="currentColor" x="13.6" y="5" width="4.2" height="14" rx="1.3"/></symbol>
  <symbol id="i-prev" viewBox="0 0 24 24"><rect fill="currentColor" x="5" y="6" width="2.2" height="12" rx="1"/><path fill="currentColor" d="M18.5 6.9v10.2a.9.9 0 0 1-1.38.76L9.6 12.76a.9.9 0 0 1 0-1.52l7.52-5.1a.9.9 0 0 1 1.38.76z"/></symbol>
  <symbol id="i-next" viewBox="0 0 24 24"><rect fill="currentColor" x="16.8" y="6" width="2.2" height="12" rx="1"/><path fill="currentColor" d="M5.5 6.9v10.2a.9.9 0 0 0 1.38.76l7.52-5.1a.9.9 0 0 0 0-1.52L6.88 6.14a.9.9 0 0 0-1.38.76z"/></symbol>
  <symbol id="i-back" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" d="M1 4v6h6M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/><text x="12.6" y="15.3" text-anchor="middle" font-size="7.2" font-weight="700" fill="currentColor" font-family="system-ui, sans-serif">15</text></symbol>
  <symbol id="i-fwd" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" d="M23 4v6h-6M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/><text x="11.4" y="15.3" text-anchor="middle" font-size="7.2" font-weight="700" fill="currentColor" font-family="system-ui, sans-serif">30</text></symbol>
  <symbol id="i-vol" viewBox="0 0 24 24"><path fill="currentColor" d="M4 9.2h3.3l4.4-3.7a.6.6 0 0 1 1 .46v12.08a.6.6 0 0 1-1 .46L7.3 14.8H4a1 1 0 0 1-1-1v-3.6a1 1 0 0 1 1-1z"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M15.5 9.2a4 4 0 0 1 0 5.6M18.2 6.6a7.6 7.6 0 0 1 0 10.8"/></symbol>
  <symbol id="i-mute" viewBox="0 0 24 24"><path fill="currentColor" d="M4 9.2h3.3l4.4-3.7a.6.6 0 0 1 1 .46v12.08a.6.6 0 0 1-1 .46L7.3 14.8H4a1 1 0 0 1-1-1v-3.6a1 1 0 0 1 1-1z"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M16 9.5l5 5M21 9.5l-5 5"/></symbol>
  <symbol id="i-cc" viewBox="0 0 24 24"><rect fill="none" stroke="currentColor" stroke-width="1.7" x="3" y="5.5" width="18" height="13" rx="2.6"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M10.6 10.3a2.1 2.1 0 1 0 0 3.4M16.6 10.3a2.1 2.1 0 1 0 0 3.4"/></symbol>
  <symbol id="i-quote" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M5 4.8h14a1.6 1.6 0 0 1 1.6 1.6v8.2a1.6 1.6 0 0 1-1.6 1.6h-7l-4.6 3.6v-3.6H5a1.6 1.6 0 0 1-1.6-1.6V6.4A1.6 1.6 0 0 1 5 4.8z"/><path fill="currentColor" d="M8.6 8.8h2.6v2.3c0 1.3-.7 2.2-2 2.6l-.4-.8c.6-.3.9-.7.9-1.3H8.6zm4.8 0H16v2.3c0 1.3-.7 2.2-2 2.6l-.4-.8c.6-.3.9-.7.9-1.3h-1.1z"/></symbol>
  <symbol id="i-moon" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M19.5 14.6A7.6 7.6 0 0 1 9.4 4.5a7.6 7.6 0 1 0 10.1 10.1z"/></symbol>
  <symbol id="i-search" viewBox="0 0 24 24"><circle fill="none" stroke="currentColor" stroke-width="2" cx="10.5" cy="10.5" r="6.2"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M15.2 15.2l4.6 4.6"/></symbol>
  <symbol id="i-home" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" d="M4 10.6 12 4l8 6.6V19a1 1 0 0 1-1 1h-4.6v-5.6H9.6V20H5a1 1 0 0 1-1-1z"/></symbol>
  <symbol id="i-sparkle" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M11 4l1.7 4.6a2 2 0 0 0 1.2 1.2L18.5 11.5l-4.6 1.7a2 2 0 0 0-1.2 1.2L11 19l-1.7-4.6a2 2 0 0 0-1.2-1.2L3.5 11.5l4.6-1.7a2 2 0 0 0 1.2-1.2z"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M19 3v3.4M17.3 4.7h3.4"/></symbol>
  <symbol id="i-stack" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M5 4.5v15M9.5 4.5v15M13.6 5.4l4.8 14.1"/></symbol>
  <symbol id="i-refresh" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4.2h-4.2"/></symbol>
  <symbol id="i-sort" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M8 5v14M5 16l3 3 3-3M16 19V5M13 8l3-3 3 3"/></symbol>
  <symbol id="i-trash" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M4.5 7h15M10 7V4.8h4V7M6.5 7l.9 12.2h9.2L17.5 7"/></symbol>
  <symbol id="i-link" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/></symbol>
  <symbol id="i-plus" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" d="M12 5v14M5 12h14"/></symbol>
  <symbol id="i-check" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M5 12.5l4.5 4.5L19 7.5"/></symbol>
  <symbol id="i-chevleft" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M15 5l-7 7 7 7"/></symbol>
  <symbol id="i-chevright" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M9 5l7 7-7 7"/></symbol>
  <symbol id="i-chevdown" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" d="M5 9l7 7 7-7"/></symbol>
  <symbol id="i-expand" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M14 4h6v6M20 4l-6.5 6.5M10 20H4v-6M4 20l6.5-6.5"/></symbol>
  <symbol id="i-theme" viewBox="0 0 24 24"><circle fill="none" stroke="currentColor" stroke-width="1.7" cx="12" cy="12" r="8"/><path fill="currentColor" d="M12 4a8 8 0 0 1 0 16z"/></symbol>
  <symbol id="i-import" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14"/></symbol>
  <symbol id="i-export" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M12 15V4M7.5 8.5 12 4l4.5 4.5M5 19.5h14"/></symbol>
  <symbol id="i-infinity" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" d="M6.8 9.3a2.7 2.7 0 1 0 0 5.4c2.7 0 7.7-5.4 10.4-5.4a2.7 2.7 0 1 1 0 5.4c-2.7 0-7.7-5.4-10.4-5.4z"/></symbol>
  <symbol id="i-swap" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M5 8h13M15 4.5 18.5 8 15 11.5M19 16H6M9 12.5 5.5 16 9 19.5"/></symbol>
  <symbol id="i-wave" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" d="M4 11v2M7.5 8v8M11 5v14M14.5 9v6M18 7v10M21 11v2"/></symbol>
  <symbol id="i-gear" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></g></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M7 3.5h7l4.5 4.5v12.5H7zM14 3.5V8h4.5"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M9.5 13h6M9.5 16.5h4"/></symbol>
</defs></svg>

<div class="app" id="app">
  <nav class="sidebar" aria-label="Main">
    <a class="brand" href="#/home" aria-label="Afterhours, Listen Now"><span class="brand-sun" aria-hidden="true"></span><span class="brand-name">Afterhours</span></a>
    <form class="side-search" id="searchForm" role="search" autocomplete="off">
      <svg aria-hidden="true"><use href="#i-search"/></svg>
      <input id="searchInput" type="search" placeholder="Search or paste a link" aria-label="Search podcasts, or paste a feed or Apple Podcasts link" spellcheck="false" enterkeyhint="search">
    </form>
    <ul class="nav">
      <li><a href="#/home" data-route="home"><svg aria-hidden="true"><use href="#i-home"/></svg><span>Listen Now</span></a></li>
      <li><a href="#/discover" data-route="discover"><svg aria-hidden="true"><use href="#i-sparkle"/></svg><span>Discover</span></a></li>
      <li><a href="#/library" data-route="library"><svg aria-hidden="true"><use href="#i-stack"/></svg><span>Library</span></a></li>
      <li class="rail-only"><a href="#/search" data-route="search"><svg aria-hidden="true"><use href="#i-search"/></svg><span>Search</span></a></li>
    </ul>
    <span class="nav-ind" id="navInd" aria-hidden="true"></span>
    <p class="nav-head"><span>Your shows</span><span class="side-progress" id="sideProgress" hidden></span></p>
    <ul class="side-shows" id="sideShows" aria-label="Your shows"></ul>
    <div class="side-foot">
      <label class="side-switch" title="Auto captions: caption episodes that have none, on this computer"><svg aria-hidden="true"><use href="#i-wave"/></svg><span class="sw-label">Auto captions</span><input type="checkbox" class="switch small" id="autoCapSwitch" role="switch" data-setting="autoCaptions" aria-label="Auto captions"></label>
      <a class="side-tool" href="#/settings" data-route="settings" title="Settings"><svg aria-hidden="true"><use href="#i-gear"/></svg><span>Settings</span></a>
    </div>
  </nav>
  <main class="main" id="main" tabindex="-1"><div class="view" id="view"></div></main>
</div>

<nav class="tabbar" id="tabbar" aria-label="Sections">
  <a href="#/home" data-route="home"><svg aria-hidden="true"><use href="#i-home"/></svg><span>Listen Now</span></a>
  <a href="#/discover" data-route="discover"><svg aria-hidden="true"><use href="#i-sparkle"/></svg><span>Discover</span></a>
  <a href="#/library" data-route="library"><svg aria-hidden="true"><use href="#i-stack"/></svg><span>Library</span></a>
  <a href="#/search" data-route="search"><svg aria-hidden="true"><use href="#i-search"/></svg><span>Search</span></a>
  <span class="tab-ind" id="tabInd" aria-hidden="true"></span>
</nav>

<div class="cap-pill" id="capPill" aria-hidden="true" hidden><span class="who" id="capWho"></span><span id="capText"></span></div>

<section class="bar idle" id="bar" aria-label="Player">
  <div class="bar-info">
    <button class="bar-art" id="barArt" type="button" aria-label="Open Now Playing"></button>
    <div class="bar-meta">
      <button class="bar-title" id="barTitle" type="button" translate="no" title="Open Now Playing"></button>
      <a class="bar-show" id="barShow" translate="no" href="#/library"></a>
    </div>
  </div>
  <div class="bar-center">
    <div class="bar-buttons">
      <button class="ctl skip" id="barBack" type="button" aria-label="Back 15 seconds" title="Back 15 seconds (←)"><svg aria-hidden="true"><use href="#i-back"/></svg></button>
      <button class="ctl play" id="barPlay" type="button" aria-label="Play" title="Play (Space)"><svg aria-hidden="true"><use href="#i-play"/></svg></button>
      <button class="ctl skip" id="barFwd" type="button" aria-label="Forward 30 seconds" title="Forward 30 seconds (→)"><svg aria-hidden="true"><use href="#i-fwd"/></svg></button>
    </div>
    <div class="bar-scrub"><span id="barCur">0:00</span><input class="seek" id="barSeek" type="range" min="0" max="1000" step="1" value="0" aria-label="Position in episode"><span id="barLeft">-0:00</span></div>
  </div>
  <div class="bar-tools">
    <button class="tool speed" id="barSpeed" type="button" data-speed-btn aria-haspopup="dialog" aria-expanded="false" title="Playback speed ([ and ])">1×</button>
    <button class="tool" id="barCC" type="button" aria-pressed="true" aria-label="Captions" title="Captions (C)"><svg aria-hidden="true"><use href="#i-cc"/></svg></button>
    <button class="tool" id="barLyrics" type="button" aria-label="Transcript" title="Transcript (T)"><svg aria-hidden="true"><use href="#i-quote"/></svg></button>
    <button class="tool" id="barSleep" type="button" aria-pressed="false" aria-label="Sleep timer, off" title="Sleep timer"><svg aria-hidden="true"><use href="#i-moon"/></svg><span class="sleep-label" id="barSleepLabel" hidden></span></button>
    <div class="vol"><button class="tool" id="barMute" type="button" aria-label="Mute" title="Mute (M)"><svg aria-hidden="true"><use href="#i-vol"/></svg></button><input class="seek" id="barVol" type="range" min="0" max="1" step="0.01" value="1" aria-label="Volume"></div>
    <button class="tool" id="barExpand" type="button" aria-label="Open Now Playing" title="Now Playing"><svg aria-hidden="true"><use href="#i-expand"/></svg></button>
  </div>
  <div class="bar-prog" aria-hidden="true"><i id="barProg"></i></div>
</section>

<section class="np" id="np" aria-label="Now Playing" hidden>
  <div class="np-bg" id="npBg"></div>
  <div class="np-scrim"></div>
  <header class="np-top">
    <button class="np-btn" id="npClose" type="button" aria-label="Close Now Playing"><svg aria-hidden="true"><use href="#i-chevdown"/></svg></button>
    <span class="np-label">Now Playing</span>
    <button class="np-btn" id="npAuto" type="button" aria-pressed="true" aria-label="Play the next episode automatically" title="Autoplay next episode"><svg aria-hidden="true"><use href="#i-infinity"/></svg></button>
  </header>
  <div class="np-body">
    <div class="np-left">
      <div class="np-art" id="npArt"></div>
      <div class="np-meta"><h2 class="np-title" id="npTitle" translate="no"></h2><a class="np-show" id="npShow" translate="no" href="#/library"></a></div>
      <p class="np-caption" id="npCaption" aria-live="off"></p>
      <div class="np-scrub"><input class="seek" id="npSeek" type="range" min="0" max="1000" step="1" value="0" aria-label="Position in episode"><div class="np-times"><span id="npCur">0:00</span><span id="npLeft">-0:00</span></div></div>
      <div class="np-transport">
        <button class="ctl" id="npPrev" type="button" aria-label="Previous episode" title="Previous episode (Shift+P)"><svg aria-hidden="true"><use href="#i-prev"/></svg></button>
        <button class="ctl skip" id="npBack" type="button" aria-label="Back 15 seconds"><svg aria-hidden="true"><use href="#i-back"/></svg></button>
        <button class="ctl play" id="npPlay" type="button" aria-label="Play"><svg aria-hidden="true"><use href="#i-play"/></svg></button>
        <button class="ctl skip" id="npFwd" type="button" aria-label="Forward 30 seconds"><svg aria-hidden="true"><use href="#i-fwd"/></svg></button>
        <button class="ctl" id="npNext" type="button" aria-label="Next episode" title="Next episode (Shift+N)"><svg aria-hidden="true"><use href="#i-next"/></svg></button>
      </div>
      <div class="np-tools">
        <button class="tool speed" id="npSpeed" type="button" data-speed-btn aria-haspopup="dialog" aria-expanded="false" title="Playback speed">1×</button>
        <button class="tool" id="npCC" type="button" aria-pressed="true" aria-label="Captions" title="Captions (C)"><svg aria-hidden="true"><use href="#i-cc"/></svg></button>
        <button class="tool" id="npLyricsBtn" type="button" aria-pressed="true" aria-label="Transcript" title="Transcript (T)"><svg aria-hidden="true"><use href="#i-quote"/></svg></button>
        <button class="tool" id="npSleep" type="button" aria-pressed="false" aria-label="Sleep timer, off" title="Sleep timer"><svg aria-hidden="true"><use href="#i-moon"/></svg><span class="sleep-label" id="npSleepLabel" hidden></span></button>
        <div class="vol"><button class="tool" id="npMute" type="button" aria-label="Mute"><svg aria-hidden="true"><use href="#i-vol"/></svg></button><input class="seek" id="npVol" type="range" min="0" max="1" step="0.01" value="1" aria-label="Volume"></div>
      </div>
    </div>
    <div class="np-right" id="npRight">
      <div class="np-status" id="npStatus" aria-live="polite" hidden></div>
      <ol class="lyrics" id="lyrics" aria-label="Transcript"></ol>
      <div class="np-notes" id="npNotes" hidden></div>
    </div>
  </div>
</section>

<div class="pop" id="speedPop" role="dialog" aria-label="Playback speed" hidden>
  <div class="pop-row"><span class="pop-val" id="speedVal">1.00×</span><button class="link-btn" id="speedReset" type="button">Reset</button></div>
  <input class="seek" id="speedRange" type="range" min="0.5" max="3" step="0.05" value="1" aria-label="Playback speed">
  <div class="presets" id="speedPresets"></div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<input type="file" id="capFile" accept=".vtt,.srt,.json,text/vtt,application/x-subrip,application/json" hidden>
<input type="file" id="opmlFile" accept=".opml,.xml,text/xml,text/x-opml" hidden>
<audio id="audio" preload="metadata"></audio>

<script type="application/json" id="catalog">
{
  "categories": [
    {"id": "citypop", "label": "City pop & anime songs", "short": "City pop", "keywords": [["city pop", "us"], ["anime music", "us"], ["シティポップ", "jp"]]},
    {"id": "jazz", "label": "Jazz, hip-hop & piano", "short": "Jazz & piano", "keywords": [["jazz", "gb"], ["hip hop history", "us"], ["classical piano", "gb"]]},
    {"id": "tech", "label": "Gadgets & latest tech", "short": "Tech", "keywords": [["gadgets", "gb"], ["tech news", "gb"], ["ガジェット", "jp"]]},
    {"id": "lit", "label": "Literature & light lit", "short": "Books", "keywords": [["books", "gb"], ["ライトノベル", "jp"], ["读书", "tw"]]},
    {"id": "mystery", "label": "Mystery novels", "short": "Mystery", "keywords": [["mystery fiction", "gb"], ["ミステリー", "jp"], ["推理", "tw"]]},
    {"id": "knowledge", "label": "Economics, physics & astronomy", "short": "Knowledge", "keywords": [["astronomy", "gb"], ["physics", "gb"], ["economics", "gb"]]},
    {"id": "games", "label": "Game industry", "short": "Games", "keywords": [["video game history", "gb"], ["game development", "gb"], ["ゲーム", "jp"]]}
  ],
  "langs": {"ja": {"label": "日本語", "keywords": [["シティポップ", "jp"], ["アニソン", "jp"], ["ライトノベル", "jp"], ["ミステリー", "jp"], ["ガジェット", "jp"]]}, "zh": {"label": "中文", "keywords": [["推理", "tw"], ["读书", "tw"], ["音乐", "tw"], ["科技", "tw"], ["游戏", "tw"]]}},
  "presets": [
    {"key": "primer", "cat": "citypop", "title": "Primer", "author": "Maximum Fun", "artist": "Maximum Fun", "term": "Primer Maximum Fun", "countries": ["us", "gb"], "why": "Japanese city pop, one album per episode.", "site": "https://www.maximumfun.org/primer"},
    {"key": "mayonaka", "cat": "citypop", "title": "Mayonaka Hour 真夜中アワー", "match": "Mayonaka Hour", "author": "Yuki Tasaka & Van Paugam", "term": "Mayonaka Hour", "countries": ["us", "gb"], "why": "Profiles of city pop’s big names.", "site": "https://www.vanpaugam.com/podcast"},
    {"key": "linernotes", "cat": "citypop", "title": "Liner Notes", "match": "Liner Notes", "author": "Light in the Attic", "artist": "Light in the Attic", "term": "Light in the Attic Liner Notes", "countries": ["us", "gb"], "why": "Includes the Pacific Breeze city pop episode.", "site": "https://nl.player.fm/series/light-in-the-attics-liner-notes"},
    {"key": "heatrocks", "cat": "jazz", "title": "Heat Rocks", "author": "Maximum Fun", "artist": "Maximum Fun", "id": 1290971833, "term": "Heat Rocks", "countries": ["us", "gb"], "why": "Guests on one album: jazz, hip-hop, soul. A finished archive of about 200 episodes.", "site": "https://www.maximumfun.org/shows/heat-rocks"},
    {"key": "jazznight", "cat": "jazz", "title": "Jazz Night in America", "author": "NPR", "artist": "NPR", "term": "Jazz Night in America", "countries": ["us", "gb"], "why": "Live jazz from NPR and WBGO.", "site": "https://www.npr.org/series/347139849/jazz-night-in-america"},
    {"key": "takeitpersonal", "cat": "jazz", "title": "Take It Personal", "match": "Take It Personal", "author": "Philaflava", "term": "Take It Personal hip hop", "countries": ["us", "gb"], "why": "Golden-era, boom-bap East Coast hip-hop."},
    {"key": "composer", "cat": "jazz", "title": "Composer of the Week", "author": "BBC Radio 3", "artist": "BBC", "id": 275377513, "term": "Composer of the Week", "countries": ["gb"], "why": "Portraits of composers, with plenty of piano. The feed is UK-only."},
    {"key": "songexploder", "cat": "jazz", "title": "Song Exploder", "author": "Hrishikesh Hirway", "artist": "Hrishikesh Hirway", "term": "Song Exploder", "countries": ["gb", "us"], "why": "Artists take their own songs apart.", "site": "https://songexploder.net"},
    {"key": "vergecast", "cat": "tech", "title": "The Vergecast", "author": "The Verge", "artist": "The Verge", "term": "The Vergecast", "feed": "https://feeds.megaphone.fm/vergecast", "countries": ["gb", "us"], "why": "Weekly gadget and tech news.", "site": "https://www.theverge.com/the-vergecast"},
    {"key": "upgrade", "cat": "tech", "title": "Upgrade", "match": "Upgrade", "author": "Relay", "artist": "Relay", "term": "Upgrade Relay FM", "feed": "https://www.relay.fm/upgrade/feed", "countries": ["gb", "us"], "why": "Relaxed Apple and tech conversation.", "site": "https://www.relay.fm/upgrade"},
    {"key": "hardfork", "cat": "tech", "title": "Hard Fork", "author": "The New York Times", "artist": "New York Times", "term": "Hard Fork", "feed": "https://feeds.simplecast.com/l2i9YnTd", "countries": ["gb", "us"], "why": "AI and the week in tech.", "site": "https://www.nytimes.com/column/hard-fork"},
    {"key": "darknet", "cat": "tech", "title": "Darknet Diaries", "author": "Jack Rhysider", "artist": "Jack Rhysider", "term": "Darknet Diaries", "feed": "https://feeds.megaphone.fm/darknetdiaries", "countries": ["gb", "us"], "why": "True stories from the security world.", "site": "https://darknetdiaries.com"},
    {"key": "rebuild", "cat": "tech", "title": "Rebuild", "match": "Rebuild", "author": "Tatsuhiko Miyagawa", "artist": "Miyagawa", "term": "Rebuild.fm", "feed": "https://feeds.rebuild.fm/rebuildfm", "countries": ["jp", "us"], "lang": "ja", "why": "Long-running Japanese tech talk.", "site": "https://rebuild.fm"},
    {"key": "inourtime", "cat": "lit", "title": "In Our Time", "author": "BBC Radio 4", "artist": "BBC", "term": "In Our Time", "feed": "https://podcasts.files.bbci.co.uk/b006qykl.rss", "countries": ["gb"], "why": "Literature, science and history with academics.", "site": "https://www.bbc.co.uk/programmes/b006qykl"},
    {"key": "backlisted", "cat": "lit", "title": "Backlisted", "match": "Backlisted", "author": "Unbound", "term": "Backlisted podcast", "countries": ["gb", "us"], "why": "Forgotten and classic books, warmly discussed.", "site": "https://www.backlisted.fm"},
    {"key": "nyfiction", "cat": "lit", "title": "The New Yorker: Fiction", "author": "The New Yorker", "artist": "New Yorker", "term": "The New Yorker Fiction", "countries": ["us", "gb"], "why": "Authors read and discuss short stories.", "site": "https://www.newyorker.com/podcast/fiction"},
    {"key": "suiji", "cat": "lit", "title": "随机波动", "author": "随机波动", "term": "随机波动", "countries": ["cn", "tw", "hk", "us"], "lang": "zh", "why": "Culture, books and society, with scholars as guests.", "site": "https://www.douban.com/podcast/36726521/"},
    {"key": "yurugengo", "cat": "lit", "title": "ゆる言語学ラジオ", "author": "Yuru Gengogaku Radio", "term": "ゆる言語学ラジオ", "countries": ["jp"], "lang": "ja", "why": "Light, funny linguistics.", "site": "https://podnews.net/podcast/i9mkt"},
    {"key": "shedunnit", "cat": "mystery", "title": "Shedunnit", "author": "Caroline Crampton", "artist": "Caroline Crampton", "term": "Shedunnit", "countries": ["gb", "us"], "why": "The history of Golden Age detective fiction.", "site": "https://shedunnitshow.com"},
    {"key": "sherlock", "cat": "mystery", "title": "I Hear of Sherlock Everywhere", "author": "Scott Monty & Burt Wolder", "term": "I Hear of Sherlock Everywhere", "countries": ["us", "gb"], "why": "Sherlock Holmes, in depth.", "site": "https://www.ihearofsherlock.com"},
    {"key": "poirot", "cat": "mystery", "title": "波罗咖啡馆", "author": "机核", "term": "波罗咖啡馆", "countries": ["cn", "tw", "hk", "us"], "lang": "zh", "why": "Chinese mystery chat, including Japanese writers.", "site": "https://rephonic.com/podcasts/bo-luo-ka-pei-guan"},
    {"key": "planetmoney", "cat": "knowledge", "title": "Planet Money", "author": "NPR", "artist": "NPR", "term": "Planet Money", "feed": "https://feeds.npr.org/510289/podcast.xml", "countries": ["gb", "us"], "why": "Friendly stories about how money works.", "site": "https://www.npr.org/podcasts/510289/planet-money"},
    {"key": "indicator", "cat": "knowledge", "title": "The Indicator from Planet Money", "author": "NPR", "artist": "NPR", "term": "The Indicator from Planet Money", "feed": "https://feeds.npr.org/510325/podcast.xml", "countries": ["gb", "us"], "why": "Short daily economics.", "site": "https://www.npr.org/podcasts/510325/the-indicator-from-planet-money"},
    {"key": "econtalk", "cat": "knowledge", "title": "EconTalk", "author": "Russ Roberts", "term": "EconTalk", "countries": ["gb", "us"], "why": "Long, thoughtful conversations about economics.", "site": "https://www.econtalk.org"},
    {"key": "oddlots", "cat": "knowledge", "title": "Odd Lots", "match": "Odd Lots", "author": "Bloomberg", "artist": "Bloomberg", "term": "Odd Lots Bloomberg", "countries": ["gb", "us"], "why": "Markets and finance with a curious streak.", "site": "https://www.bloomberg.com/oddlots"},
    {"key": "mindscape", "cat": "knowledge", "title": "Sean Carroll’s Mindscape", "match": "Mindscape", "author": "Sean Carroll", "artist": "Sean Carroll", "term": "Sean Carroll Mindscape", "feed": "https://rss.art19.com/sean-carrolls-mindscape", "countries": ["gb", "us"], "why": "Physics and the big questions.", "site": "https://www.preposterousuniverse.com/podcast/"},
    {"key": "monkeycage", "cat": "knowledge", "title": "The Infinite Monkey Cage", "author": "BBC Radio 4", "artist": "BBC", "term": "The Infinite Monkey Cage", "feed": "https://podcasts.files.bbci.co.uk/b00snr0w.rss", "countries": ["gb"], "why": "Science with comedians.", "site": "https://www.bbc.co.uk/programmes/b00snr0w"},
    {"key": "radiolab", "cat": "knowledge", "title": "Radiolab", "author": "WNYC Studios", "artist": "WNYC", "term": "Radiolab", "countries": ["gb", "us"], "why": "Atmospheric science storytelling.", "site": "https://radiolab.org"},
    {"key": "retronauts", "cat": "games", "title": "Retronauts", "author": "Retronauts", "term": "Retronauts", "countries": ["gb", "us"], "why": "Retro games, with plenty of Japanese game history.", "site": "https://retronauts.com"},
    {"key": "vghh", "cat": "games", "title": "Video Game History Hour", "author": "Video Game History Foundation", "term": "Video Game History Hour", "countries": ["us", "gb"], "why": "Developers and historians, every other week.", "site": "https://gamehistory.org"}
  ],
  "extras": [
    {"key": "x-switchedonpop", "cat": "citypop", "title": "Switched On Pop", "artist": "Vox", "term": "Switched On Pop", "why": "How pop songs work, including a city pop episode.", "site": "https://switchedonpop.com"},
    {"key": "x-strongsongs", "cat": "citypop", "title": "Strong Songs", "match": "Strong Songs", "artist": "Kirk Hamilton", "term": "Strong Songs Kirk Hamilton", "why": "One song per episode, taken apart with love."},
    {"key": "x-anncast", "cat": "citypop", "title": "ANNCast", "match": "ANNCast", "term": "ANNCast Anime News Network", "why": "Anime industry talk, music included."},
    {"key": "x-questlove", "cat": "jazz", "title": "Questlove Supreme", "term": "Questlove Supreme", "why": "Music history with the people who made it."},
    {"key": "x-stickynotes", "cat": "jazz", "title": "Sticky Notes", "match": "Sticky Notes", "artist": "Joshua Weilerstein", "term": "Sticky Notes classical music podcast", "why": "A conductor explains classical pieces warmly."},
    {"key": "x-desertisland", "cat": "jazz", "title": "Desert Island Discs", "artist": "BBC", "term": "Desert Island Discs", "countries": ["gb"], "why": "Lives told through eight favourite records."},
    {"key": "x-waveform", "cat": "tech", "title": "Waveform: The MKBHD Podcast", "match": "Waveform", "term": "Waveform MKBHD", "why": "Gadget reviews and tech news with Marques Brownlee."},
    {"key": "x-atp", "cat": "tech", "title": "Accidental Tech Podcast", "term": "Accidental Tech Podcast", "why": "Three developers on Apple and technology.", "site": "https://atp.fm"},
    {"key": "x-decoder", "cat": "tech", "title": "Decoder with Nilay Patel", "match": "Decoder", "artist": "The Verge", "term": "Decoder with Nilay Patel", "why": "Big ideas in tech, from The Verge."},
    {"key": "x-bookreview", "cat": "lit", "title": "The Book Review", "match": "The Book Review", "artist": "New York Times", "term": "The Book Review New York Times", "why": "New books and the people who write them."},
    {"key": "x-lrb", "cat": "lit", "title": "LRB Podcast", "artist": "London Review of Books", "term": "LRB Podcast", "countries": ["gb"], "why": "Essays and books from the London Review of Books."},
    {"key": "x-ritan", "cat": "lit", "title": "日谈公园", "term": "日谈公园", "countries": ["cn", "tw", "hk", "us"], "lang": "zh", "why": "Chinese chat on anime, games, books and culture."},
    {"key": "x-sherlockco", "cat": "mystery", "title": "Sherlock & Co.", "match": "Sherlock & Co", "term": "Sherlock & Co.", "why": "Sherlock Holmes reimagined as an audio drama."},
    {"key": "x-magnus", "cat": "mystery", "title": "The Magnus Archives", "artist": "Rusty Quill", "term": "The Magnus Archives", "why": "A slow-burn horror mystery in audio."},
    {"key": "x-nightvale", "cat": "mystery", "title": "Welcome to Night Vale", "term": "Welcome to Night Vale", "why": "Strange, gentle mystery fiction.", "site": "https://www.welcometonightvale.com"},
    {"key": "x-freakonomics", "cat": "knowledge", "title": "Freakonomics Radio", "term": "Freakonomics Radio", "why": "The hidden side of everything, through economics.", "site": "https://freakonomics.com"},
    {"key": "x-startalk", "cat": "knowledge", "title": "StarTalk Radio", "artist": "Neil deGrasse Tyson", "term": "StarTalk Radio", "why": "Astrophysics with Neil deGrasse Tyson."},
    {"key": "x-coten", "cat": "knowledge", "title": "コテンラジオ", "match": "コテンラジオ", "term": "コテンラジオ", "countries": ["jp"], "lang": "ja", "why": "Japanese history told as long, lively stories."},
    {"key": "x-besties", "cat": "games", "title": "The Besties", "match": "The Besties", "term": "The Besties podcast", "why": "Friends talking games, warmly and funnily."},
    {"key": "x-insertcredit", "cat": "games", "title": "Insert Credit", "match": "Insert Credit", "term": "Insert Credit Show", "why": "Thoughtful game talk with industry veterans.", "site": "https://insertcredit.com"},
    {"key": "x-retrohour", "cat": "games", "title": "The Retro Hour", "term": "The Retro Hour", "why": "Interviews with the people who made classic games.", "site": "https://theretrohour.com"}
  ]
}
</script>

<script>
/* Afterhours interface translations. Keys are the English text; values are [简体中文, 日本語].
   Podcast titles, descriptions, captions and anything marked translate="no" are never changed. */
(() => {
'use strict';
const T = {
  // navigation and player
  'Listen Now': ['现在收听', '今すぐ聴く'], 'Discover': ['发现', '見つける'], 'Library': ['资料库', 'ライブラリ'], 'Search': ['搜索', '検索'],
  'Settings': ['设置', '設定'], 'Your shows': ['我的节目', 'マイ番組'], 'Main': ['主导航', 'メイン'], 'Sections': ['栏目', 'セクション'],
  'Search or paste a link': ['搜索或粘贴链接', '検索、またはリンクを貼り付け'],
  'Search podcasts, or paste a feed or Apple Podcasts link': ['搜索播客，或粘贴订阅源或 Apple 播客链接', 'ポッドキャストを検索、またはフィードか Apple Podcasts のリンクを貼り付け'],
  'Auto captions': ['自动字幕', '自動字幕'], 'Afterhours, Listen Now': ['Afterhours，现在收听', 'Afterhours、今すぐ聴く'],
  'Auto captions: caption episodes that have none, on this computer': ['自动字幕：在这台电脑上为没有字幕的单集生成字幕', '自動字幕：字幕のないエピソードに、このコンピュータで字幕を付けます'],
  'Player': ['播放器', 'プレーヤー'], 'Now Playing': ['正在播放', '再生中'], 'Open Now Playing': ['打开“正在播放”', '再生中の画面を開く'],
  'Back 15 seconds': ['后退 15 秒', '15秒戻る'], 'Forward 30 seconds': ['前进 30 秒', '30秒進む'],
  'Back 15 seconds (←)': ['后退 15 秒 (←)', '15秒戻る (←)'], 'Forward 30 seconds (→)': ['前进 30 秒 (→)', '30秒進む (→)'],
  'Play': ['播放', '再生'], 'Pause': ['暂停', '一時停止'], 'Play (Space)': ['播放（空格键）', '再生（スペース）'], 'Pause (Space)': ['暂停（空格键）', '一時停止（スペース）'],
  'Position in episode': ['播放位置', '再生位置'], 'Playback speed': ['播放速度', '再生速度'], 'Playback speed ([ and ])': ['播放速度（[ 和 ]）', '再生速度（[ と ]）'],
  'Captions': ['字幕', '字幕'], 'Captions (C)': ['字幕 (C)', '字幕 (C)'], 'Transcript': ['文字稿', '文字起こし'], 'Transcript (T)': ['文字稿 (T)', '文字起こし (T)'],
  'Sleep timer': ['睡眠定时', 'スリープタイマー'], 'Sleep timer, off': ['睡眠定时：关闭', 'スリープタイマー：オフ'],
  'Sleep timer, stops at the end of this episode': ['睡眠定时：本集结束时停止', 'スリープタイマー：このエピソードの終わりで停止'], 'Ep. end': ['集末', '終了時'],
  'Mute': ['静音', 'ミュート'], 'Unmute': ['取消静音', 'ミュート解除'], 'Mute (M)': ['静音 (M)', 'ミュート (M)'], 'Volume': ['音量', '音量'],
  'Close Now Playing': ['关闭“正在播放”', '再生中の画面を閉じる'], 'Play the next episode automatically': ['自动播放下一集', '次のエピソードを自動再生'],
  'Autoplay next episode': ['自动播放下一集', '次のエピソードを自動再生'], 'Previous episode': ['上一集', '前のエピソード'], 'Next episode': ['下一集', '次のエピソード'],
  'Previous episode (Shift+P)': ['上一集 (Shift+P)', '前のエピソード (Shift+P)'], 'Next episode (Shift+N)': ['下一集 (Shift+N)', '次のエピソード (Shift+N)'],
  'About this episode': ['关于本集', 'このエピソードについて'], 'No notes for this episode.': ['本集没有简介。', 'このエピソードには説明がありません。'],
  'Load a caption file': ['载入字幕文件', '字幕ファイルを読み込む'], 'Loading captions…': ['正在载入字幕…', '字幕を読み込み中…'], 'Reset': ['重置', 'リセット'], 'Topics': ['主题', 'トピック'],
  // captions
  'Getting ready to recognise speech…': ['正在准备语音识别…', '音声認識の準備中…'], 'Loading the speech model…': ['正在载入语音模型…', '音声モデルを読み込み中…'],
  'Auto captions, made on this computer': ['自动字幕，在这台电脑上生成', '自動字幕（このコンピュータで作成）'], 'Auto captions, partly done': ['自动字幕，部分完成', '自動字幕（一部完了）'],
  'Auto captions stopped': ['自动字幕已停止', '自動字幕を停止しました'], 'Caption recognition stopped.': ['字幕识别已停止。', '字幕の認識が停止しました。'],
  'Stop': ['停止', '停止'], 'Continue': ['继续', '続ける'], 'Redo with more accuracy': ['用更高准确度重做', 'より高い精度でやり直す'],
  'Recognising speech on this computer': ['正在这台电脑上识别语音', 'このコンピュータで音声を認識中'],
  'Captions appear here as they’re ready, starting from where you are in the episode.': ['字幕会从你当前收听的位置开始，生成后逐步显示在这里。', '字幕は今聴いている位置から、できた順にここに表示されます。'],
  'Caption any episode': ['为任意单集生成字幕', 'どのエピソードにも字幕を'],
  'This episode has no captions. Afterhours can recognise the speech on your computer and caption it, privately and for free. To turn this on, quit Afterhours and run this once:': ['本集没有字幕。Afterhours 可以在你的电脑上识别语音并生成字幕，私密且免费。要开启此功能，请退出 Afterhours 并运行一次：', 'このエピソードには字幕がありません。Afterhours はコンピュータ上で音声を認識して字幕を付けられます（プライベートで無料）。有効にするには、Afterhours を終了して次を一度実行してください：'],
  'Then start Afterhours again. If you’d rather not, switch auto captions off.': ['然后重新启动 Afterhours。如果不需要，可以关闭自动字幕。', 'その後 Afterhours をもう一度起動してください。不要なら自動字幕をオフにできます。'],
  'No captions for this episode': ['本集没有字幕', 'このエピソードには字幕がありません'], 'No captions for this episode.': ['本集没有字幕。', 'このエピソードには字幕がありません。'],
  'Auto captions are off, so episodes without captions aren’t captioned automatically.': ['自动字幕已关闭，没有字幕的单集不会自动生成字幕。', '自動字幕がオフのため、字幕のないエピソードには自動で字幕が付きません。'],
  'Caption this episode': ['为本集生成字幕', 'このエピソードに字幕を付ける'], 'Captions couldn’t be made': ['无法生成字幕', '字幕を作成できませんでした'], 'Try again': ['重试', 'もう一度試す'],
  'Captions stopped': ['字幕已停止', '字幕を停止しました'],
  'This transcript isn’t timed. Open the transcript to read it.': ['这份文字稿没有时间轴。请打开文字稿阅读。', 'この文字起こしにはタイミング情報がありません。文字起こしを開いて読んでください。'],
  'No captions for this episode. Open the transcript to turn on auto captions.': ['本集没有字幕。打开文字稿即可开启自动字幕。', 'このエピソードには字幕がありません。文字起こしを開くと自動字幕をオンにできます。'],
  'This episode’s captions couldn’t be loaded.': ['无法载入本集字幕。', 'このエピソードの字幕を読み込めませんでした。'],
  'Auto captions aren’t available on phones yet. Feed transcripts and caption files still work.': ['手机上暂不支持自动字幕。订阅源自带的文字稿和字幕文件仍可使用。', 'スマートフォンではまだ自動字幕を使えません。フィードの文字起こしと字幕ファイルは使えます。'],
  // Listen Now
  'Good morning': ['早上好', 'おはようございます'], 'Good afternoon': ['下午好', 'こんにちは'], 'Good evening': ['晚上好', 'こんばんは'], 'Up late': ['夜深了', '夜ふかしですね'],
  'Up Next': ['接着听', '次に聴く'], 'Pick up where you left off.': ['从上次停下的地方继续。', '前回の続きから。'], 'Latest episodes': ['最新单集', '最新エピソード'],
  'New from the shows on your shelf.': ['你节目架上的新单集。', 'あなたの番組の新着。'], 'Picked for you': ['为你精选', 'あなたへのおすすめ'],
  'Related shows that sit well next to your shelf.': ['与你的节目架相得益彰的相关节目。', 'あなたの棚に合う関連番組。'], 'See all': ['查看全部', 'すべて表示'],
  'Gathering the latest episodes…': ['正在收集最新单集…', '最新エピソードを集めています…'], 'No episodes yet. Open a show to load it.': ['还没有单集。打开一个节目即可载入。', 'まだエピソードがありません。番組を開くと読み込まれます。'],
  'Setting up your shelf': ['正在布置你的节目架', '棚を準備しています'],
  'Apple’s directory asked for a short pause. Carrying on in a moment…': ['Apple 的目录要求稍作暂停，马上继续…', 'Apple のディレクトリから少し待つよう求められました。まもなく再開します…'],
  // episodes and shows
  'Playing': ['播放中', '再生中'], 'Played': ['已播放', '再生済み'], 'Resume': ['继续播放', '続きから'], 'More': ['更多', 'もっと見る'], 'Less': ['收起', '閉じる'],
  'Captions available': ['有字幕', '字幕あり'], 'Today': ['今天', '今日'], 'Episodes': ['单集', 'エピソード'], 'Newest first': ['最新在前', '新しい順'], 'Oldest first': ['最早在前', '古い順'],
  'Latest episode': ['最新一集', '最新エピソード'], 'Check for new episodes': ['检查新单集', '新着を確認'], 'Change source': ['更换来源', 'ソースを変更'], 'Website': ['网站', 'ウェブサイト'],
  'Remove from library': ['从资料库移除', 'ライブラリから削除'], 'Add to Library': ['添加到资料库', 'ライブラリに追加'], 'Preview': ['预览', 'プレビュー'], 'Your additions': ['我添加的', '自分で追加'],
  'This show has no playable episodes.': ['这个节目没有可播放的单集。', 'この番組には再生できるエピソードがありません。'], 'Finding this show’s feed…': ['正在查找这个节目的订阅源…', 'この番組のフィードを探しています…'],
  'Still loading. If the feed doesn’t answer, Afterhours will look for episodes on the show’s website.': ['仍在载入。如果订阅源没有回应，Afterhours 会去节目的网站上查找单集。', '読み込み中です。フィードが応答しない場合は、番組のウェブサイトでエピソードを探します。'],
  'Afterhours couldn’t find this show’s feed in Apple’s directory or on its website.': ['Afterhours 在 Apple 目录和节目网站上都没找到这个节目的订阅源。', 'Apple のディレクトリでも番組のウェブサイトでも、この番組のフィードが見つかりませんでした。'],
  'or': ['或', 'または'], 'search for it': ['搜索它', '検索する'],
  'No working RSS feed, so these episodes come from the show’s website.': ['没有可用的 RSS 订阅源，所以这些单集来自节目的网站。', '使える RSS フィードがないため、エピソードは番組のウェブサイトから取得しています。'],
  'Paste the show’s RSS feed or website': ['粘贴节目的 RSS 订阅源或网站', '番組の RSS フィードかウェブサイトを貼り付け'], 'RSS feed or website address': ['RSS 订阅源或网站地址', 'RSS フィードまたはウェブサイトのアドレス'],
  'Use this source': ['使用这个来源', 'このソースを使う'],
  'Afterhours reads the RSS feed. If there isn’t one, it looks through the website for episodes.': ['Afterhours 会读取 RSS 订阅源；如果没有，就在网站上查找单集。', 'Afterhours は RSS フィードを読み込みます。ない場合はウェブサイトからエピソードを探します。'],
  'Checking that address. A website can take up to half a minute to look through…': ['正在检查这个地址。查找网站可能需要半分钟…', 'アドレスを確認中です。ウェブサイトの確認には30秒ほどかかることがあります…'],
  'This show is up to date.': ['这个节目已是最新。', 'この番組は最新です。'], 'This show isn’t in your library': ['这个节目不在你的资料库里', 'この番組はライブラリにありません'],
  'It may have been removed.': ['它可能已被移除。', '削除された可能性があります。'], 'Open Library': ['打开资料库', 'ライブラリを開く'],
  // Library and Discover
  'Paste a feed or Apple Podcasts link': ['粘贴订阅源或 Apple 播客链接', 'フィードか Apple Podcasts のリンクを貼り付け'], 'Add a show by link': ['通过链接添加节目', 'リンクから番組を追加'],
  'Add': ['添加', '追加'], 'Your library is empty': ['你的资料库是空的', 'ライブラリは空です'],
  'Find shows in Discover, or search by name.': ['去“发现”里找节目，或按名称搜索。', '「見つける」で番組を探すか、名前で検索してください。'], 'Open Discover': ['打开“发现”', '「見つける」を開く'],
  'Not found automatically': ['未能自动找到', '自動では見つかりませんでした'], 'Finding feed…': ['正在查找订阅源…', 'フィードを探しています…'],
  'City pop & anime songs': ['城市流行与动画歌曲', 'シティポップとアニソン'], 'Jazz, hip-hop & piano': ['爵士、嘻哈与钢琴', 'ジャズ、ヒップホップ、ピアノ'],
  'Gadgets & latest tech': ['数码与前沿科技', 'ガジェットと最新テック'], 'Literature & light lit': ['文学与轻文学', '文学とライト文芸'], 'Mystery novels': ['推理小说', 'ミステリー小説'],
  'Economics, physics & astronomy': ['经济、物理与天文', '経済・物理・天文'], 'Game industry': ['游戏行业', 'ゲーム業界'],
  'city pop & anime songs': ['城市流行与动画歌曲', 'シティポップとアニソン'], 'jazz, hip-hop & piano': ['爵士、嘻哈与钢琴', 'ジャズ、ヒップホップ、ピアノ'],
  'gadgets & latest tech': ['数码与前沿科技', 'ガジェットと最新テック'], 'literature & light lit': ['文学与轻文学', '文学とライト文芸'], 'mystery novels': ['推理小说', 'ミステリー小説'],
  'economics, physics & astronomy': ['经济、物理与天文', '経済・物理・天文'], 'game industry': ['游戏行业', 'ゲーム業界'],
  'City pop': ['城市流行', 'シティポップ'], 'Jazz & piano': ['爵士与钢琴', 'ジャズとピアノ'], 'Tech': ['科技', 'テック'], 'Books': ['书籍', '本'], 'Mystery': ['推理', 'ミステリー'],
  'Knowledge': ['知识', '知識'], 'Games': ['游戏', 'ゲーム'], 'city pop': ['城市流行', 'シティポップ'], 'jazz & piano': ['爵士与钢琴', 'ジャズとピアノ'], 'tech': ['科技', 'テック'],
  'books': ['书籍', '本'], 'mystery': ['推理', 'ミステリー'], 'knowledge': ['知识', '知識'], 'games': ['游戏', 'ゲーム'],
  'Related shows, picked around what you love.': ['围绕你的喜好挑选的相关节目。', 'あなたの好みに合わせて選んだ関連番組。'], 'For you': ['为你推荐', 'おすすめ'],
  'Made for your evenings': ['为你的夜晚而选', 'あなたの夜のために'], 'City pop, slow jazz, good books and big ideas': ['城市流行、慢爵士、好书与大问题', 'シティポップ、スロージャズ、良い本、大きなアイデア'],
  'Related shows for everything on your shelf. Press + to add one, or open it to preview its episodes first.': ['为你节目架上的每个节目找来相关节目。按 + 添加，或先打开预览单集。', '棚の番組すべてに関連する番組です。＋で追加、または開いてエピソードをプレビューできます。'],
  'From the interests you told me about.': ['根据你告诉我的兴趣。', '教えてもらった興味から。'], 'Hand-picked to sit next to your shelf.': ['精心挑选，与你的节目架相配。', 'あなたの棚に合わせて厳選。'],
  'Already on your shelf': ['已在你的节目架上', 'すでに棚にあります'], 'Japanese picks': ['日语精选', '日本語のおすすめ'], 'Chinese picks': ['中文精选', '中国語のおすすめ'],
  'From your poster': ['来自你的海报', 'ポスターから'], 'Shows you removed from your library.': ['你从资料库中移除的节目。', 'ライブラリから削除した番組。'],
  'Nothing new here right now.': ['这里暂时没有新内容。', '今は新しいものがありません。'], 'In your library': ['已在资料库中', 'ライブラリにあります'], 'Add to library': ['添加到资料库', 'ライブラリに追加'],
  'Shows, topics, or a feed link': ['节目、主题或订阅源链接', '番組、トピック、またはフィードのリンク'], 'Search podcasts': ['搜索播客', 'ポッドキャストを検索'], 'Try a topic': ['试试这些主题', 'トピックを試す'],
  'No shows found. Try fewer words, or paste the feed link.': ['没有找到节目。试试少用几个词，或粘贴订阅源链接。', '番組が見つかりませんでした。言葉を減らすか、フィードのリンクを貼り付けてください。'],
  // Settings
  'Afterhours runs on this computer. Your library and settings stay in this browser.': ['Afterhours 在这台电脑上运行。你的资料库和设置保存在此浏览器中。', 'Afterhours はこのコンピュータで動作します。ライブラリと設定はこのブラウザに保存されます。'],
  'Language': ['语言', '言語'], 'System': ['跟随系统', 'システム'],
  'Choose the language for Afterhours. Podcast titles and descriptions stay as their publishers wrote them.': ['选择 Afterhours 的界面语言。播客标题和简介保持发布者的原文。', 'Afterhours の表示言語を選びます。番組名や説明は配信者が書いたままです。'],
  'Show captions': ['显示字幕', '字幕を表示'], 'The current line above the player and in Now Playing.': ['在播放器上方和“正在播放”中显示当前这一句。', 'プレーヤーの上と再生中の画面に、今の一行を表示します。'],
  'When an episode has no captions, recognise the speech on this computer and caption it.': ['单集没有字幕时，在这台电脑上识别语音并生成字幕。', '字幕のないエピソードは、このコンピュータで音声を認識して字幕を付けます。'],
  'Recognition quality': ['识别质量', '認識の品質'],
  'Captions come from OpenAI’s Whisper speech models, running on this computer. Fast uses Whisper base, Accurate uses Whisper small and Most accurate uses Whisper medium. Automatic picks base for English shows and small for Japanese and Chinese ones.': ['字幕由在这台电脑上运行的 OpenAI Whisper 语音模型生成。“快速”使用 Whisper base，“准确”使用 Whisper small，“最准确”使用 Whisper medium。“自动”会为英语节目选择 base，为日语和中文节目选择 small。', '字幕は、このコンピュータで動く OpenAI の Whisper 音声モデルが作ります。「高速」は Whisper base、「正確」は Whisper small、「最も正確」は Whisper medium を使います。「自動」は英語の番組に base、日本語と中国語の番組に small を選びます。'],
  'Automatic': ['自动', '自動'], 'Fast': ['快速', '高速'], 'Accurate': ['准确', '正確'], 'Most accurate': ['最准确', '最も正確'],
  'Caption recognition': ['字幕识别', '字幕の認識'], 'Checking…': ['正在检查…', '確認中…'], 'Ready.': ['已就绪。', '準備完了。'],
  'Not set up yet. Quit Afterhours, run this once, then start it again:': ['尚未设置。请退出 Afterhours，运行一次以下命令，然后重新启动：', 'まだ設定されていません。Afterhours を終了し、次を一度実行してから再起動してください：'],
  'Afterhours’ helper isn’t answering. Is afterhours.py still running?': ['Afterhours 的后台程序没有响应。afterhours.py 还在运行吗？', 'Afterhours のヘルパーが応答しません。afterhours.py は動いていますか？'],
  'The player’s helper isn’t answering. Is afterhours.py still running?': ['Afterhours 的后台程序没有响应。afterhours.py 还在运行吗？', 'Afterhours のヘルパーが応答しません。afterhours.py は動いていますか？'],
  'Saved auto captions': ['已保存的自动字幕', '保存済みの自動字幕'],
  'Captions made on this computer are kept, so they load instantly next time.': ['在这台电脑上生成的字幕会被保存，下次可立即载入。', 'このコンピュータで作った字幕は保存され、次回はすぐ読み込まれます。'],
  'Delete': ['删除', '削除'], 'Playback': ['播放', '再生'], 'Appearance': ['外观', '外観'],
  'Follow your system, or always use light or dark.': ['跟随系统，或始终使用浅色或深色。', 'システムに合わせるか、常にライトまたはダークを使います。'], 'Light': ['浅色', 'ライト'], 'Dark': ['深色', 'ダーク'],
  'Animations': ['动画效果', 'アニメーション'],
  'Fluent-style motion: pages glide in, artwork flies into place, and controls respond as you press them.': ['Fluent 风格的动效：页面滑入、封面飞入到位、控件随按压回应。', 'Fluent スタイルの動き：ページがすべり込み、アートワークが飛んで収まり、押すと操作が反応します。'],
  'Mouse halo': ['鼠标光晕', 'マウスの光彩'],
  'The soft glow that follows your mouse over lists, menus and buttons.': ['鼠标移过列表、菜单和按钮时跟随的柔和光晕。', 'リストやメニュー、ボタンの上でマウスを追うやわらかな光。'],
  'Look for missing shows again': ['重新查找缺失的节目', '見つからない番組をもう一度探す'], 'Every show on your shelf was found.': ['你节目架上的每个节目都已找到。', '棚の番組はすべて見つかりました。'],
  'Look again': ['重新查找', 'もう一度探す'], 'Import OPML': ['导入 OPML', 'OPML を読み込む'], 'Add shows from another podcast app.': ['从其他播客应用添加节目。', 'ほかのポッドキャストアプリから番組を追加します。'],
  'Import…': ['导入…', '読み込む…'], 'Export OPML': ['导出 OPML', 'OPML を書き出す'], 'Save your shows for another podcast app.': ['保存你的节目，供其他播客应用使用。', '番組をほかのアプリ用に保存します。'],
  'Export': ['导出', '書き出す'], 'About': ['关于', 'このアプリについて'], 'Version': ['版本', 'バージョン'], 'Quit Afterhours': ['退出 Afterhours', 'Afterhours を終了'],
  'Stops Afterhours on this computer. Open it again whenever you want to listen.': ['在这台电脑上停止 Afterhours。想听时再打开即可。', 'このコンピュータで Afterhours を停止します。聴きたいときにまた開いてください。'],
  'Quit': ['退出', '終了'], 'Afterhours has stopped': ['Afterhours 已停止', 'Afterhours を停止しました'],
  'You can close this tab. To listen again, open Afterhours.': ['你可以关闭这个页面。要再次收听，请打开 Afterhours。', 'この画面は閉じて構いません。また聴くときは Afterhours を開いてください。'],
  'Window': ['窗口', 'ウィンドウ'], 'Open in its own window': ['在独立窗口中打开', '専用ウィンドウで開く'],
  'Afterhours opens like an app, without tabs or an address bar. Closing the window quits it. Takes effect the next time you open Afterhours.': ['Afterhours 会像应用一样打开，没有标签页和地址栏；关闭窗口即退出。下次打开 Afterhours 时生效。', 'タブやアドレスバーのないアプリとして開き、ウィンドウを閉じると終了します。次に Afterhours を開いたときから有効です。'],
  'Uses a native window.': ['使用原生窗口。', 'ネイティブのウィンドウを使います。'], 'your browser': ['你的浏览器', 'お使いのブラウザ'],
  'Needs Microsoft Edge, Google Chrome or Chromium. Without one, Afterhours opens in your browser.': ['需要 Microsoft Edge、Google Chrome 或 Chromium。没有的话，Afterhours 会在浏览器中打开。', 'Microsoft Edge、Google Chrome、Chromium のいずれかが必要です。ない場合はブラウザで開きます。'],
  'Phone': ['手机', 'スマートフォン'], 'Use on your phone': ['在手机上使用', 'スマートフォンで使う'],
  'Let phones on the same Wi-Fi use Afterhours from this computer, with a private link.': ['让同一 Wi-Fi 下的手机通过私密链接使用这台电脑上的 Afterhours。', '同じ Wi-Fi のスマートフォンから、専用リンクでこのコンピュータの Afterhours を使えるようにします。'],
  'Open this link on your phone, then add it to your home screen:': ['在手机上打开这个链接，然后添加到主屏幕：', 'スマートフォンでこのリンクを開き、ホーム画面に追加してください：'],
  'Copy link': ['复制链接', 'リンクをコピー'], 'New link': ['新链接', '新しいリンク'], 'Link copied.': ['链接已复制。', 'リンクをコピーしました。'],
  'Phone access on. Keep this computer awake while you listen.': ['已开启手机访问。收听时请让这台电脑保持唤醒。', 'スマートフォンからの利用をオンにしました。聴いている間はこのコンピュータをスリープさせないでください。'],
  'Phone access off.': ['已关闭手机访问。', 'スマートフォンからの利用をオフにしました。'],
  'The old link no longer works. Use the new one on your phones.': ['旧链接已失效，请在手机上使用新链接。', '古いリンクは使えなくなりました。新しいリンクを使ってください。'],
  'This computer doesn’t seem to be on a network your phone can reach.': ['这台电脑似乎不在手机可以访问的网络上。', 'このコンピュータはスマートフォンからつながるネットワークにないようです。'],
  'Only the computer running Afterhours can change that.': ['只有运行 Afterhours 的电脑才能更改此项。', 'これは Afterhours を動かしているコンピュータでのみ変更できます。'],
  // pro mode
  'Pro mode': ['专业模式', 'プロモード'], 'Fine-tune the speech recognition: model, language, hardware and decoding.': ['精细调整语音识别：模型、语言、硬件与解码。', '音声認識を細かく調整します：モデル、言語、ハードウェア、デコード。'],
  'Model': ['模型', 'モデル'], 'Hardware': ['硬件', 'ハードウェア'], 'Decoding': ['解码', 'デコード'], 'Silence and filtering': ['静音与过滤', '無音とフィルター'],
  'Caption lines': ['字幕行', '字幕の行'], 'Vocabulary': ['词汇', '語彙'],
  'Bigger models are more accurate and slower. “.en” models are English-only. large-v3-turbo needs faster-whisper 1.1 or later.': ['模型越大越准确，也越慢。“.en”模型只支持英语。large-v3-turbo 需要 faster-whisper 1.1 或更新版本。', '大きいモデルほど正確ですが遅くなります。「.en」モデルは英語専用です。large-v3-turbo には faster-whisper 1.1 以降が必要です。'],
  'Spoken language': ['口语语言', '話されている言語'], 'Automatic detects it from the episode.': ['“自动”会从单集中检测。', '「自動」はエピソードから判定します。'],
  'Translate into English': ['翻译成英语', '英語に翻訳'], 'Write the captions in English, whatever language is spoken.': ['无论说的是什么语言，字幕都用英语写出。', '話されている言語にかかわらず、字幕を英語で書きます。'],
  'Run on': ['运行于', '実行する場所'], 'Automatic uses a supported NVIDIA graphics card when there is one.': ['“自动”会在有兼容的 NVIDIA 显卡时使用它。', '「自動」は対応する NVIDIA のグラフィックカードがあれば使います。'],
  'Processor': ['处理器', 'プロセッサ'], 'Graphics card': ['显卡', 'グラフィックカード'], 'Precision': ['精度', '精度'],
  'Lower precision is faster and uses less memory.': ['精度越低越快，占用内存也越少。', '精度を下げると速く、メモリも少なくて済みます。'],
  'Processor threads': ['处理器线程', 'プロセッサのスレッド数'], '0 lets Afterhours decide.': ['0 表示由 Afterhours 决定。', '0 にすると Afterhours が決めます。'],
  'Beam size': ['束搜索宽度', 'ビームサイズ'], 'How many guesses Whisper weighs at once. 1 is fastest; 5 is more careful.': ['Whisper 同时权衡的候选数量。1 最快；5 更仔细。', 'Whisper が同時に比べる候補の数。1 が最速、5 はより慎重です。'],
  'Temperature': ['温度', '温度'], 'Above 0 adds variety. Keep it at 0 for steady results.': ['大于 0 会增加变化。保持 0 可获得稳定结果。', '0 より大きいと多様になります。安定させるなら 0 のままに。'],
  'Use previous text': ['利用前文', '直前の文を使う'], 'Lets Whisper use what it just heard as context. Smoother, but it can repeat itself.': ['让 Whisper 把刚听到的内容作为上下文。更流畅，但可能重复。', '直前に聞いた内容を文脈として使います。なめらかになりますが、繰り返すことがあります。'],
  'Skip silence': ['跳过静音', '無音をスキップ'], 'Finds speech first, so music and silence are skipped.': ['先检测语音，跳过音乐和静音。', '先に音声を見つけ、音楽や無音を飛ばします。'],
  'Speech threshold': ['语音阈值', '音声のしきい値'], 'Higher means only clear speech counts.': ['越高，只有清晰的语音才算数。', '高いほど、はっきりした声だけを音声とみなします。'],
  'Shortest silence (ms)': ['最短静音（毫秒）', '最短の無音（ミリ秒）'], 'Pauses shorter than this don’t split speech.': ['短于此的停顿不会切分语音。', 'これより短い間では音声を区切りません。'],
  'No-speech threshold': ['无语音阈值', '無音判定のしきい値'], 'How sure Whisper must be that a stretch has no speech before skipping it.': ['Whisper 判定一段没有语音并跳过之前需要的把握程度。', '区間に音声がないと判断して飛ばすための確信度。'],
  'Repetition limit': ['重复上限', '繰り返しの上限'], 'Lines that compress more than this are treated as mistakes and retried.': ['压缩率高于此值的句子会被视为错误并重试。', 'これより圧縮率が高い行は誤りとみなして再試行します。'],
  'Confidence floor': ['置信度下限', '確信度の下限'], 'Lines less confident than this are retried.': ['置信度低于此值的句子会重试。', 'これより確信度の低い行は再試行します。'],
  'Word timing': ['逐词时间', '単語ごとのタイミング'], 'Splits captions into short lines using the timing of each word.': ['利用每个词的时间把字幕切成短句。', '単語ごとのタイミングで字幕を短い行に分けます。'],
  'Longest line (characters)': ['最长一行（字符）', '1行の最大文字数'], 'Longest line (seconds)': ['最长一行（秒）', '1行の最大秒数'],
  'Vocabulary hint': ['词汇提示', '語彙のヒント'], 'Names and words to expect, such as hosts, guests or places.': ['可能出现的名字和词，比如主持人、嘉宾或地名。', '出てきそうな名前や言葉（ホスト、ゲスト、地名など）。'],
  'e.g. Agatha Christie, Hercule Poirot': ['例如：阿加莎·克里斯蒂、赫尔克里·波洛', '例：アガサ・クリスティー、エルキュール・ポアロ'],
  'Hotwords': ['热词', 'ホットワード'], 'Words Whisper should listen out for. Needs faster-whisper 1.0 or later.': ['让 Whisper 特别留意的词。需要 faster-whisper 1.0 或更新版本。', 'Whisper に特に注意して聞き取ってほしい言葉。faster-whisper 1.0 以降が必要です。'],
  'Re-caption this episode': ['重新生成本集字幕', 'このエピソードの字幕を作り直す'], 'Reset to defaults': ['恢复默认', '初期設定に戻す'],
  'Pro settings reset.': ['已恢复专业模式的默认设置。', 'プロモードの設定を初期状態に戻しました。'], 'Start an episode first.': ['请先开始播放一集。', '先にエピソードを再生してください。'],
  'Pro settings apply to the next captions Afterhours makes.': ['专业设置会用于 Afterhours 接下来生成的字幕。', 'プロモードの設定は、次に作る字幕から使われます。'],
  // toasts and messages
  'Captions on.': ['字幕已开启。', '字幕をオンにしました。'], 'Captions off.': ['字幕已关闭。', '字幕をオフにしました。'], 'Sleep timer off.': ['睡眠定时已关闭。', 'スリープタイマーをオフにしました。'],
  'Sleep timer: at the end of this episode.': ['睡眠定时：本集结束时。', 'スリープタイマー：このエピソードの終わり。'],
  'Sleep timer paused playback. Good night.': ['睡眠定时已暂停播放。晚安。', 'スリープタイマーで一時停止しました。おやすみなさい。'],
  'Sleep timer stopped at the end of the episode. Good night.': ['睡眠定时在本集结束时停止。晚安。', 'エピソードの終わりで停止しました。おやすみなさい。'],
  'The next episode will play automatically.': ['将自动播放下一集。', '次のエピソードを自動再生します。'], 'Autoplay off.': ['已关闭自动播放。', '自動再生をオフにしました。'],
  'Animations on.': ['动画效果已开启。', 'アニメーションをオンにしました。'], 'Animations off.': ['动画效果已关闭。', 'アニメーションをオフにしました。'],
  'Mouse halo on.': ['鼠标光晕已开启。', 'マウスの光彩をオンにしました。'], 'Mouse halo off.': ['鼠标光晕已关闭。', 'マウスの光彩をオフにしました。'],
  'Auto captions off. Episodes without captions won’t be captioned automatically.': ['自动字幕已关闭。没有字幕的单集不会自动生成字幕。', '自動字幕をオフにしました。字幕のないエピソードに自動で字幕は付きません。'],
  'Auto captions on. Episodes without captions will be captioned on this computer.': ['自动字幕已开启。没有字幕的单集会在这台电脑上生成字幕。', '自動字幕をオンにしました。字幕のないエピソードはこのコンピュータで字幕が付きます。'],
  'Press play to start listening.': ['按播放开始收听。', '再生を押して聴き始めましょう。'], 'This episode couldn’t start. Try again in a moment.': ['本集无法开始播放，请稍后再试。', 'このエピソードを開始できませんでした。少し待ってからもう一度お試しください。'],
  'This episode’s audio couldn’t be played. The link may have changed, so try refreshing the show.': ['无法播放本集音频。链接可能已变更，请尝试刷新节目。', 'このエピソードの音声を再生できませんでした。リンクが変わった可能性があるので、番組を更新してみてください。'],
  'That’s the last episode in this list.': ['这是列表中的最后一集。', 'このリストの最後のエピソードです。'], 'That’s the first episode in this list.': ['这是列表中的第一集。', 'このリストの最初のエピソードです。'],
  'Loading that link…': ['正在载入链接…', 'リンクを読み込み中…'], 'That isn’t a feed, so Afterhours is looking through the page for episodes…': ['这不是订阅源，Afterhours 正在页面中查找单集…', 'フィードではないので、ページからエピソードを探しています…'],
  'There’s nothing to export yet.': ['还没有可导出的内容。', '書き出すものがまだありません。'], 'That file isn’t valid OPML.': ['这不是有效的 OPML 文件。', '有効な OPML ファイルではありません。'],
  'No new shows in that file.': ['这个文件中没有新节目。', 'このファイルに新しい番組はありません。'], 'That file has no timed captions I can read.': ['这个文件里没有可读取的带时间字幕。', 'このファイルには読み取れる字幕がありません。'],
  'Start an episode first, then load its caption file.': ['请先开始播放一集，再载入它的字幕文件。', '先にエピソードを再生してから字幕ファイルを読み込んでください。'],
  'Saved auto captions deleted.': ['已删除保存的自动字幕。', '保存済みの自動字幕を削除しました。'], 'Nothing is missing.': ['没有缺失的节目。', '見つからない番組はありません。'],
  'Delete the auto captions saved on this computer? They’ll be made again when you next play those episodes.': ['要删除这台电脑上保存的自动字幕吗？下次播放这些单集时会重新生成。', 'このコンピュータに保存した自動字幕を削除しますか？次にそのエピソードを再生するときにもう一度作られます。'],
  'Quit Afterhours? Playback stops, and you can open Afterhours again any time.': ['要退出 Afterhours 吗？播放会停止，你可以随时再打开。', 'Afterhours を終了しますか？再生は止まりますが、いつでもまた開けます。'],
  'The site took too long to answer.': ['网站响应超时。', 'サイトの応答に時間がかかりすぎました。'], 'That link doesn’t look like a podcast feed.': ['这个链接看起来不是播客订阅源。', 'このリンクはポッドキャストのフィードではないようです。'],
  'No episodes were found at that address.': ['在这个地址没有找到单集。', 'このアドレスでエピソードが見つかりませんでした。'], 'This feed has no playable episodes.': ['这个订阅源没有可播放的单集。', 'このフィードには再生できるエピソードがありません。'],
  'Apple’s podcast directory asked Afterhours to slow down. Try again in a minute.': ['Apple 播客目录要求 Afterhours 放慢速度，请一分钟后再试。', 'Apple のポッドキャストディレクトリから速度を落とすよう求められました。1分後にもう一度お試しください。'],
  'Apple doesn’t list a public feed for that show.': ['Apple 没有列出这个节目的公开订阅源。', 'Apple にはこの番組の公開フィードがありません。'],
  'Moving your library': ['正在搬移你的资料库', 'ライブラリを移動しています'],
  'Sending your shows, progress and settings to the Afterhours window…': ['正在把你的节目、进度和设置发送到 Afterhours 窗口…', '番組、再生位置、設定を Afterhours のウィンドウに送っています…'],
  'Done. Your library is in the Afterhours window now, so you can close this tab.': ['完成。你的资料库已经在 Afterhours 窗口里了，可以关闭这个标签页。', '完了しました。ライブラリは Afterhours のウィンドウに移りました。このタブは閉じて構いません。'],
  'Used Afterhours in your browser before?': ['以前在浏览器里用过 Afterhours？', '以前ブラウザで Afterhours を使っていましたか？'],
  'Bring your shows, listening progress and settings into this window. Your browser opens for a moment to hand them over.': ['把你的节目、收听进度和设置带到这个窗口。浏览器会短暂打开来转交它们。', '番組、再生位置、設定をこのウィンドウに移せます。受け渡しのため、ブラウザが少しだけ開きます。'],
  'Bring them over': ['搬过来', '移動する'], 'Not now': ['以后再说', '今はしない'],
  'Opening your browser to collect your library…': ['正在打开浏览器以取回你的资料库…', 'ライブラリを受け取るためにブラウザを開いています…'],
  'Your library is here.': ['你的资料库已经搬过来了。', 'ライブラリを移動しました。'],
  'Nothing arrived from your browser. Try again, or use Export and Import OPML.': ['没有收到浏览器发来的内容。请重试，或使用导出和导入 OPML。', 'ブラウザから何も届きませんでした。もう一度試すか、OPML の書き出しと読み込みを使ってください。'],
  'Bring over your library': ['搬入资料库', 'ライブラリを移す'],
  'Copy your shows, progress and settings from the browser you used before.': ['从你以前使用的浏览器复制节目、进度和设置。', '以前使っていたブラウザから番組、再生位置、設定をコピーします。'],
  'Bring over': ['搬入', '移す'],
  'That’s too much to move at once.': ['一次要搬的内容太多了。', '一度に移すには多すぎます。'], 'That isn’t a library.': ['这不是资料库。', 'これはライブラリではありません。'],
  'Afterhours is its own app here, using the web view built into your system. No browser is involved, and closing the window quits Afterhours.': ['Afterhours 在这里作为独立应用运行，使用系统内置的网页视图，不经过任何浏览器；关闭窗口即退出 Afterhours。', 'Afterhours はここでは単独のアプリとして、システム内蔵の Web ビューで動いています。ブラウザは使わず、ウィンドウを閉じると終了します。'],
  'The window component is installed. Switch now, or Afterhours opens in its own window the next time you start it.': ['窗口组件已安装。可以现在切换，或者下次启动时 Afterhours 会在独立窗口中打开。', 'ウィンドウ部品はインストール済みです。今すぐ切り替えるか、次回の起動時から専用ウィンドウで開きます。'],
  'Switch now': ['现在切换', '今すぐ切り替える'], 'Install': ['安装', 'インストール'], 'Set it up': ['开始设置', '設定する'],
  'Installing the window component…': ['正在安装窗口组件…', 'ウィンドウ部品をインストールしています…'],
  'Making a private Python environment for Afterhours…': ['正在为 Afterhours 创建专用的 Python 环境…', 'Afterhours 専用の Python 環境を作っています…'],
  'Afterhours can run as its own app instead of in your browser, using the web view built into your system. It needs a small window component, installed once.': ['Afterhours 可以使用系统内置的网页视图，作为独立应用运行，而不是在浏览器里。这需要安装一次小小的窗口组件。', 'Afterhours はブラウザではなく、システム内蔵の Web ビューを使った単独のアプリとして動かせます。小さなウィンドウ部品を一度だけインストールします。'],
  'The window component couldn’t be installed. Check your internet connection, then try again.': ['无法安装窗口组件。请检查网络连接后重试。', 'ウィンドウ部品をインストールできませんでした。インターネット接続を確認して、もう一度お試しください。'],
  'Couldn’t make a private Python environment. Run: python3 afterhours.py --setup-window': ['无法创建专用的 Python 环境。请运行：python3 afterhours.py --setup-window', '専用の Python 環境を作れませんでした。次を実行してください：python3 afterhours.py --setup-window'],
  'The window component isn’t installed yet.': ['窗口组件还没有安装。', 'ウィンドウ部品はまだインストールされていません。'],
  'Afterhours is opening in its own window': ['Afterhours 正在独立窗口中打开', 'Afterhours を専用ウィンドウで開いています'],
  'Your library goes with it. You can close this tab.': ['你的资料库也会一起带过去。你可以关闭这个标签页。', 'ライブラリも一緒に移ります。このタブは閉じて構いません。'],
  'Make Afterhours its own app': ['让 Afterhours 成为独立应用', 'Afterhours を単独のアプリに'],
  'Run it in its own window, without a browser. Afterhours installs a small window component once.': ['在独立窗口中运行，无需浏览器。Afterhours 只需安装一次小小的窗口组件。', 'ブラウザを使わず専用ウィンドウで動かせます。小さなウィンドウ部品を一度だけインストールします。'],
  'Afterhours runs on this computer, and your library and settings stay on it.': ['Afterhours 在这台电脑上运行，你的资料库和设置都保存在这里。', 'Afterhours はこのコンピュータで動作し、ライブラリと設定もここに保存されます。']
};
// Sentences with names or numbers in them. $1, $2… are filled in (and translated where possible).
const P = [
  [/^(Good morning|Good afternoon|Good evening|Up late)\. Here’s what’s new on your shelf\.$/, '$1。这是你的节目架上的新内容。', '$1。あなたの棚の新着です。'],
  [/^Finding the (\d+) shows from your poster\. (\d+) of (\d+) ready\. Apple’s directory is queried gently, so this takes about a minute the first time\.$/, '正在查找海报上的 $1 个节目，已就绪 $2/$3。为了不打扰 Apple 的目录，首次大约需要一分钟。', 'ポスターの $1 番組を探しています（$2/$3 完了）。Apple のディレクトリに負担をかけないよう、初回は1分ほどかかります。'],
  [/^Checking (\d+) shows for new episodes…$/, '正在检查 $1 个节目的新单集…', '$1 番組の新着を確認中…'],
  [/^Checking for new episodes… (\d+) of (\d+)$/, '正在检查新单集… $1/$2', '新着を確認中… $1/$2'],
  [/^(\d+) of (\d+)$/, '$1/$2', '$1/$2'],
  [/^Downloading the episode for captions… (\d+)%$/, '正在下载本集用于生成字幕… $1%', '字幕作成のためにエピソードをダウンロード中… $1%'],
  [/^Loading the (.+) speech model\. The first time, it downloads once \(about (.+)\)\.$/, '正在载入 $1 语音模型。首次使用时会下载一次（约 $2）。', '$1 音声モデルを読み込み中です。初回のみダウンロードします（約 $2）。'],
  [/^Auto captions: recognising speech, (\d+)% done$/, '自动字幕：正在识别语音，已完成 $1%', '自動字幕：音声を認識中（$1% 完了）'],
  [/^Scroll (.+) left$/, '向左滚动“$1”', '「$1」を左へスクロール'],
  [/^Scroll (.+) right$/, '向右滚动“$1”', '「$1」を右へスクロール'],
  [/^(\d.*) left$/, '剩余 $1', '残り $1'],
  [/^Sleep timer, (\d+) minutes left$/, '睡眠定时：还剩 $1 分钟', 'スリープタイマー：残り$1分'],
  [/^(\d+) min$/, '$1 分钟', '$1分'],
  [/^Episode (\d+)$/, '第 $1 集', '第$1回'],
  [/^Show more episodes \((\d+)\)$/, '显示更多单集（$1）', 'さらにエピソードを表示（$1）'],
  [/^Play (.+) again$/, '再次播放“$1”', '「$1」をもう一度再生'],
  [/^Resume (.+)$/, '继续播放“$1”', '「$1」を続きから再生'],
  [/^Pause (.+)$/, '暂停“$1”', '「$1」を一時停止'],
  [/^Play (.+)$/, '播放“$1”', '「$1」を再生'],
  [/^Couldn’t load episodes\. (.+)$/, '无法载入单集。$1', 'エピソードを読み込めませんでした。$1'],
  [/^Couldn’t refresh\. (.+)$/, '无法刷新。$1', '更新できませんでした。$1'],
  [/^(.+) now has (\d+) episodes\.$/, '“$1”现在有 $2 集。', '「$1」は $2 エピソードになりました。'],
  [/^(\d+) shows$/, '$1 个节目', '$1 番組'],
  [/^Because you listen to (.+)$/, '因为你在听“$1”', '「$1」を聴いているあなたへ'],
  [/^Because you like (.+)$/, '因为你喜欢$1', '$1が好きなあなたへ'],
  [/^More about (.+)\.$/, '更多关于“$1”的节目。', '「$1」についてもっと。'],
  [/^More (.+)$/, '更多$1', 'もっと：$1'],
  [/^Shows about “(.+)”$/, '关于“$1”的节目', '「$1」の番組'],
  [/^Results for “(.+)”$/, '“$1”的搜索结果', '「$1」の検索結果'],
  [/^Open (.+)$/, '打开“$1”', '「$1」を開く'],
  [/^Add (.+) to your library$/, '将“$1”添加到资料库', '「$1」をライブラリに追加'],
  [/^(.+) is in your library$/, '“$1”已在资料库中', '「$1」はライブラリにあります'],
  [/^Couldn’t find a public feed for (.+)\.$/, '找不到“$1”的公开订阅源。', '「$1」の公開フィードが見つかりませんでした。'],
  [/^(\d+) shows? couldn’t be found automatically\.$/, '有 $1 个节目未能自动找到。', '$1 番組が自動では見つかりませんでした。'],
  [/^Looking for (\d+) shows? again…$/, '正在重新查找 $1 个节目…', '$1 番組をもう一度探しています…'],
  [/^Speed (.+)$/, '速度 $1', '速度 $1'],
  [/^Sleep timer: (\d+) minutes\.$/, '睡眠定时：$1 分钟。', 'スリープタイマー：$1分。'],
  [/^Auto captions need a one-time setup\. Quit Afterhours and run: (.+)$/, '自动字幕需要一次性设置。请退出 Afterhours 并运行：$1', '自動字幕には一度だけ設定が必要です。Afterhours を終了して次を実行してください：$1'],
  [/^Couldn’t add that show\. (.+)$/, '无法添加这个节目。$1', 'この番組を追加できませんでした。$1'],
  [/^Added (.+) to your library\.$/, '已将“$1”添加到资料库。', '「$1」をライブラリに追加しました。'],
  [/^Removed (.+)\.$/, '已移除“$1”。', '「$1」を削除しました。'],
  [/^Remove “(.+)” from your library\?$/, '要从资料库移除“$1”吗？', '「$1」をライブラリから削除しますか？'],
  [/^Imported (\d+) shows\.$/, '已导入 $1 个节目。', '$1 番組を読み込みました。'],
  [/^Loaded captions from (.+)\.$/, '已从 $1 载入字幕。', '$1 から字幕を読み込みました。'],
  [/^(.+): the feed didn’t work, so episodes now come from the show’s website\.$/, '“$1”：订阅源无法使用，单集现在来自节目的网站。', '「$1」：フィードが使えないため、番組のウェブサイトから取得しています。'],
  [/^(.+): switched to a working feed\.$/, '“$1”：已切换到可用的订阅源。', '「$1」：使えるフィードに切り替えました。'],
  [/^(.+) Afterhours also looked on the show’s website and found no episodes\.$/, '$1 Afterhours 也查看了节目的网站，但没有找到单集。', '$1 番組のウェブサイトも確認しましたが、エピソードは見つかりませんでした。'],
  [/^The site answered with an error \((\d+)\)\.$/, '网站返回了错误（$1）。', 'サイトがエラーを返しました（$1）。'],
  [/^Couldn['’]t reach the site \((.+)\)\.$/, '无法连接到网站（$1）。', 'サイトに接続できませんでした（$1）。'],
  [/^The window component couldn’t be installed \((.+)\)\.$/, '无法安装窗口组件（$1）。', 'ウィンドウ部品をインストールできませんでした（$1）。'],
  [/^Afterhours (\d[\d.]*)$/, 'Afterhours $1', 'Afterhours $1']
];
const IDX = { 'zh-Hans': 0, ja: 1 };
const ATTRS = ['placeholder', 'aria-label', 'title'];
const ORIG = new WeakMap(), ATTR = new WeakMap();
let lang = 'en', idx = -1, observer = null;

function tr(text, depth) {
  depth = depth || 0;
  if (idx < 0 || text == null) return text;
  const s = String(text), k = s.trim();
  if (!k || !/[A-Za-z]/.test(k)) return s;
  let out = T[k] ? T[k][idx] : null;
  if (out == null && depth < 3) {
    for (const p of P) {
      const m = k.match(p[0]);
      if (m) { out = p[idx + 1].replace(/\$(\d)/g, (_, n) => tr(m[+n] || '', depth + 1)); break; }
    }
  }
  return out == null ? s : s.replace(k, out);
}
function doText(n) {
  if (!ORIG.has(n)) { if (idx < 0) return; ORIG.set(n, n.nodeValue); }
  const base = ORIG.get(n), want = idx < 0 ? base : tr(base);
  if (n.nodeValue !== want) n.nodeValue = want;
}
function doAttrs(el) {
  let rec = ATTR.get(el);
  for (const a of ATTRS) {
    if (!el.hasAttribute(a)) continue;
    const cur = el.getAttribute(a);
    if (!rec) { if (idx < 0) return; rec = {}; ATTR.set(el, rec); }
    const r = rec[a], base = r && cur === r[1] ? r[0] : cur, want = idx < 0 ? base : tr(base);
    rec[a] = [base, want];
    if (cur !== want) el.setAttribute(a, want);
  }
}
const SKIP = /^(SCRIPT|STYLE|CODE|TEXTAREA)$/;
const skipped = el => !!(el && el.closest && el.closest('[translate="no"], script, style, code, textarea'));
function walk(root) {
  if (root.nodeType === 3) { if (!skipped(root.parentElement)) doText(root); return; }
  if (root.nodeType !== 1 || skipped(root)) return;
  doAttrs(root);
  const w = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {
    acceptNode: n => n.nodeType === 1 && (n.getAttribute('translate') === 'no' || SKIP.test(n.tagName)) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
  let n;
  while ((n = w.nextNode())) { if (n.nodeType === 3) doText(n); else doAttrs(n); }
}
function onMutations(list) {
  for (const m of list) {
    if (m.type === 'attributes') { if (!skipped(m.target)) doAttrs(m.target); }
    else for (const n of m.addedNodes) walk(n);
  }
}
function setLang(code) {
  lang = code;
  idx = code in IDX ? IDX[code] : -1;
  walk(document.body);
  if (idx >= 0 && !observer) {
    observer = new MutationObserver(onMutations);
    observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ATTRS });
  } else if (idx < 0 && observer) { observer.disconnect(); observer = null; }
}
window.AHI18N = { tr, setLang, get lang() { return lang; }, keys: () => Object.keys(T) };
})();
</script>
<script>
(() => {
'use strict';

/* ---------- basics ---------- */
const $ = id => document.getElementById(id);
const audio = $('audio');
const view = $('view');
const CATALOG = JSON.parse($('catalog').textContent);
const CATS = CATALOG.categories;
const CAT = Object.fromEntries(CATS.map(c => [c.id, c]));
const PRESET = Object.fromEntries(CATALOG.presets.map(p => [p.key, p]));
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const VIA_HELPER = location.protocol === 'http:' || location.protocol === 'https:';
const sleep = ms => new Promise(r => setTimeout(r, ms));

let IMPORTING = false;   // set while a moved library is written in, so nothing overwrites it before the reload
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { if (IMPORTING) return false; try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } },
  del(k) { try { localStorage.removeItem(k); } catch {} }
};
const idb = (() => {
  let dbp = null;
  const open = () => dbp || (dbp = new Promise((resolve, reject) => {
    const r = indexedDB.open('afterhours', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('kv');
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  }));
  const run = async (mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('kv', mode);
      const req = fn(tx.objectStore('kv'));
      tx.oncomplete = () => resolve(req ? req.result : undefined);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error);
    });
  };
  return {
    get: k => run('readonly', s => s.get(k)).catch(() => undefined),
    set: (k, v) => run('readwrite', s => s.put(v, k)).catch(() => {}),
    del: k => run('readwrite', s => s.delete(k)).catch(() => {}),
    delPrefix: prefix => run('readwrite', s => {
      const req = s.openCursor();
      req.onsuccess = () => { const c = req.result; if (!c) return; if (String(c.key).startsWith(prefix)) c.delete(); c.continue(); };
      return null;
    }).catch(() => {})
  };
})();
function fnv(str) { let h = 0x811c9dc5; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193); } return (h >>> 0).toString(36); }
const norm = s => String(s || '').normalize('NFKC').toLowerCase().replace(/[\s\p{P}\p{S}]+/gu, '');
const KEY = { library: 'ah.library', settings: 'ah.settings', progress: 'ah.progress', last: 'ah.last', recent: 'ah.recent', resolved: 'ah.resolved', dir: 'ah.dir', colors: 'ah.colors', cap: id => 'ah.cap.' + fnv(id) };

const tr = text => (window.AHI18N ? window.AHI18N.tr(text) : text);
const IN_WINDOW = new URLSearchParams(location.search).get('window') === '1';   // opened as Afterhours' own window
const NO_TR = /(^|\s)(ep-title|ep-desc|ep-show|show-title|show-author|show-about|card-title|card-why|up-show|up-title|notes-text|lyric|t)(\s|$)/;  // podcast content stays as published
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  if (props.class && NO_TR.test(props.class)) n.setAttribute('translate', 'no');
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k === 'dataset') Object.assign(n.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? '' : v);
  }
  for (const c of kids.flat()) if (c != null && c !== false) n.append(c);
  return n;
}
const SVGNS = 'http://www.w3.org/2000/svg';
function icon(name) {
  const s = document.createElementNS(SVGNS, 'svg');
  s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS(SVGNS, 'use');
  u.setAttribute('href', '#i-' + name);
  s.append(u);
  return s;
}
function setIcon(btn, name) { const u = btn && btn.querySelector('use'); if (u) u.setAttribute('href', '#i-' + name); }
const firstChar = t => (Array.from(String(t || '').trim())[0] || '♪').toUpperCase();
function art(src, title, opts = {}) {
  const w = el('span', { class: 'art' + (opts.cls ? ' ' + opts.cls : '') + (opts.pending ? ' shimmer' : '') });
  w.style.setProperty('--mh', parseInt(fnv(String(title || '?')), 36) % 360);
  w.append(el('span', { class: 'mono', 'aria-hidden': 'true', text: firstChar(title) }));
  if (src) {
    const img = el('img', { alt: '', decoding: 'async', loading: opts.eager ? 'eager' : 'lazy', src });
    img.addEventListener('load', () => img.classList.add('ok'), { once: true });
    img.addEventListener('error', () => img.remove(), { once: true });
    w.append(img);
  }
  return w;
}
function fmtTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}
let LANG = 'en';
function fmtDur(s) {
  if (!s) return '';
  const m = Math.round(s / 60), h = Math.floor(m / 60), r = m % 60, mm = Math.max(1, m);
  if (LANG === 'zh-Hans') return m < 60 ? `${mm} 分钟` : r ? `${h} 小时 ${r} 分钟` : `${h} 小时`;
  if (LANG === 'ja') return m < 60 ? `${mm}分` : r ? `${h}時間${r}分` : `${h}時間`;
  return m < 60 ? `${mm} min` : r ? `${h} h ${r} min` : `${h} h`;
}
let dShort, dLong, dWeek;
function makeFormatters() {
  const loc = LANG === 'zh-Hans' ? 'zh-CN' : LANG === 'ja' ? 'ja-JP' : undefined;
  dShort = new Intl.DateTimeFormat(loc, { day: 'numeric', month: 'short' });
  dLong = new Intl.DateTimeFormat(loc, { day: 'numeric', month: 'short', year: 'numeric' });
  dWeek = new Intl.DateTimeFormat(loc, { weekday: 'long' });
}
makeFormatters();
function fmtDate(ms) {
  if (!ms) return '';
  const days = Math.floor((Date.now() - ms) / 864e5);
  if (days >= 0 && days < 1 && new Date(ms).getDate() === new Date().getDate()) return 'Today';
  if (days >= 0 && days < 7) return dWeek.format(ms);
  return (new Date(ms).getFullYear() === new Date().getFullYear() ? dShort : dLong).format(ms);
}
const fmtSpeed = v => (Math.round(v * 100) / 100).toString() + '×';
const clip = (s, n) => s.length > n ? s.slice(0, n).replace(/\s+\S*$/, '') + '…' : s;
function abs(u, base) { if (!u) return ''; try { return new URL(u, base).href; } catch { return u; } }
let toastTimer;
function toast(msg, isError) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.toggle('error', !!isError);
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), isError ? 6000 : 3000);
}

/* ---------- state ---------- */
const DEFAULTS = { speed: 1, volume: 1, muted: false, captions: true, autoNext: true, theme: 'system', sort: 'newest', autoCaptions: true, captionModel: 'auto', motion: !reduceMotion, pointerGlow: true, language: 'system', pro: false, asr: {} };
const settings = Object.assign({}, DEFAULTS, store.get(KEY.settings, {}));
const saveSettings = () => store.set(KEY.settings, settings);
const motionOn = () => settings.motion !== false;
function applyMotion() { document.documentElement.classList.toggle('no-motion', !motionOn()); }
function currentLang() {
  const v = settings.language || 'system';
  if (v !== 'system') return v;
  const n = (navigator.language || 'en').toLowerCase();
  return n.startsWith('zh') ? 'zh-Hans' : n.startsWith('ja') ? 'ja' : 'en';
}
function applyLanguage() {
  LANG = currentLang();
  document.documentElement.lang = LANG;
  makeFormatters();
  if (window.AHI18N) window.AHI18N.setLang(LANG);
}
let whoP = null;
const whoami = () => whoP || (whoP = getJSON('/api/ping').then(j => { S.client = j.client || 'local'; S.platform = j.platform || ''; return j; }, () => ({ client: 'local' })));
function applyGlow() { document.documentElement.classList.toggle('no-glow', settings.pointerGlow === false); }
let library = store.get(KEY.library, null);
let progress = store.get(KEY.progress, {});
let recent = store.get(KEY.recent, []);
const resolved = store.get(KEY.resolved, {});
const transient = new Map();
let route = { name: 'home', arg: '' };
const S = {
  current: null, queue: [], pendingSeek: null,
  cues: [], untimed: '', cueIdx: -2, capLoading: false, capError: '', activeLyric: null,
  dragging: false, lastSecond: -1, raf: 0, lastUserScroll: 0, npOpen: false, lyricsOn: true,
  sleep: { mode: 'off', endsAt: 0, timer: 0, fadeBase: null }
};
const saveLibrary = () => store.set(KEY.library, library);
const showBySid = sid => library.find(s => s.sid === sid) || transient.get(sid) || null;
const showByFeed = url => (url && library.find(s => s.feedUrl === url)) || null;
const snap = s => ({ sid: s.sid, title: s.title, author: s.author || '', image: s.image || '', feedUrl: s.feedUrl, cat: s.cat });

function seedLibrary() {
  if (Array.isArray(library)) return;
  library = CATALOG.presets.map(p => ({ sid: 'p-' + p.key, preset: p.key, cat: p.cat, title: p.title, author: p.author || '', image: '', feedUrl: '', status: 'pending', addedAt: Date.now() }));
  for (const s of store.get('ah.shows', [])) {   // shows added in version 1
    if (s.feedUrl && !library.some(x => x.feedUrl === s.feedUrl)) library.push({ sid: 'u-' + fnv(s.feedUrl), preset: null, cat: 'mine', title: s.title, author: s.author || '', image: s.image || '', feedUrl: s.feedUrl, status: 'ready', description: s.description || '', link: s.link || '', addedAt: s.addedAt || Date.now() });
  }
  try { for (const k of Object.keys(localStorage)) if (k.startsWith('ah.cache.')) localStorage.removeItem(k); } catch {}
  saveLibrary();
}

/* ---------- network ---------- */
function decodeBytes(buf, type) {
  const head = new TextDecoder('latin1').decode(new Uint8Array(buf, 0, Math.min(buf.byteLength, 300)));
  const xmlEnc = (head.match(/<\?xml[^>]*encoding=["']([^"']+)["']/i) || [])[1];
  const httpEnc = (type.match(/charset=["']?([^;"']+)/i) || [])[1];
  const enc = (xmlEnc || httpEnc || 'utf-8').trim().toLowerCase();
  try { return new TextDecoder(enc).decode(buf); } catch { return new TextDecoder('utf-8').decode(buf); }
}
async function getJSON(path) {
  let res;
  try { res = await fetch(path, { cache: 'no-store' }); }
  catch { const e = new Error('The player’s helper isn’t answering. Is afterhours.py still running?'); e.status = 0; throw e; }
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) { const e = new Error((data && data.error) || `Something went wrong (${res.status}).`); e.status = res.status; throw e; }
  return data;
}
async function postJSON(path) {
  let res;
  try { res = await fetch(path, { method: 'POST', headers: { 'X-Afterhours': '1' } }); }
  catch { const e = new Error('Afterhours’ helper isn’t answering.'); e.status = 0; throw e; }
  let data = null;
  try { data = await res.json(); } catch {}
  if (!res.ok) { const e = new Error((data && data.error) || `Something went wrong (${res.status}).`); e.status = res.status; throw e; }
  return data;
}
async function fetchText(url) {
  let res;
  try { res = await fetch(VIA_HELPER ? '/api/fetch?url=' + encodeURIComponent(url) : url, { cache: 'no-store' }); }
  catch { throw new Error(VIA_HELPER ? 'The player’s helper isn’t answering. Is afterhours.py still running?' : 'Open the player by running afterhours.py.'); }
  if (!res.ok) { let msg = ''; try { msg = (await res.json()).error || ''; } catch {} throw new Error(msg || `The site answered with an error (${res.status}).`); }
  const type = res.headers.get('Content-Type') || '';
  return { text: decodeBytes(await res.arrayBuffer(), type), type };
}
const feedInfo = url => getJSON('/api/feedinfo?url=' + encodeURIComponent(url));

/* ---------- feed parsing ---------- */
const NS = { itunes: ['itunes.com'], podcast: ['podcastindex'], content: ['purl.org/rss/1.0/modules/content'], media: ['search.yahoo.com/mrss'], dc: ['purl.org/dc/elements'] };
function inNS(n, key) {
  if (!key) return !n.prefix;
  if ((n.prefix || '').toLowerCase() === key) return true;
  const uri = (n.namespaceURI || '').toLowerCase();
  return NS[key].some(s => uri.includes(s));
}
function kidsNamed(parent, local, ns) { const out = []; if (!parent) return out; for (const n of parent.children) if (n.localName === local && inNS(n, ns)) out.push(n); return out; }
const kid = (p, local, ns) => kidsNamed(p, local, ns)[0] || null;
const txt = n => n ? n.textContent.trim() : '';
const attr = (n, a) => n ? (n.getAttribute(a) || '').trim() : '';
const ENTITIES = { nbsp: 160, mdash: 8212, ndash: 8211, hellip: 8230, rsquo: 8217, lsquo: 8216, rdquo: 8221, ldquo: 8220, copy: 169, reg: 174, trade: 8482, middot: 183, bull: 8226, eacute: 233, egrave: 232, aacute: 225, agrave: 224, ccedil: 231, uuml: 252, ouml: 246, auml: 228, szlig: 223 };
function parseXML(text) {
  const parser = new DOMParser();
  const bad = d => d.getElementsByTagName('parsererror').length > 0;
  let doc = parser.parseFromString(text.replace(/^[\s\uFEFF]+/, ''), 'application/xml');
  if (!bad(doc)) return doc;
  const cleaned = text.replace(/^[\s\uFEFF]+/, '')
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, '')
    .replace(/&([a-zA-Z][a-zA-Z0-9]*);/g, (m, name) => ['amp', 'lt', 'gt', 'quot', 'apos'].includes(name) ? m : (ENTITIES[name] ? `&#${ENTITIES[name]};` : `&amp;${name};`))
    .replace(/&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[\da-fA-F]+);)/g, '&amp;');
  doc = parser.parseFromString(cleaned, 'application/xml');
  return bad(doc) ? null : doc;
}
function htmlToText(html) {
  if (!html) return '';
  const pre = String(html).replace(/<br\s*\/?>/gi, '\n').replace(/<\/(p|div|li|h[1-6]|blockquote|tr)>/gi, '\n\n');
  const doc = new DOMParser().parseFromString(pre, 'text/html');
  return (doc.body ? doc.body.textContent : '').replace(/\u00a0/g, ' ').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}
function parseDuration(s) {
  if (!s) return 0;
  s = String(s).trim();
  if (/^\d+(\.\d+)?$/.test(s)) return parseFloat(s);
  const parts = s.split(':').map(Number);
  return parts.some(isNaN) ? 0 : parts.reduce((a, p) => a * 60 + p, 0);
}
function parseFeed(text, feedUrl) {
  const doc = parseXML(text);
  const notFeed = new Error('That link doesn’t look like a podcast feed.');
  if (!doc) throw notFeed;
  const root = doc.documentElement;
  const channel = root.localName === 'rss' ? kid(root, 'channel') : (root.localName === 'channel' ? root : null);
  if (!channel) throw notFeed;
  const showImage = abs(attr(kid(channel, 'image', 'itunes'), 'href') || txt(kid(kid(channel, 'image'), 'url')) || attr(kid(channel, 'thumbnail', 'media'), 'url'), feedUrl);
  const show = {
    title: txt(kid(channel, 'title')) || feedUrl,
    author: txt(kid(channel, 'author', 'itunes')) || txt(kid(kid(channel, 'owner', 'itunes'), 'name', 'itunes')) || txt(kid(channel, 'managingEditor')),
    image: showImage,
    description: clip(htmlToText(txt(kid(channel, 'description')) || txt(kid(channel, 'summary', 'itunes'))), 1200),
    link: txt(kid(channel, 'link'))
  };
  const prefix = fnv(feedUrl) + ':';
  const seen = new Set();
  const episodes = [];
  for (const item of kidsNamed(channel, 'item')) {
    const enc = kid(item, 'enclosure');
    let url = attr(enc, 'url'), type = attr(enc, 'type');
    if (!url) { const mc = kidsNamed(item, 'content', 'media').find(m => /audio|video/i.test(attr(m, 'type') + attr(m, 'medium'))); url = attr(mc, 'url'); type = attr(mc, 'type'); }
    if (!url) continue;
    url = abs(url, feedUrl);
    const id = prefix + (txt(kid(item, 'guid')) || url);
    if (seen.has(id)) continue;
    seen.add(id);
    const img = abs(attr(kid(item, 'image', 'itunes'), 'href') || attr(kid(item, 'thumbnail', 'media'), 'url'), feedUrl);
    episodes.push({
      id, url, type,
      title: txt(kid(item, 'title')) || 'Untitled episode',
      date: Date.parse(txt(kid(item, 'pubDate')) || txt(kid(item, 'date', 'dc'))) || 0,
      duration: parseDuration(txt(kid(item, 'duration', 'itunes'))),
      image: img && img !== showImage ? img : '',
      number: txt(kid(item, 'episode', 'itunes')),
      desc: clip(htmlToText(txt(kid(item, 'encoded', 'content')) || txt(kid(item, 'description')) || txt(kid(item, 'summary', 'itunes'))), 1600),
      transcripts: kidsNamed(item, 'transcript', 'podcast').map(t => ({ url: abs(attr(t, 'url'), feedUrl), type: attr(t, 'type').toLowerCase(), lang: attr(t, 'language') })).filter(t => t.url)
    });
  }
  episodes.sort((a, b) => b.date - a.date);
  return { show, episodes };
}

/* ---------- captions ---------- */
function parseClock(s) { let sec = 0; for (const p of String(s).trim().replace(',', '.').split(':')) sec = sec * 60 + parseFloat(p); return sec; }
function parseCues(text) {
  const cues = [];
  for (const block of text.replace(/\r\n?/g, '\n').split(/\n{2,}/)) {
    const lines = block.split('\n');
    const i = lines.findIndex(l => l.includes('-->'));
    if (i < 0) continue;
    const [a, b = ''] = lines[i].split('-->');
    const start = parseClock(a), end = parseClock(b.trim().split(/\s+/)[0]);
    let body = lines.slice(i + 1).join('\n');
    const v = body.match(/<v(?:\.[^\s>]+)?\s+([^>]+)>/);
    body = body.replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&nbsp;/g, ' ').trim();
    if (!body || !isFinite(start)) continue;
    cues.push({ start, end: isFinite(end) ? end : start + 3, text: body, speaker: v ? v[1].trim() : '' });
  }
  return cues.sort((x, y) => x.start - y.start);
}
function parseJSONTranscript(obj) {
  const segs = Array.isArray(obj) ? obj : (obj.segments || []);
  const num = v => typeof v === 'number' ? v : (typeof v === 'string' && v.includes(':') ? parseClock(v) : parseFloat(v));
  const cjk = /[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]/;
  const cues = [];
  let cur = null;
  for (const s of segs) {
    const start = num(s.startTime ?? s.start), end = num(s.endTime ?? s.end ?? s.startTime ?? s.start);
    const body = String(s.body ?? s.text ?? '').trim();
    if (!body || !isFinite(start)) continue;
    const speaker = s.speaker || '';
    if (cur && (speaker !== cur.speaker || start - cur.end > 1.5 || cur.text.length > 84 || end - cur.start > 7 || /[.!?。！？…]["”’)]?$/.test(cur.text))) { cues.push(cur); cur = null; }
    if (!cur) cur = { start, end: isFinite(end) ? end : start + 2, text: body, speaker };
    else { cur.text += (cjk.test(cur.text.slice(-1)) || cjk.test(body[0]) ? '' : ' ') + body; if (isFinite(end)) cur.end = end; }
  }
  if (cur) cues.push(cur);
  return cues;
}
function applyTranscript(text, type) {
  type = (type || '').toLowerCase();
  const head = text.slice(0, 300).trim();
  let cues = [];
  if (type.includes('json') || /^[\[{]/.test(head)) { try { cues = parseJSONTranscript(JSON.parse(text)); } catch { cues = []; } }
  else if (/vtt|srt|subrip/.test(type) || text.slice(0, 4000).includes('-->')) cues = parseCues(text);
  S.cues = cues;
  S.untimed = cues.length ? '' : (/<[a-z][\s\S]*>/i.test(head) || type.includes('html') ? htmlToText(text) : text.trim());
  S.cueIdx = -2;
}
function pickTranscript(list) {
  if (!list || !list.length) return null;
  const score = t => { const u = t.url.toLowerCase().split('?')[0], ty = t.type; if (ty.includes('vtt') || u.endsWith('.vtt')) return 5; if (ty.includes('srt') || ty.includes('subrip') || u.endsWith('.srt')) return 4; if (ty.includes('json') || u.endsWith('.json')) return 3; if (ty.includes('html') || u.endsWith('.html')) return 2; return 1; };
  return [...list].sort((a, b) => score(b) - score(a))[0];
}
async function loadCaptions(ep) {
  Recog.stop();
  S.cues = []; S.untimed = ''; S.cueIdx = -2; S.capError = ''; S.capLoading = false; S.auto = null;
  const mine = () => S.current && S.current.ep.id === ep.id;
  const saved = store.get(KEY.cap(ep.id), null);
  if (saved && saved.text) applyTranscript(saved.text, saved.type);
  else {
    const t = pickTranscript(ep.transcripts);
    if (t) {
      S.capLoading = true;
      renderLyrics(); renderCaptions(true);
      try {
        const res = await fetchText(t.url);
        if (!mine()) return;
        applyTranscript(res.text, t.type || res.type);
      } catch { if (mine()) S.capError = 'This episode’s captions couldn’t be loaded.'; }
      if (!mine()) return;
      S.capLoading = false;
    }
  }
  if (!S.cues.length && !S.untimed) {   // no captions of its own: use recognised ones, or make them
    const auto = await idb.get('asr:' + ep.id);
    if (!mine()) return;
    const has = !!(auto && auto.cues && auto.cues.length);
    if (has) { S.cues = auto.cues; S.auto = { state: auto.complete ? 'done' : 'partial', model: auto.model, progress: auto.complete ? 1 : 0 }; }
    if (!has || !auto.complete) Recog.maybeStart(ep, has);
  }
  renderLyrics();
  renderCaptions(true);
  paintRecog();
}
/* recognising speech on this computer, through the helper */
const ACTIVE = ['queued', 'downloading', 'loading', 'transcribing'];
const recogActive = () => !!(S.auto && ACTIVE.includes(S.auto.state));
const langOf = show => { const lib = show && showByFeed(show.feedUrl); const p = lib && lib.preset ? PRESET[lib.preset] : null; return (p && p.lang) || ''; };
const Recog = {
  cap: null, timer: 0, ep: null, url: '', model: '', since: 0, lastSave: 0, dropCached: false,
  async init() {
    if (!VIA_HELPER) { this.cap = { available: false }; return this.cap; }
    try { this.cap = await getJSON('/api/captions'); } catch { this.cap = { available: false }; }
    return this.cap;
  },
  modelFor(show) { if (settings.pro && asrGet('model') !== 'auto') return asrGet('model'); return settings.captionModel && settings.captionModel !== 'auto' ? settings.captionModel : (langOf(show) ? 'small' : 'base'); },
  maybeStart(ep, resume) {
    if (!this.cap) { this.init().then(() => { if (S.current && S.current.ep.id === ep.id) { this.maybeStart(ep, resume); paintRecog(); renderLyrics(); renderCaptions(true); } }); return; }
    if (!this.cap.available) { if (!S.auto) S.auto = { state: 'unavailable' }; return; }
    if (!settings.autoCaptions) { if (!resume) S.auto = { state: 'off' }; return; }
    this.start(ep);
  },
  async start(ep, model) {
    this.stop();
    if (!S.current || S.current.ep.id !== ep.id) return;
    const show = S.current.show;
    Object.assign(this, { ep, id: '', url: ep.url, model: model || this.modelFor(show), since: 0, dropCached: S.cues.length > 0 });
    S.auto = { state: 'queued', progress: 0, model: this.model };
    paintRecog();
    const at = Math.floor(S.pendingSeek != null ? S.pendingSeek : (audio.currentTime || 0));
    try {
      const st = await getJSON(`/api/captions/start?url=${encodeURIComponent(ep.url)}&model=${this.model}&start=${at}&lang=${langOf(show)}${settings.pro ? '&opts=' + encodeURIComponent(JSON.stringify(proOpts())) : ''}`);
      if (this.ep === ep) this.apply(st);
    } catch (e) { if (this.ep === ep) { S.auto = { state: 'error', error: e.message }; paintRecog(); renderLyrics(); renderCaptions(true); } return; }
    this.poll();
  },
  poll() {
    clearTimeout(this.timer);
    if (!this.ep || !recogActive()) return;
    const ep = this.ep;
    this.timer = setTimeout(async () => {
      try { const st = await getJSON(`/api/captions/status?url=${encodeURIComponent(this.url)}&model=${this.model}&since=${this.since}&id=${this.id || ''}`); if (this.ep === ep) this.apply(st); }
      catch {}
      if (this.ep === ep) this.poll();
    }, 1500);
  },
  apply(st) {
    if (!st || st.state === 'none') return;
    if (st.id) this.id = st.id;
    const fresh = (st.segments || []).map(c => ({ start: c.start, end: c.end, text: c.text, speaker: '', auto: true }));
    if (fresh.length) {
      const before = S.cues.length;
      if (this.dropCached || this.since === 0) { S.cues = []; this.dropCached = false; }
      const ordered = !S.cues.length || fresh[0].start >= S.cues[S.cues.length - 1].start;
      S.cues = S.cues.concat(fresh);
      if (!ordered) S.cues.sort((a, b) => a.start - b.start);
      S.cueIdx = -2;
      if (ordered && S.cues.length - fresh.length === before && before > 0 && $('lyrics').children.length === before) appendLyrics(fresh, before);
      else renderLyrics();
    }
    this.since = st.total || this.since;
    S.auto = { state: st.state, progress: st.progress || 0, model: st.model || this.model, message: st.message || '', error: st.error || '' };
    if (st.state === 'done' || (fresh.length && Date.now() - this.lastSave > 15000)) this.save(st.state === 'done');
    if (!S.cues.length) renderLyrics();
    paintRecog();
    renderCaptions(true);
  },
  save(complete) {
    if (!this.ep || !S.cues.length) return;
    this.lastSave = Date.now();
    idb.set('asr:' + this.ep.id, { cues: S.cues.map(c => ({ start: c.start, end: c.end, text: c.text, speaker: '', auto: true })), model: this.model, complete, t: Date.now() });
  },
  stop() {
    clearTimeout(this.timer);
    if (this.ep && recogActive()) {
      this.save(false);
      getJSON(`/api/captions/cancel?url=${encodeURIComponent(this.url)}&model=${this.model}&id=${this.id || ''}`).catch(() => {});
    }
    this.ep = null;
  }
};
function recogPct() { return S.auto ? Math.round(Math.min(1, S.auto.progress || 0) * 100) : 0; }
function recogText() {
  const a = S.auto;
  if (!a) return '';
  switch (a.state) {
    case 'queued': return 'Getting ready to recognise speech…';
    case 'downloading': return `Downloading the episode for captions… ${recogPct()}%`;
    case 'loading': return a.message || 'Loading the speech model…';
    case 'transcribing': return `Auto captions: recognising speech, ${recogPct()}% done`;
    case 'done': return 'Auto captions, made on this computer';
    case 'partial': return 'Auto captions, partly done';
    case 'cancelled': return 'Auto captions stopped';
    case 'error': return a.error || 'Caption recognition stopped.';
    default: return '';
  }
}
function paintRecog() {
  const box = $('npStatus');
  const a = S.auto;
  const show = !!(a && S.cues.length && ['queued', 'downloading', 'loading', 'transcribing', 'done', 'partial', 'cancelled', 'error'].includes(a.state));
  box.hidden = !show;
  if (!show) return;
  const kids = [el('span', { text: recogText() })];
  if (recogActive()) kids.push(el('span', { class: 'meter' }, el('i', { style: `width:${recogPct()}%` })), el('button', { class: 'link-btn light', type: 'button', text: 'Stop', onclick: stopRecognition }));
  else if (S.current && (a.state === 'cancelled' || a.state === 'partial')) kids.push(el('button', { class: 'link-btn light', type: 'button', text: 'Continue', onclick: () => Recog.start(S.current.ep, a.model) }));
  else if (S.current && a.state === 'done' && ['tiny', 'base'].includes(a.model)) kids.push(el('button', { class: 'link-btn light', type: 'button', text: 'Redo with more accuracy', onclick: () => Recog.start(S.current.ep, 'small') }));
  kids.push(capSwitch());
  box.replaceChildren(...kids);
}
function stopRecognition() { Recog.stop(); if (S.auto) S.auto = Object.assign({}, S.auto, { state: 'cancelled' }); paintRecog(); renderLyrics(); renderCaptions(true); }
function captionCard() {
  const a = S.auto, ep = S.current && S.current.ep;
  if (!a || !ep) return null;
  const card = (title, text, ...rest) => el('div', { class: 'cap-card' }, el('h3', { text: title }), text ? el('p', { text }) : null, ...rest);
  const startBtn = label => el('button', { class: 'np-chip', type: 'button', onclick: () => Recog.start(ep) }, icon('wave'), el('span', { text: label }));
  if (recogActive()) return card('Recognising speech on this computer', 'Captions appear here as they’re ready, starting from where you are in the episode.',
    el('p', { text: recogText() }), el('span', { class: 'meter' }, el('i', { style: `width:${recogPct()}%` })),
    el('div', { class: 'cap-actions' }, el('button', { class: 'np-chip', type: 'button', onclick: stopRecognition }, el('span', { text: 'Stop' })), capSwitch()));
  if (a.state === 'unavailable' && S.client === 'android') return card('No captions for this episode', 'Auto captions aren’t available on phones yet. Feed transcripts and caption files still work.');
  if (a.state === 'unavailable') return card('Caption any episode', 'This episode has no captions. Afterhours can recognise the speech on your computer and caption it, privately and for free. To turn this on, quit Afterhours and run this once:',
    el('code', { text: (Recog.cap && Recog.cap.install) || 'python3 afterhours.py --setup-captions' }), el('p', { text: 'Then start Afterhours again. If you’d rather not, switch auto captions off.' }), el('div', { class: 'cap-actions' }, capSwitch()));
  if (a.state === 'off') return card('No captions for this episode', 'Auto captions are off, so episodes without captions aren’t captioned automatically.', el('div', { class: 'cap-actions' }, startBtn('Caption this episode'), capSwitch()));
  if (a.state === 'error') return card('Captions couldn’t be made', a.error || '', el('div', { class: 'cap-actions' }, startBtn('Try again'), capSwitch()));
  if (a.state === 'cancelled') return card('Captions stopped', '', el('div', { class: 'cap-actions' }, startBtn('Caption this episode'), capSwitch()));
  return null;
}
function findCue(t) { const c = S.cues; let lo = 0, hi = c.length - 1, ans = -1; while (lo <= hi) { const mid = (lo + hi) >> 1; if (c[mid].start <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1; } return ans; }
const lyricsVisible = () => S.npOpen && S.lyricsOn;
function captionHint() {
  if (S.capLoading) return 'Loading captions…';
  if (recogActive()) return recogText();
  if (S.auto && S.auto.state === 'error') return S.auto.error || 'Caption recognition stopped.';
  if (S.auto && S.auto.state === 'unavailable') return 'No captions for this episode. Open the transcript to turn on auto captions.';
  return S.capError || (S.untimed ? 'This transcript isn’t timed. Open the transcript to read it.' : 'No captions for this episode.');
}
function renderCaptions(force) {
  const t = S.pendingSeek != null ? S.pendingSeek : audio.currentTime;
  let i = -1;
  if (S.cues.length) { i = findCue(t); if (i >= 0 && t > S.cues[i].end + 1.2) i = -1; }
  const changed = i !== S.cueIdx;
  if (!changed && !force) return;
  S.cueIdx = i;
  const cur = i >= 0 ? S.cues[i] : null;
  const pill = $('capPill');
  const hint = !cur && recogActive() && !S.cues.length;
  pill.hidden = !(settings.captions && S.current && !S.npOpen && (cur || hint));
  pill.classList.toggle('hint', hint);
  if (cur) {
    $('capWho').textContent = cur.speaker || ''; $('capWho').hidden = !cur.speaker; $('capText').textContent = cur.text;
    if (changed && !pill.hidden && motionOn()) $('capText').animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 260, easing: 'cubic-bezier(0, 0, 0, 1)' });
  }
  else if (hint) { $('capWho').hidden = true; $('capText').textContent = recogText(); }
  const line = $('npCaption');
  line.hidden = !settings.captions || lyricsVisible() || !S.current;
  line.textContent = cur ? cur.text : (S.cues.length ? '' : captionHint());
  highlightLyric(i);
}
function lyricNode(c, i, prev) {
  return el('li', { class: 'lyric', dataset: { i } }, c.speaker && c.speaker !== prev ? el('span', { class: 'who', text: c.speaker }) : null, c.text);
}
function appendLyrics(fresh, from) {
  const list = $('lyrics');
  let prev = from > 0 ? (S.cues[from - 1].speaker || '') : '';
  fresh.forEach((c, k) => { list.append(lyricNode(c, from + k, prev)); prev = c.speaker || prev; });
}
function renderLyrics() {
  const list = $('lyrics'), notes = $('npNotes');
  list.replaceChildren();
  S.activeLyric = null;
  const has = S.cues.length > 0;
  list.hidden = !has;
  notes.hidden = has;
  if (has) {
    let prev = '';
    const frag = document.createDocumentFragment();
    S.cues.forEach((c, i) => { frag.append(lyricNode(c, i, prev)); prev = c.speaker || prev; });
    list.append(frag);
  } else {
    const c = S.current;
    notes.replaceChildren(
      captionCard() || '',
      el('h3', { text: S.untimed ? 'Transcript' : 'About this episode' }),
      el('p', { class: 'notes-text', text: S.untimed || (c && c.ep.desc) || 'No notes for this episode.' }),
      el('div', { class: 'notes-actions' }, S.capLoading ? el('p', { class: 'np-label', text: 'Loading captions…' }) :
        el('button', { class: 'np-chip', type: 'button', onclick: () => $('capFile').click() }, icon('file'), el('span', { text: 'Load a caption file' })))
    );
  }
  if (S.cueIdx >= 0) highlightLyric(S.cueIdx);
}
function highlightLyric(i) {
  if (S.activeLyric) S.activeLyric.classList.remove('active');
  S.activeLyric = null;
  if (i < 0) return;
  const li = $('lyrics').children[i];
  if (!li) return;
  li.classList.add('active');
  S.activeLyric = li;
  if (lyricsVisible() && Date.now() - S.lastUserScroll > 3500) {
    const box = $('lyrics');
    box.scrollTo({ top: li.offsetTop - box.clientHeight * 0.36, behavior: !motionOn() ? 'auto' : 'smooth' });
  }
}

/* ---------- Apple Podcasts directory (paced by the helper) ---------- */
const Q = { jobs: [], busy: false };
function dirJob(fn, pri) { return new Promise((resolve, reject) => { Q.jobs.push({ fn, pri, resolve, reject }); Q.jobs.sort((a, b) => a.pri - b.pri); pumpQ(); }); }
async function pumpQ() {
  if (Q.busy) return;
  const job = Q.jobs.shift();
  if (!job) return;
  Q.busy = true;
  try { job.resolve(await job.fn()); } catch (e) { job.reject(e); } finally { Q.busy = false; pumpQ(); }
}
const dirCache = store.get(KEY.dir, {});
function dirCacheSet(k, r) {
  dirCache[k] = { t: Date.now(), r };
  const keys = Object.keys(dirCache);
  if (keys.length > 300) keys.sort((a, b) => dirCache[a].t - dirCache[b].t).slice(0, keys.length - 250).forEach(x => delete dirCache[x]);
  store.set(KEY.dir, dirCache);
}
const slim = it => ({ appleId: it.collectionId, title: it.collectionName || it.trackName || '', author: it.artistName || '', feedUrl: it.feedUrl || '', image: it.artworkUrl600 || it.artworkUrl100 || '', genre: it.primaryGenreName || '' });
async function dirSearch(term, country = 'gb', pri = 5, limit = 20) {
  const k = `s:${country}:${limit}:${term.toLowerCase()}`;
  const hit = dirCache[k];
  if (hit && Date.now() - hit.t < 7 * 864e5) return hit.r;
  if (!VIA_HELPER) throw new Error('Search needs the player’s helper. Open the player by running afterhours.py.');
  const data = await dirJob(() => getJSON(`/api/search?term=${encodeURIComponent(term)}&country=${country}&limit=${limit}`), pri);
  const r = (data.results || []).filter(x => x.feedUrl).map(slim);
  dirCacheSet(k, r);
  return r;
}
async function dirLookup(ids, country = 'gb', pri = 3) {
  const data = await dirJob(() => getJSON(`/api/lookup?id=${ids.join(',')}&country=${country}`), pri);
  return (data.results || []).filter(x => x.feedUrl).map(slim);
}
const resolving = new Map();
function titleOk(found, item) { const a = norm(found), b = norm(item.match || item.title); return !!a && !!b && (a.includes(b) || b.includes(a)); }
function bestMatch(results, item) {
  const want = norm(item.match || item.title), artist = norm(item.artist || '');
  let best = null, top = 0;
  for (const r of results) {
    const t = norm(r.title), a = norm(r.author);
    let sc = 0;
    if (t === want) sc += 6;
    else if (t.includes(want) || (want.includes(t) && t.length >= 4)) sc += 3;
    if (artist) sc += a.includes(artist) ? 3 : (sc < 6 ? -2 : 0);
    if (sc > top) { top = sc; best = r; }
  }
  return top >= 3 ? best : null;
}
function resolveItem(item, pri = 5) {
  if (resolved[item.key]) return Promise.resolve(resolved[item.key]);
  if (resolving.has(item.key)) return resolving.get(item.key);
  const p = (async () => {
    let r = null;
    const countries = item.countries || ['gb', 'us'];
    if (item.id) { try { r = (await dirLookup([item.id], countries[0], pri))[0] || null; } catch (e) { if (e.status === 429) throw e; } }
    if (!r && item.feed && VIA_HELPER) { try { const info = await feedInfo(item.feed); if (info && titleOk(info.title, item)) r = { title: info.title, author: info.author || '', feedUrl: item.feed, image: info.image || '', genre: '' }; } catch {} }
    if (!r) {
      try { for (const c of countries) { r = bestMatch(await dirSearch(item.term || item.title, c, pri, 10), item); if (r) break; } }
      catch (e) { if (e.status === 429) throw e; }
    }
    if (!r && item.site && VIA_HELPER) {   // not in Apple's directory: look on the show's own website
      try {
        const res = await crawlSite(item.site, item.title);
        const image = (res.show && res.show.image) || '';
        if (res.feedUrl) r = { title: item.title, author: item.author || '', feedUrl: res.feedUrl, image, genre: '', site: res.pageUrl, source: 'rss' };
        else if (res.episodes && res.episodes.length) {
          const key = 'web:' + res.pageUrl;
          await idb.set('eps:' + key, { t: Date.now(), eps: webEpisodes(res, key) });
          r = { title: item.title, author: item.author || '', feedUrl: key, image, genre: '', site: res.pageUrl, source: 'web' };
        }
      } catch {}
    }
    if (r) { resolved[item.key] = r; store.set(KEY.resolved, resolved); }
    return r;
  })();
  resolving.set(item.key, p);
  p.then(() => resolving.delete(item.key), () => resolving.delete(item.key));
  return p;
}

/* ---------- first-run setup: find feeds for the pre-added shows ---------- */
const Setup = { running: false, msg: '' };
function setupCounts() { const ps = library.filter(s => s.preset); return { total: ps.length, done: ps.filter(s => s.status !== 'pending').length }; }
async function runSetup() {
  if (Setup.running || !VIA_HELPER) return;
  const pending = library.filter(s => s.status === 'pending' && s.preset);
  if (!pending.length) return;
  Setup.running = true;
  updateSetupUI();
  try {
    const withId = pending.filter(s => !resolved[s.preset] && PRESET[s.preset] && PRESET[s.preset].id);
    if (withId.length) {
      try {
        for (const r of await dirLookup(withId.map(s => PRESET[s.preset].id), 'gb', 2)) {
          const s = withId.find(x => String(PRESET[x.preset].id) === String(r.appleId));
          if (s) resolved[s.preset] = r;
        }
        store.set(KEY.resolved, resolved);
      } catch {}
    }
    for (const s of pending) {
      if (s.status !== 'pending' || !library.includes(s)) continue;
      let r = null;
      for (let attempt = 0; attempt < 3; attempt++) {
        try { r = await resolveItem(PRESET[s.preset], 2); break; }
        catch (e) {
          if (e.status === 429) { Setup.msg = 'Apple’s directory asked for a short pause. Carrying on in a moment…'; updateSetupUI(); await sleep(30000); Setup.msg = ''; continue; }
          break;
        }
      }
      applyResolved(s, r);
      updateSetupUI();
    }
  } finally { Setup.running = false; updateSetupUI(); }
  refreshAllFeeds();
}
function applyResolved(s, r) {
  if (r && r.feedUrl) {
    if (library.some(x => x !== s && x.feedUrl === r.feedUrl)) { library = library.filter(x => x !== s); saveLibrary(); renderSidebar(); return; }
    Object.assign(s, { feedUrl: r.feedUrl, author: r.author || s.author, image: r.image || s.image, appleId: r.appleId || null, genre: r.genre || '', site: r.site || '', source: r.source || 'rss', status: 'ready' });
  } else s.status = 'missing';
  saveLibrary();
  renderSidebar();
  for (const n of view.querySelectorAll(`.tile[data-sid="${s.sid}"]`)) n.replaceWith(showTile(s));
}
function updateSetupUI() {
  const { total, done } = setupCounts();
  const active = Setup.running && done < total;
  const sp = $('sideProgress');
  sp.hidden = !active;
  sp.textContent = active ? `${done} of ${total}` : '';
  const b = $('setupBanner');
  if (!b) return;
  if (!active) { b.replaceChildren(); return; }
  b.replaceChildren(el('div', { class: 'banner' },
    el('div', { class: 'banner-text' }, el('strong', { text: 'Setting up your shelf' }), el('span', { text: Setup.msg || `Finding the ${total} shows from your poster. ${done} of ${total} ready. Apple’s directory is queried gently, so this takes about a minute the first time.` })),
    el('div', { class: 'meter', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(total), 'aria-valuenow': String(done) }, el('i', { style: `width:${Math.round(done / Math.max(1, total) * 100)}%` }))));
}

/* ---------- feeds (cached in IndexedDB), with a website fallback ---------- */
const crawlSite = (url, title) => getJSON('/api/crawl?url=' + encodeURIComponent(url) + (title ? '&title=' + encodeURIComponent(title) : ''));
const appleUrlOf = s => s && s.appleId ? `https://podcasts.apple.com/gb/podcast/id${s.appleId}` : '';
function webEpisodes(result, key) {
  const prefix = fnv(key) + ':', seen = new Set();
  return (result.episodes || []).filter(e => e.url && !seen.has(e.url) && seen.add(e.url)).map(e => ({
    id: prefix + (e.guid || e.url), url: e.url, type: '', title: e.title || 'Untitled episode', date: e.date || 0,
    duration: e.duration || 0, image: e.image || '', number: '', desc: clip(e.desc || '', 1600), transcripts: [], link: e.link || ''
  }));
}
function switchSource(show, feedUrl, kind, site, meta) {
  const old = show.feedUrl;
  const targets = [...new Set([show, showByFeed(old)].filter(Boolean))];
  for (const t of targets) {
    Object.assign(t, { feedUrl, source: kind, site: site || t.site || '', origFeed: t.origFeed || old || '', status: 'ready' });
    if (meta) { if (!t.image && meta.image) t.image = meta.image; if (!t.description && meta.description) t.description = meta.description; }
  }
  saveLibrary();
  renderSidebar();
  if (old && old !== feedUrl) toast(kind === 'web' ? `${show.title}: the feed didn’t work, so episodes now come from the show’s website.` : `${show.title}: switched to a working feed.`);
}
const Feeds = {
  inflight: new Map(),
  async cached(show) { if (!show || !show.feedUrl) return null; return (await idb.get('eps:' + show.feedUrl)) || null; },
  async fromFeed(show, url) {
    const { text } = await fetchText(url);
    const parsed = parseFeed(text, url);
    noteFeedMeta(show, parsed.show);
    return parsed.episodes.slice(0, 300);
  },
  async fromCrawl(show, result) {
    if (result.feedUrl) {
      const eps = await this.fromFeed(show, result.feedUrl);
      if (eps.length) { switchSource(show, result.feedUrl, 'rss', result.pageUrl, result.show); return eps; }
    }
    const key = 'web:' + result.pageUrl;
    const eps = webEpisodes(result, key);
    if (!eps.length) throw new Error('No episodes were found at that address.');
    switchSource(show, key, 'web', result.pageUrl, result.show);
    return eps;
  },
  async rescue(show, err) {
    const p = show.preset ? PRESET[show.preset] : null;
    const sites = [...new Set([show.feedUrl, show.site, p && p.site, show.link, appleUrlOf(show)].filter(u => /^https?:\/\//i.test(u || '')))];
    for (const site of sites) { try { return await this.fromCrawl(show, await crawlSite(site, show.title)); } catch {} }
    throw new Error(`${err.message} Afterhours also looked on the show’s website and found no episodes.`);
  },
  refresh(show) {
    const url = show.feedUrl;
    if (this.inflight.has(url)) return this.inflight.get(url);
    const p = (async () => {
      let eps;
      if (url.startsWith('web:')) eps = await this.fromCrawl(show, await crawlSite(url.slice(4), show.title));
      else {
        try { eps = await this.fromFeed(show, url); if (!eps.length) throw new Error('This feed has no playable episodes.'); }
        catch (e) { eps = await this.rescue(show, e); }
      }
      await idb.set('eps:' + show.feedUrl, { t: Date.now(), eps });
      return eps;
    })();
    this.inflight.set(url, p);
    p.then(() => this.inflight.delete(url), () => this.inflight.delete(url));
    return p;
  },
  async useSource(show, input) {
    let url = input.trim().replace(/^(feed|pcast|podcast|itpc):\/\//i, 'https://');
    if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
    let eps = null;
    try { eps = await this.fromFeed(show, url); if (eps.length) switchSource(show, url, 'rss'); else eps = null; } catch {}
    if (!eps) eps = await this.fromCrawl(show, await crawlSite(url, show.title));
    await idb.set('eps:' + show.feedUrl, { t: Date.now(), eps });
    return eps;
  }
};
function noteFeedMeta(show, meta) {
  const s = showByFeed(show.feedUrl) || transient.get(show.sid);
  if (!s) return;
  let changed = false;
  for (const [k, v] of [['image', meta.image], ['author', meta.author]]) if (!s[k] && v) { s[k] = v; changed = true; }
  for (const [k, v] of [['description', meta.description], ['link', meta.link]]) if (v && s[k] !== v) { s[k] = v; changed = true; }
  if (changed && library.includes(s)) saveLibrary();
}
async function pool(items, n, fn) {
  const q = [...items];
  await Promise.all(Array.from({ length: Math.min(n, q.length) }, async () => { while (q.length) { const it = q.shift(); try { await fn(it); } catch {} } }));
}
let refreshingAll = false, latestTimer = 0;
async function refreshAllFeeds() {
  if (refreshingAll || !VIA_HELPER) return;
  refreshingAll = true;
  try {
    const stale = [];
    for (const s of library.filter(x => x.status === 'ready' && x.feedUrl)) { const c = await Feeds.cached(s); if (!c || Date.now() - c.t > 6 * 3600e3) stale.push(s); }
    let done = 0;
    if (stale.length) feedStatus(`Checking ${stale.length} shows for new episodes…`);
    await pool(stale, 3, async s => { try { await Feeds.refresh(s); } finally { done++; feedStatus(`Checking for new episodes… ${done} of ${stale.length}`); scheduleLatest(); } });
    feedStatus('');
  } finally { refreshingAll = false; scheduleLatest(); }
}
function feedStatus(t) { const n = $('feedStatus'); if (n) n.textContent = t; }
function scheduleLatest() { clearTimeout(latestTimer); latestTimer = setTimeout(() => { if (route.name === 'home') fillLatest(); }, 500); }

/* ---------- colour from artwork ---------- */
const colors = store.get(KEY.colors, {});
function rgbToHsl(r, g, b) {
  r /= 255; g /= 255; b /= 255;
  const max = Math.max(r, g, b), min = Math.min(r, g, b), l = (max + min) / 2;
  if (max === min) return [0, 0, l];
  const d = max - min, s = l > .5 ? d / (2 - max - min) : d / (max + min);
  const h = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return [h * 60, s, l];
}
function vibrant(d) {
  const buckets = new Map();
  for (let i = 0; i < d.length; i += 4) {
    if (d[i + 3] < 200) continue;
    const [h, s, l] = rgbToHsl(d[i], d[i + 1], d[i + 2]);
    if (l < .12 || l > .92 || s < .12) continue;
    const k = Math.round(h / 24) % 15;
    const b = buckets.get(k) || { n: 0, h: 0, s: 0, l: 0 };
    b.n++; b.h += h; b.s += s; b.l += l;
    buckets.set(k, b);
  }
  let best = null, top = -1;
  for (const b of buckets.values()) {
    const s = b.s / b.n, l = b.l / b.n, score = b.n * (.35 + s) * (1 - Math.abs(l - .5));
    if (score > top) { top = score; best = { h: Math.round(b.h / b.n), s: Math.round(s * 100), l: Math.round(l * 100) }; }
  }
  return best;
}
const smallArt = u => u.replace(/\/\d+x\d+(bb|cc|sr)?\.(jpg|jpeg|png|webp)(\?.*)?$/i, '/120x120bb.$2');
function artColor(url) {
  if (!url || !VIA_HELPER) return Promise.resolve(null);
  if (colors[url]) return Promise.resolve(colors[url]);
  return new Promise(resolve => {
    const img = new Image();
    img.onload = () => {
      try {
        const c = document.createElement('canvas');
        c.width = c.height = 24;
        const g = c.getContext('2d', { willReadFrequently: true });
        g.drawImage(img, 0, 0, 24, 24);
        const hsl = vibrant(g.getImageData(0, 0, 24, 24).data);
        if (hsl) { colors[url] = hsl; const keys = Object.keys(colors); if (keys.length > 400) delete colors[keys[0]]; store.set(KEY.colors, colors); }
        resolve(hsl);
      } catch { resolve(null); }
    };
    img.onerror = () => resolve(null);
    img.src = '/api/image?url=' + encodeURIComponent(smallArt(url));
  });
}
function applyTint(node, hsl) {
  if (!node || !hsl) return;
  node.style.setProperty('--th', hsl.h);
  node.style.setProperty('--ts', Math.max(38, Math.min(82, hsl.s)) + '%');
  node.style.setProperty('--tl', Math.max(42, Math.min(60, hsl.l)) + '%');
}

/* ---------- building blocks ---------- */
function pageHead(title, sub, extra) { return el('header', { class: 'page-head' }, el('div', {}, el('h1', { class: 'page-title', text: title }), sub ? el('p', { class: 'page-sub', text: sub }) : null), extra || null); }
function section(title, sub, body, opts = {}) {
  const tools = el('div', { class: 'section-tools' });
  if (opts.link) tools.append(el('a', { class: 'see-all', href: opts.link[1], text: opts.link[0] }));
  if (body.classList.contains('row')) {
    const by = dir => () => body.scrollBy({ left: dir * body.clientWidth * 0.9, behavior: !motionOn() ? 'auto' : 'smooth' });
    tools.append(el('button', { class: 'row-nav', type: 'button', 'aria-label': `Scroll ${title} left`, onclick: by(-1) }, icon('chevleft')), el('button', { class: 'row-nav', type: 'button', 'aria-label': `Scroll ${title} right`, onclick: by(1) }, icon('chevright')));
  }
  return el('section', { class: 'section' }, el('div', { class: 'section-head' }, el('div', {}, el('h2', { class: 'section-title', text: title }), sub ? el('p', { class: 'section-sub', text: sub }) : null), tools), body);
}
const skeletonCards = n => Array.from({ length: n }, () => el('div', { class: 'card skeleton', 'aria-hidden': 'true' }, el('span', { class: 'art shimmer' }), el('span', { class: 'sk' }), el('span', { class: 'sk short' })));
const skeletonRows = n => Array.from({ length: n }, () => el('li', { class: 'sk-row', 'aria-hidden': 'true' }));
function emptyState(title, text, label, href) { return el('div', { class: 'empty' }, el('h2', { text: title }), el('p', { text }), label ? el('a', { class: 'btn primary', href, text: label }) : null); }
function iconBtn(name, label, fn) { const b = el('button', { class: 'icon-round', type: 'button', 'aria-label': label, title: label }, icon(name)); b.addEventListener('click', () => fn(b)); return b; }
function eq() { return el('span', { class: 'eq', 'aria-hidden': 'true' }, el('i'), el('i'), el('i')); }
const playingFeed = () => S.current ? S.current.show.feedUrl : null;

/* candidates: shows that could be added (curated picks or directory results) */
function candFromItem(item) {
  const r = resolved[item.key];
  return { key: item.key, item, title: item.title, author: (r && r.author) || item.author || '', why: item.why || '', image: (r && r.image) || '', feedUrl: (r && r.feedUrl) || '', appleId: r && r.appleId, genre: r && r.genre, cat: item.cat, preset: PRESET[item.key] ? item.key : null };
}
const candFromResult = (r, cat) => ({ key: 'f-' + fnv(r.feedUrl), title: r.title, author: r.author, why: r.genre || '', image: r.image, feedUrl: r.feedUrl, appleId: r.appleId, genre: r.genre, cat: cat || 'mine', preset: null });
const candFromShow = s => ({ key: 'l-' + s.sid, title: s.title, author: s.author, why: '', image: s.image, feedUrl: s.feedUrl, cat: s.cat, preset: s.preset, sid: s.sid });
const libFor = c => (c.feedUrl && showByFeed(c.feedUrl)) || (c.preset && library.find(s => s.preset === c.preset)) || null;
async function ensureFeed(c, pri) {
  if (c.feedUrl) return true;
  if (!c.item) return false;
  try {
    const r = await resolveItem(c.item, pri);
    if (r && r.feedUrl) { Object.assign(c, { feedUrl: r.feedUrl, image: r.image || c.image, author: r.author || c.author, appleId: r.appleId, genre: r.genre, site: r.site || '', source: r.source || '' }); return true; }
  } catch (e) { if (e.status === 429) toast(e.message, true); }
  return false;
}
let io = null;
function observe(node) {
  if (!('IntersectionObserver' in window)) { const f = node._resolve; node._resolve = null; if (f) f(); return; }
  if (!io) io = new IntersectionObserver(entries => {
    for (const e of entries) if (e.isIntersecting) { io.unobserve(e.target); const f = e.target._resolve; e.target._resolve = null; if (f) f(); }
  }, { root: $('main'), rootMargin: '160px' });
  io.observe(node);
}
function card(c) {
  const node = el('div', { class: 'card', dataset: { key: c.key } });
  const body = el('button', { class: 'card-body', type: 'button', 'aria-label': `Open ${c.title}` },
    art(c.image, c.title, { pending: !!c.item && !c.feedUrl && !c.failed }),
    el('span', { class: 'card-title', text: c.title }),
    el('span', { class: 'card-sub', translate: 'no', text: c.author || '\u00a0' }),
    c.why ? el('span', { class: 'card-why', text: c.why }) : null);
  body.addEventListener('click', () => openCandidate(c, body.querySelector('.art')));
  const add = el('button', { class: 'add-btn', type: 'button' });
  const sync = () => {
    const has = !!libFor(c);
    add.classList.toggle('added', has);
    add.replaceChildren(icon(has ? 'check' : 'plus'));
    add.setAttribute('aria-label', has ? `${c.title} is in your library` : `Add ${c.title} to your library`);
    add.title = has ? 'In your library' : 'Add to library';
  };
  sync();
  node._sync = sync;
  add.addEventListener('click', async e => {
    e.stopPropagation();
    const lib = libFor(c);
    if (lib) { go('show/' + lib.sid); return; }
    add.classList.add('busy');
    const ok = await ensureFeed(c, 1);
    add.classList.remove('busy');
    if (!ok) { toast(`Couldn’t find a public feed for ${c.title}.`, true); return; }
    addShow(c);
  });
  node.append(body, add);
  if (c.item && !c.feedUrl && !c.failed) {
    node._resolve = () => ensureFeed(c, 5).then(ok => {
      if (!node.isConnected) return;
      if (ok) node.replaceWith(card(c));
      else { c.failed = true; node.classList.add('unavailable'); const a = node.querySelector('.art'); if (a) a.classList.remove('shimmer'); }
    });
    observe(node);
  }
  return node;
}
const cardRow = cands => { const row = el('div', { class: 'row' }, cands.map(card)); animateItems(row.children); return row; };
async function openCandidate(c, artEl) {
  const lib = libFor(c);
  if (lib) { transitionTo('show/' + lib.sid, artEl); return; }
  if (!(await ensureFeed(c, 1))) { toast(`Couldn’t find a public feed for ${c.title}.`, true); return; }
  const again = libFor(c);
  if (again) { transitionTo('show/' + again.sid, artEl); return; }
  const sid = 't-' + fnv(c.feedUrl);
  transient.set(sid, { sid, preset: c.preset, cat: c.cat || 'mine', title: c.title, author: c.author, image: c.image, feedUrl: c.feedUrl, status: 'ready', appleId: c.appleId, genre: c.genre, why: c.why, transient: true });
  transitionTo('show/' + sid, artEl && artEl.isConnected ? artEl : null);
}
function addShow(c, quiet) {
  const existing = libFor(c);
  if (existing) return existing;
  const sidWanted = c.preset ? 'p-' + c.preset : 'u-' + fnv(c.feedUrl);
  const s = { sid: library.some(x => x.sid === sidWanted) ? 'u-' + fnv(c.feedUrl) : sidWanted, preset: c.preset || null, cat: c.cat || 'mine', title: c.title, author: c.author || '', image: c.image || '', feedUrl: c.feedUrl, status: 'ready', appleId: c.appleId || null, genre: c.genre || '', description: c.description || '', link: c.link || '', site: c.site || '', source: c.source || (String(c.feedUrl).startsWith('web:') ? 'web' : 'rss'), addedAt: Date.now() };
  library.push(s);
  saveLibrary();
  renderSidebar();
  for (const n of document.querySelectorAll('.card')) if (n._sync) n._sync();
  if (!quiet) toast(`Added ${s.title} to your library.`);
  return s;
}
function removeShow(s) {
  if (!confirm(tr(`Remove “${s.title}” from your library?`))) return;
  library = library.filter(x => x !== s);
  saveLibrary();
  idb.del('eps:' + s.feedUrl);
  renderSidebar();
  toast(`Removed ${s.title}.`);
  go('library');
}
function showTile(s) {
  const status = s.status === 'missing' ? 'Not found automatically' : s.status === 'pending' ? 'Finding feed…' : (s.author || '');
  const t = el('div', { class: 'tile' + (s.status === 'missing' ? ' missing' : ''), dataset: { sid: s.sid } },
    el('a', { class: 'tile-link', href: '#/show/' + s.sid }, art(s.image, s.title, { pending: s.status === 'pending' }), el('span', { class: 'card-title', text: s.title }), el('span', Object.assign({ class: 'card-sub', text: status }, status === s.author ? { translate: 'no' } : {}))));
  if (s.feedUrl && s.feedUrl === playingFeed()) t.append(el('span', { class: 'tile-eq' }, eq()));
  return t;
}

/* episodes */
function playFrom(ep, show, queue) {
  if (S.current && S.current.ep.id === ep.id) { togglePlay(); return; }
  loadEpisode(ep, show, queue, true);
}
function paintPill(p) {
  const ep = p._ep, pr = progress[ep.id], isCur = !!(S.current && S.current.ep.id === ep.id), playing = isCur && !audio.paused;
  p.className = 'pill' + (isCur ? ' current' : pr && pr.done ? ' done' : '');
  const kids = [];
  if (playing) { kids.push(eq(), el('span', { text: 'Playing' })); p.setAttribute('aria-label', `Pause ${ep.title}`); }
  else if (pr && pr.done && !isCur) { kids.push(icon('check'), el('span', { text: 'Played' })); p.setAttribute('aria-label', `Play ${ep.title} again`); }
  else {
    kids.push(icon('play'));
    const left = pr && pr.p > 30 && pr.d ? pr.d - pr.p : 0;
    if (left) kids.push(el('span', { text: `${fmtDur(left)} left` }), el('span', { class: 'pill-prog' }, el('i', { style: `width:${Math.min(100, pr.p / pr.d * 100)}%` })));
    else kids.push(el('span', { text: isCur ? 'Resume' : (fmtDur(ep.duration) || 'Play') }));
    p.setAttribute('aria-label', `Play ${ep.title}`);
  }
  p.replaceChildren(...kids);
}
function paintAllPills() { for (const p of document.querySelectorAll('.pill')) if (p._ep) paintPill(p); }
function episodeRow(ep, show, queue, opts = {}) {
  const li = el('li', { class: 'ep' + (opts.rich ? ' rich' : ''), dataset: { id: ep.id } });
  const pill = el('button', { class: 'pill', type: 'button' });
  pill._ep = ep;
  paintPill(pill);
  pill.addEventListener('click', e => { e.stopPropagation(); playFrom(ep, show, queue); });
  const more = ep.desc && ep.desc.length > 180 ? el('button', { class: 'link-btn', type: 'button', text: 'More' }) : null;
  if (more) more.addEventListener('click', e => { e.stopPropagation(); more.textContent = li.classList.toggle('open') ? 'Less' : 'More'; });
  if (opts.rich) li.append(art(ep.image || show.image, show.title, { cls: 'ep-art' }));
  li.append(el('div', { class: 'ep-body' },
    el('p', { class: 'ep-date' },
      opts.rich ? el('a', { class: 'ep-show', href: '#/show/' + ((showByFeed(show.feedUrl) || show).sid), text: show.title }) : null,
      el('span', { text: fmtDate(ep.date) }),
      ep.number && !opts.rich ? el('span', { text: `Episode ${ep.number}` }) : null),
    el('h3', { class: 'ep-title', text: ep.title }),
    ep.desc ? el('p', { class: 'ep-desc', text: ep.desc }) : null,
    el('div', { class: 'ep-actions' }, pill, ep.transcripts && ep.transcripts.length ? el('span', { class: 'badge', title: 'Captions available', text: 'CC' }) : null, more)));
  li.addEventListener('click', e => { if (e.target.closest('a, button') || String(getSelection())) return; playFrom(ep, show, queue); });
  return li;
}
function upNextCard(r, queue) {
  const pr = progress[r.ep.id];
  const pct = pr && pr.d ? Math.min(100, pr.p / pr.d * 100) : 0;
  const b = el('button', { class: 'up-card', type: 'button', 'aria-label': `Resume ${r.ep.title}` },
    art(r.ep.image || r.show.image, r.show.title, { cls: 'up-art' }),
    el('span', { class: 'up-text' }, el('span', { class: 'up-show', text: r.show.title }), el('span', { class: 'up-title', text: r.ep.title }),
      el('span', { class: 'up-left' }, el('span', { class: 'pill-prog' }, el('i', { style: `width:${pct}%` })), el('span', { text: pr && pr.d ? `${fmtDur(pr.d - pr.p)} left` : fmtDur(r.ep.duration) }))));
  b.addEventListener('click', () => playFrom(r.ep, r.show, queue));
  return b;
}

/* ---------- motion (Fluent-style) ---------- */
function animateItems(nodes, step = 28) {
  if (!motionOn()) return;
  let i = 0;
  for (const n of nodes) {
    if (!n.animate) continue;
    n.animate([{ opacity: 0, transform: 'translateY(16px)' }, { opacity: 1, transform: 'none' }],
      { duration: 460, delay: Math.min(i, 16) * step, easing: 'cubic-bezier(0.1, 0.9, 0.2, 1)', fill: 'backwards' });
    i++;
  }
}
function moveIndicators() {
  const place = (ind, link, vertical) => {
    if (!ind) return;
    if (!link || !link.offsetParent) { ind.classList.remove('on'); return; }
    if (vertical) { ind.style.setProperty('--y', (link.offsetTop + link.offsetHeight / 2 - 9) + 'px'); ind.style.setProperty('--x', link.offsetLeft + 'px'); }
    else ind.style.setProperty('--x', (link.offsetLeft + link.offsetWidth / 2 - 14) + 'px');
    if (!ind.classList.contains('on')) { ind.classList.add('instant'); void ind.offsetWidth; ind.classList.add('on'); requestAnimationFrame(() => ind.classList.remove('instant')); }
  };
  place($('navInd'), document.querySelector('.sidebar [data-route][aria-current="page"]'), true);
  place($('tabInd'), document.querySelector('.tabbar [aria-current="page"]'), false);
}
// connected animation: the artwork you pressed flies into the show page (where the browser supports it)
function transitionTo(path, artEl) {
  if (!artEl || !motionOn() || !document.startViewTransition) { go(path); return; }
  artEl.style.viewTransitionName = 'show-art';
  S.flyArt = true;
  const vt = document.startViewTransition(() => { artEl.style.viewTransitionName = ''; history.pushState(null, '', '#/' + path); render(); });
  vt.finished.finally(() => { S.flyArt = false; const h = document.querySelector('.hero-art'); if (h) h.style.viewTransitionName = ''; });
}
// the Now Playing artwork flies from (or back to) the player bar
function flyArt(from, to, back) {
  if (!motionOn() || !from || !to || !to.animate) return;
  const a = from.getBoundingClientRect(), b = to.getBoundingClientRect();
  if (!a.width || !b.width) return;
  const moved = { transform: `translate(${a.left - b.left}px, ${a.top - b.top}px) scale(${a.width / b.width})`, borderRadius: '10px' };
  const home = { transform: 'none', borderRadius: '14px' };
  to.style.transformOrigin = '0 0';
  const anim = to.animate(back ? [home, moved] : [moved, home], back
    ? { duration: 240, easing: 'cubic-bezier(0.9, 0.1, 1, 0.2)', fill: 'forwards' }
    : { duration: 560, easing: 'cubic-bezier(0.1, 0.9, 0.2, 1)' });
  if (!back) anim.finished.then(() => { to.style.transformOrigin = ''; }, () => {});
}
/* ---------- routing ---------- */
function parseRoute() { const h = location.hash.replace(/^#\/?/, ''); const [name, ...rest] = h.split('/'); return { name: name || 'home', arg: decodeURIComponent(rest.join('/')) }; }
function go(path) { if (location.hash === '#/' + path) render(); else location.hash = '#/' + path; }
let renderSeq = 0;
function render() {
  renderSeq++;
  route = parseRoute();
  closeSpeed();
  if (S.npOpen) closeNp();
  view.replaceChildren();
  view.classList.toggle('drill', route.name === 'show');
  view.style.animation = 'none'; void view.offsetWidth; view.style.animation = S.flyArt ? 'none' : '';
  const fn = { home: renderHome, discover: renderDiscover, library: renderLibrary, show: renderShow, search: renderSearch, settings: renderSettings, move: renderMove }[route.name] || renderHome;
  fn(route.arg);
  $('main').scrollTop = 0;
  markNav();
}
function markNav() {
  const top = route.name === 'show' ? '' : route.name;
  for (const a of document.querySelectorAll('[data-route]')) a.setAttribute('aria-current', a.dataset.route === top ? 'page' : 'false');
  renderSidebar();
  moveIndicators();
}
function renderSidebar() {
  const ul = $('sideShows');
  const cur = route.name === 'show' ? route.arg : null;
  const playing = playingFeed();
  ul.replaceChildren(...library.map(s => el('li', {}, el('a', { href: '#/show/' + s.sid, 'aria-current': s.sid === cur ? 'page' : null, class: s.status !== 'ready' ? 'dim' : null, title: s.title, translate: 'no' },
    art(s.image, s.title, { cls: 'side-art', eager: true }), el('span', { class: 't', text: s.title }), s.feedUrl && s.feedUrl === playing ? eq() : null))));
}

/* ---------- views ---------- */
async function renderMove() {
  view.append(pageHead('Moving your library', null));
  const note = el('p', { class: 'move-note', text: 'Sending your shows, progress and settings to the Afterhours window…' });
  view.append(note);
  const data = {};
  for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (k && k.startsWith('ah.') && k !== 'ah.moveOffer') data[k] = localStorage.getItem(k); }
  try {
    const r = await fetch('/api/move', { method: 'POST', headers: { 'X-Afterhours': '1', 'Content-Type': 'application/json' }, body: JSON.stringify(data) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'Afterhours’ helper isn’t answering. Is afterhours.py still running?');
    note.textContent = 'Done. Your library is in the Afterhours window now, so you can close this tab.';
  } catch (e) { note.textContent = e.message; }
}
function importMove(data) {
  IMPORTING = true;
  for (const [k, v] of Object.entries(data)) if (k.startsWith('ah.') && typeof v === 'string') localStorage.setItem(k, v);
  localStorage.removeItem('ah.moveOffer');
  toast('Your library is here.');
  setTimeout(() => location.reload(), 700);
}
async function startMove() {
  toast('Opening your browser to collect your library…');
  try { await postJSON('/api/move/open'); } catch (e) { toast(e.message, true); return; }
  for (const until = Date.now() + 120000; Date.now() < until;) {
    await sleep(1500);
    let j = null;
    try { j = await getJSON('/api/move'); } catch { continue; }
    if (j && j.data) { importMove(j.data); return; }
  }
  toast('Nothing arrived from your browser. Try again, or use Export and Import OPML.', true);
}
function moveCard() {
  const card = el('section', { class: 'move-card' },
    el('div', { class: 'move-text' }, el('h2', { text: 'Used Afterhours in your browser before?' }),
      el('p', { text: 'Bring your shows, listening progress and settings into this window. Your browser opens for a moment to hand them over.' })),
    el('div', { class: 'set-actions' },
      el('button', { class: 'btn primary small', type: 'button', text: 'Bring them over', onclick: startMove }),
      el('button', { class: 'btn tonal small', type: 'button', text: 'Not now', onclick: () => { localStorage.removeItem('ah.moveOffer'); card.remove(); } })));
  return card;
}
function renderHome() {
  const h = new Date().getHours();
  const greet = h < 5 ? 'Up late' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  view.append(pageHead('Listen Now', `${greet}. Here’s what’s new on your shelf.`, gearLink()));
  if (IN_WINDOW && localStorage.getItem('ah.moveOffer')) view.append(moveCard());
  if (!IN_WINDOW && !localStorage.getItem('ah.windowOfferOff')) offerWindow();
  view.append(el('div', { id: 'setupBanner' }));
  updateSetupUI();
  const up = recent.filter(r => { const p = progress[r.ep.id]; return p && !p.done && p.p > 10; }).slice(0, 12);
  if (up.length) {
    const q = up.map(r => ({ ep: r.ep, show: r.show }));
    view.append(section('Up Next', 'Pick up where you left off.', el('div', { class: 'row wide' }, up.map(r => upNextCard(r, q)))));
  }
  view.append(section('Latest episodes', 'New from the shows on your shelf.', el('div', {}, el('p', { class: 'feed-status', id: 'feedStatus' }), el('ol', { class: 'eps', id: 'homeLatest' }, skeletonRows(3)))));
  fillLatest();
  const picks = CATS.map(c => CATALOG.extras.find(x => x.cat === c.id)).filter(Boolean);
  view.append(section('Picked for you', 'Related shows that sit well next to your shelf.', cardRow(picks.map(candFromItem)), { link: ['See all', '#/discover'] }));
  if (!Setup.running) refreshAllFeeds();
}
async function fillLatest() {
  const box = $('homeLatest');
  if (!box) return;
  const shows = library.filter(s => s.status === 'ready' && s.feedUrl);
  const lists = await Promise.all(shows.map(async s => ({ s, c: await Feeds.cached(s) })));
  if (!box.isConnected) return;
  const all = [];
  for (const { s, c } of lists) if (c) for (const ep of c.eps.slice(0, 3)) all.push({ ep, show: snap(s) });
  all.sort((a, b) => b.ep.date - a.ep.date);
  const top = all.slice(0, 24);
  if (!top.length) {
    if (!box.querySelector('.sk-row')) box.replaceChildren(el('li', { class: 'list-note', text: Setup.running || refreshingAll ? 'Gathering the latest episodes…' : 'No episodes yet. Open a show to load it.' }));
    return;
  }
  box.replaceChildren(...top.map(x => episodeRow(x.ep, x.show, top, { rich: true })));
  if (!box.dataset.anim) { box.dataset.anim = '1'; animateItems(box.children, 24); }
}
function favouriteCats(n) {
  const counts = new Map();
  for (const id of Object.keys(progress)) { const pre = id.split(':')[0]; counts.set(pre, (counts.get(pre) || 0) + 1); }
  const ranked = library.filter(s => s.feedUrl && counts.get(fnv(s.feedUrl))).sort((a, b) => counts.get(fnv(b.feedUrl)) - counts.get(fnv(a.feedUrl)));
  const out = [];
  for (const s of ranked) if (CAT[s.cat] && !out.some(o => o.cat === s.cat)) { out.push({ cat: s.cat, show: s }); if (out.length >= n) return out; }
  for (const cat of ['citypop', 'mystery', 'knowledge']) if (!out.some(o => o.cat === cat)) { out.push({ cat, show: null }); if (out.length >= n) break; }
  return out;
}
function searchRow(term, country, cat) {
  const row = el('div', { class: 'row' }, skeletonCards(6));
  dirSearch(term, country, 4, 20).then(rs => {
    if (!row.isConnected) return;
    const list = rs.filter(r => !showByFeed(r.feedUrl)).slice(0, 14).map(r => card(candFromResult(r, cat)));
    row.replaceChildren(...(list.length ? list : [el('p', { class: 'row-empty', text: 'Nothing new here right now.' })]));
    animateItems(row.children);
  }).catch(e => {
    if (!row.isConnected) return;
    row.replaceChildren(el('p', { class: 'row-empty' }, e.message + ' ', el('button', { class: 'link-btn', type: 'button', text: 'Try again', onclick: () => row.replaceWith(searchRow(term, country, cat)) })));
  });
  return row;
}
function renderDiscover(tab) {
  tab = tab || 'for-you';
  view.append(pageHead('Discover', 'Related shows, picked around what you love.'));
  const tabs = [['for-you', 'For you'], ...CATS.map(c => [c.id, c.short]), ['ja', '日本語'], ['zh', '中文']];
  view.append(el('div', { class: 'chips', role: 'tablist', 'aria-label': 'Topics' }, tabs.map(([id, label]) => el('a', { class: 'chip', role: 'tab', href: '#/discover/' + id, 'aria-selected': String(id === tab), text: label }))));
  const items = [...CATALOG.presets, ...CATALOG.extras];
  if (tab === 'for-you') {
    const arts = library.filter(s => s.image).slice(0, 3);
    view.append(el('div', { class: 'hero' },
      el('div', {}, el('p', { class: 'hero-kicker', text: 'Made for your evenings' }), el('h2', { class: 'hero-title', text: 'City pop, slow jazz, good books and big ideas' }), el('p', { class: 'hero-sub', text: 'Related shows for everything on your shelf. Press + to add one, or open it to preview its episodes first.' })),
      el('div', { class: 'hero-stack', 'aria-hidden': 'true' }, arts.map((s, i) => art(s.image, s.title, { cls: 's' + i, eager: true })))));
    for (const f of favouriteCats(2)) {
      const [term, country] = CAT[f.cat].keywords[0];
      view.append(section(f.show ? `Because you listen to ${f.show.title}` : `Because you like ${CAT[f.cat].short.toLowerCase()}`, f.show ? `More about ${term}.` : 'From the interests you told me about.', searchRow(term, country, f.cat)));
    }
    for (const c of CATS) view.append(section(`More ${c.label.charAt(0).toLowerCase() + c.label.slice(1)}`, null, cardRow(CATALOG.extras.filter(x => x.cat === c.id).map(candFromItem)), { link: ['See all', '#/discover/' + c.id] }));
    const removed = CATALOG.presets.filter(p => !library.some(s => s.preset === p.key));
    if (removed.length) view.append(section('From your poster', 'Shows you removed from your library.', cardRow(removed.map(candFromItem))));
    return;
  }
  if (tab === 'ja' || tab === 'zh') {
    const picks = items.filter(x => x.lang === tab);
    if (picks.length) view.append(section(tab === 'ja' ? 'Japanese picks' : 'Chinese picks', null, cardRow(picks.map(p => { const lib = library.find(s => s.preset === p.key); return lib ? candFromShow(lib) : candFromItem(p); }))));
    for (const [term, country] of CATALOG.langs[tab].keywords) view.append(section(term, null, searchRow(term, country, 'mine')));
    return;
  }
  const c = CAT[tab];
  if (!c) { go('discover'); return; }
  view.append(section(c.label, 'Hand-picked to sit next to your shelf.', cardRow(CATALOG.extras.filter(x => x.cat === c.id).map(candFromItem))));
  const shelf = library.filter(s => s.cat === c.id && s.status === 'ready');
  if (shelf.length) view.append(section('Already on your shelf', null, cardRow(shelf.map(candFromShow))));
  for (const [term, country] of c.keywords) view.append(section(`Shows about “${term}”`, null, searchRow(term, country, c.id)));
}
function renderLibrary() {
  const input = el('input', { type: 'text', placeholder: 'Paste a feed or Apple Podcasts link', 'aria-label': 'Add a show by link', spellcheck: 'false' });
  const form = el('form', { class: 'inline-add', autocomplete: 'off' }, input, el('button', { class: 'btn tonal small', type: 'submit', text: 'Add' }));
  form.addEventListener('submit', e => { e.preventDefault(); if (input.value.trim()) handleQuery(input.value.trim()); });
  view.append(pageHead('Library', `${library.length} shows`, el('div', { class: 'head-tools' }, form, gearLink())));
  if (!library.length) { view.append(emptyState('Your library is empty', 'Find shows in Discover, or search by name.', 'Open Discover', '#/discover')); return; }
  for (const c of [...CATS, { id: 'mine', label: 'Your additions' }]) {
    const shows = library.filter(s => s.cat === c.id);
    if (shows.length) view.append(section(c.label, null, el('div', { class: 'grid' }, shows.map(showTile))));
  }
  animateItems(view.querySelectorAll('.tile'), 22);
}
function sourcePanel(show, onDone) {
  const input = el('input', { type: 'text', inputmode: 'url', placeholder: 'Paste the show’s RSS feed or website', 'aria-label': 'RSS feed or website address', spellcheck: 'false', value: show.site || show.link || '' });
  const btn = el('button', { class: 'btn primary small', type: 'submit', text: 'Use this source' });
  const note = el('p', { class: 'source-note', text: 'Afterhours reads the RSS feed. If there isn’t one, it looks through the website for episodes.' });
  const form = el('form', { class: 'source-form', autocomplete: 'off' }, el('div', { class: 'source-row' }, input, btn), note);
  form.addEventListener('submit', async e => {
    e.preventDefault();
    if (!input.value.trim()) return;
    btn.disabled = true;
    note.textContent = 'Checking that address. A website can take up to half a minute to look through…';
    try { onDone(await Feeds.useSource(show, input.value)); }
    catch (err) { note.textContent = err.message; }
    finally { btn.disabled = false; }
  });
  return form;
}
async function renderShow(sid) {
  const show = showBySid(sid);
  if (!show) { view.append(emptyState('This show isn’t in your library', 'It may have been removed.', 'Open Library', '#/library')); return; }
  const inLib = library.includes(show);
  const preset = show.preset ? PRESET[show.preset] : null;
  const actions = el('div', { class: 'show-actions' });
  const srcBox = el('div', { class: 'source-box', hidden: true });
  const srcNote = el('p', { class: 'show-source', hidden: show.source !== 'web', text: 'No working RSS feed, so these episodes come from the show’s website.' });
  const about = el('p', { class: 'show-about', text: show.description || show.why || (preset && preset.why) || '' });
  const hero = el('header', { class: 'show-hero' },
    art(show.image, show.title, { cls: 'hero-art', pending: show.status === 'pending', eager: true }),
    el('div', { class: 'show-info' },
      el('p', { class: 'show-kicker', text: (CAT[show.cat] && CAT[show.cat].label) || (show.transient ? 'Preview' : 'Your additions') }),
      el('h1', { class: 'show-title', text: show.title }),
      show.author ? el('p', { class: 'show-author', text: show.author }) : null,
      about, actions, srcNote, srcBox));
  view.append(hero);
  if (S.flyArt) { const h = hero.querySelector('.hero-art'); if (h) h.style.viewTransitionName = 'show-art'; }
  artColor(show.image).then(c => applyTint(hero, c));
  const list = el('ol', { class: 'eps' });
  const note = el('p', { class: 'list-note', hidden: true });
  const sortLabel = () => settings.sort === 'oldest' ? 'Oldest first' : 'Newest first';
  let eps = [], shown = 60;
  const still = () => route.name === 'show' && route.arg === sid && list.isConnected;
  const openSource = () => {
    if (!srcBox.hidden) { srcBox.hidden = true; return; }
    srcBox.replaceChildren(sourcePanel(show, fresh => { toast(`${show.title} now has ${fresh.length} episodes.`); render(); }));
    srcBox.hidden = false;
    srcBox.querySelector('input').focus();
  };
  const paint = () => {
    const sorted = settings.sort === 'oldest' ? [...eps].reverse() : eps;
    const sh = snap(show);
    const queue = sorted.map(e => ({ ep: e, show: sh }));
    list.replaceChildren(...sorted.slice(0, shown).map(e => episodeRow(e, sh, queue)));
    if (!list.dataset.anim) { list.dataset.anim = '1'; animateItems(list.children, 30); }
    if (sorted.length > shown) list.append(el('li', { class: 'more-row' }, el('button', { class: 'btn tonal', type: 'button', text: `Show more episodes (${sorted.length - shown})`, onclick: () => { shown += 60; paint(); } })));
    note.hidden = sorted.length > 0;
    if (!sorted.length) note.textContent = 'This show has no playable episodes.';
    srcNote.hidden = show.source !== 'web';
  };
  const sortBtn = el('button', { class: 'btn tonal small', type: 'button' }, icon('sort'), el('span', { text: sortLabel() }));
  sortBtn.addEventListener('click', () => { settings.sort = settings.sort === 'oldest' ? 'newest' : 'oldest'; saveSettings(); sortBtn.lastChild.textContent = sortLabel(); paint(); });
  view.append(el('div', { class: 'eps-head' }, el('h2', { class: 'section-title', text: 'Episodes' }), sortBtn), note, list);

  const playLatest = el('button', { class: inLib ? 'btn primary' : 'btn tonal', type: 'button' }, icon('play'), el('span', { text: 'Latest episode' }));
  playLatest.addEventListener('click', () => { if (eps.length) { const sh = snap(show); playFrom(eps[0], sh, eps.map(e => ({ ep: e, show: sh }))); } });
  if (inLib) {
    actions.append(playLatest, iconBtn('refresh', 'Check for new episodes', async b => {
      b.classList.add('spin');
      try { eps = await Feeds.refresh(show); if (still()) paint(); toast('This show is up to date.'); } catch (e) { toast(`Couldn’t refresh. ${e.message}`, true); }
      b.classList.remove('spin');
    }), iconBtn('swap', 'Change source', openSource));
    if (/^https?:\/\//i.test(show.link || '')) actions.append(el('a', { class: 'icon-round', href: show.link, target: '_blank', rel: 'noopener noreferrer', 'aria-label': 'Website', title: 'Website' }, icon('link')));
    actions.append(iconBtn('trash', 'Remove from library', () => removeShow(show)));
  } else {
    const addBtn = el('button', { class: 'btn primary', type: 'button' }, icon('plus'), el('span', { text: 'Add to Library' }));
    addBtn.addEventListener('click', () => { const s = addShow(show); transient.delete(sid); history.replaceState(null, '', '#/show/' + s.sid); render(); });
    actions.append(addBtn, playLatest);
  }
  const failed = msg => {
    list.replaceChildren();
    note.hidden = false;
    note.replaceChildren(msg + ' ', el('button', { class: 'link-btn', type: 'button', text: 'Change source', onclick: openSource }), ' or ',
      el('a', { class: 'link-btn', href: '#/search/' + encodeURIComponent(show.title), text: 'search for it' }), '.');
  };
  if (show.status === 'pending') {
    note.hidden = false;
    note.textContent = 'Finding this show’s feed…';
    list.append(...skeletonRows(4));
    const wait = setInterval(() => { if (!still()) { clearInterval(wait); return; } if (show.status !== 'pending') { clearInterval(wait); render(); } }, 800);
    return;
  }
  if (show.status === 'missing') {
    failed('Afterhours couldn’t find this show’s feed in Apple’s directory or on its website.');
    openSource();
    return;
  }
  list.append(...skeletonRows(5));
  const cached = await Feeds.cached(show);
  if (cached && still()) { eps = cached.eps; paint(); }
  if (!cached || Date.now() - cached.t > 30 * 60e3) {
    const slow = setTimeout(() => { if (still() && !eps.length) { note.hidden = false; note.textContent = 'Still loading. If the feed doesn’t answer, Afterhours will look for episodes on the show’s website.'; } }, 5000);
    try { eps = await Feeds.refresh(show); if (still()) { note.hidden = true; paint(); } }
    catch (e) { if (still() && !eps.length) failed(`Couldn’t load episodes. ${e.message}`); }
    finally { clearTimeout(slow); }
  }
  if (still() && !about.textContent && show.description) about.textContent = show.description;
}
const gearLink = () => el('a', { class: 'icon-round phone-only', href: '#/settings', 'aria-label': 'Settings', title: 'Settings' }, icon('gear'));
function segmented(options, value, onChange, label) {
  const box = el('div', { class: 'seg', role: 'radiogroup', 'aria-label': label });
  const thumb = el('span', { class: 'seg-thumb', 'aria-hidden': 'true' });
  const btns = [];
  const place = instant => {
    const b = btns.find(x => x.getAttribute('aria-checked') === 'true');
    if (!b || !b.offsetWidth) return;
    thumb.classList.toggle('instant', !!instant);
    Object.assign(thumb.style, { width: b.offsetWidth + 'px', height: b.offsetHeight + 'px', transform: `translate(${b.offsetLeft}px, ${b.offsetTop}px)` });
    thumb.classList.add('on');
  };
  const paint = v => { for (const b of btns) b.setAttribute('aria-checked', String(b.dataset.v === v)); place(false); };
  box.append(thumb);
  for (const [v, text] of options) {
    const b = el('button', { class: 'seg-btn', type: 'button', role: 'radio', dataset: { v }, text, 'aria-checked': String(v === value) });
    b.addEventListener('click', () => { paint(v); onChange(v); });
    btns.push(b);
    box.append(b);
  }
  requestAnimationFrame(() => place(true));
  if ('ResizeObserver' in window) new ResizeObserver(() => place(true)).observe(box);
  return box;
}
function retryMissing() {
  let n = 0;
  for (const s of library) if (s.status === 'missing' && s.preset) { s.status = 'pending'; delete resolved[s.preset]; n++; }
  store.set(KEY.resolved, resolved);
  saveLibrary();
  toast(n ? `Looking for ${n} show${n > 1 ? 's' : ''} again…` : 'Nothing is missing.');
  runSetup();
  render();
}
async function clearAutoCaptions() {
  if (!confirm(tr('Delete the auto captions saved on this computer? They’ll be made again when you next play those episodes.'))) return;
  try { await postJSON('/api/captions/clear'); } catch {}
  await idb.delPrefix('asr:');
  toast('Saved auto captions deleted.');
}
const ASR_DEFAULTS = { model: 'auto', language: 'auto', task: 'transcribe', device: 'auto', compute: 'auto', threads: 0, beam: 1, temperature: 0,
  condition: false, vad: true, vadThreshold: 0.5, minSilence: 500, noSpeech: 0.6, compression: 2.4, logProb: -1, words: true, maxChars: 84, maxSeconds: 6.5, prompt: '', hotwords: '' };
const asrGet = k => (settings.asr && settings.asr[k] !== undefined ? settings.asr[k] : ASR_DEFAULTS[k]);
function asrSet(k, v) { settings.asr = Object.assign({}, settings.asr, { [k]: v }); saveSettings(); }
function proOpts() {
  const o = {};
  for (const k of Object.keys(ASR_DEFAULTS)) if (k !== 'model') o[k] = asrGet(k);
  if (o.language === 'auto') o.language = '';
  return o;
}
const setRow = (title, sub, control) => el('div', { class: 'set-row' }, el('div', { class: 'set-text' }, el('span', { class: 'set-title', text: title }), sub ? el('span', { class: 'set-sub' }, sub) : null), control || null);
const setGroup = (title, ...rows) => el('section', { class: 'set-group' }, el('h2', { class: 'set-head', text: title }), el('div', { class: 'set-card' }, rows));
function selectInput(options, value, onChange, label) {
  const sel = el('select', { class: 'sel', 'aria-label': label });
  for (const [v, text] of options) sel.append(el('option', { value: v, text }));
  sel.value = value;
  sel.addEventListener('change', () => onChange(sel.value));
  return sel;
}
function numberInput(value, min, max, step, onChange, label) {
  const i = el('input', { type: 'number', class: 'num', min, max, step, value: String(value), 'aria-label': label, inputmode: 'decimal' });
  i.addEventListener('change', () => { let v = parseFloat(i.value); if (!isFinite(v)) v = value; v = Math.min(max, Math.max(min, v)); i.value = String(v); onChange(v); });
  return i;
}
function textInput(value, placeholder, onChange, label) {
  const i = el('input', { type: 'text', class: 'txt', value, placeholder, 'aria-label': label, spellcheck: 'false', autocomplete: 'off' });
  i.addEventListener('change', () => onChange(i.value.trim()));
  return i;
}
function asrToggle(k, label, on, off) {
  const i = el('input', { type: 'checkbox', class: 'switch', role: 'switch', 'aria-label': label });
  i.checked = on ? asrGet(k) === on : !!asrGet(k);
  i.addEventListener('change', () => asrSet(k, on ? (i.checked ? on : off) : i.checked));
  return i;
}
function proPanel() {
  const head = text => el('div', { class: 'set-subhead', text });
  const num = (k, min, max, step, label) => numberInput(asrGet(k), min, max, step, v => asrSet(k, v), label);
  const models = [['auto', 'Automatic'], ['tiny', 'tiny · 75 MB'], ['tiny.en', 'tiny.en · 75 MB'], ['base', 'base · 145 MB'], ['base.en', 'base.en · 145 MB'],
    ['small', 'small · 480 MB'], ['small.en', 'small.en · 480 MB'], ['medium', 'medium · 1.5 GB'], ['medium.en', 'medium.en · 1.5 GB'],
    ['large-v3', 'large-v3 · 3 GB'], ['large-v3-turbo', 'large-v3-turbo · 1.6 GB'], ['distil-large-v3', 'distil-large-v3 · 1.5 GB']];
  const langs = [['auto', 'Automatic'], ['en', 'English'], ['ja', '日本語'], ['zh', '中文'], ['yue', '粵語'], ['ko', '한국어'], ['fr', 'Français'],
    ['de', 'Deutsch'], ['es', 'Español'], ['it', 'Italiano'], ['pt', 'Português'], ['ru', 'Русский']];
  return setGroup('Pro mode',
    head('Model'),
    setRow('Model', 'Bigger models are more accurate and slower. “.en” models are English-only. large-v3-turbo needs faster-whisper 1.1 or later.', selectInput(models, asrGet('model'), v => asrSet('model', v), 'Model')),
    setRow('Spoken language', 'Automatic detects it from the episode.', selectInput(langs, asrGet('language'), v => asrSet('language', v), 'Spoken language')),
    setRow('Translate into English', 'Write the captions in English, whatever language is spoken.', asrToggle('task', 'Translate into English', 'translate', 'transcribe')),
    head('Hardware'),
    setRow('Run on', 'Automatic uses a supported NVIDIA graphics card when there is one.', selectInput([['auto', 'Automatic'], ['cpu', 'Processor'], ['cuda', 'Graphics card']], asrGet('device'), v => asrSet('device', v), 'Run on')),
    setRow('Precision', 'Lower precision is faster and uses less memory.', selectInput([['auto', 'Automatic'], ['int8', 'int8'], ['int8_float16', 'int8_float16'], ['int16', 'int16'], ['float16', 'float16'], ['float32', 'float32']], asrGet('compute'), v => asrSet('compute', v), 'Precision')),
    setRow('Processor threads', '0 lets Afterhours decide.', num('threads', 0, 64, 1, 'Processor threads')),
    head('Decoding'),
    setRow('Beam size', 'How many guesses Whisper weighs at once. 1 is fastest; 5 is more careful.', num('beam', 1, 10, 1, 'Beam size')),
    setRow('Temperature', 'Above 0 adds variety. Keep it at 0 for steady results.', num('temperature', 0, 1, 0.1, 'Temperature')),
    setRow('Use previous text', 'Lets Whisper use what it just heard as context. Smoother, but it can repeat itself.', asrToggle('condition', 'Use previous text')),
    head('Silence and filtering'),
    setRow('Skip silence', 'Finds speech first, so music and silence are skipped.', asrToggle('vad', 'Skip silence')),
    setRow('Speech threshold', 'Higher means only clear speech counts.', num('vadThreshold', 0.05, 0.95, 0.05, 'Speech threshold')),
    setRow('Shortest silence (ms)', 'Pauses shorter than this don’t split speech.', num('minSilence', 50, 5000, 50, 'Shortest silence (ms)')),
    setRow('No-speech threshold', 'How sure Whisper must be that a stretch has no speech before skipping it.', num('noSpeech', 0, 1, 0.05, 'No-speech threshold')),
    setRow('Repetition limit', 'Lines that compress more than this are treated as mistakes and retried.', num('compression', 1, 10, 0.1, 'Repetition limit')),
    setRow('Confidence floor', 'Lines less confident than this are retried.', num('logProb', -10, 0, 0.1, 'Confidence floor')),
    head('Caption lines'),
    setRow('Word timing', 'Splits captions into short lines using the timing of each word.', asrToggle('words', 'Word timing')),
    setRow('Longest line (characters)', null, num('maxChars', 20, 300, 1, 'Longest line (characters)')),
    setRow('Longest line (seconds)', null, num('maxSeconds', 1, 30, 0.5, 'Longest line (seconds)')),
    head('Vocabulary'),
    setRow('Vocabulary hint', 'Names and words to expect, such as hosts, guests or places.', textInput(asrGet('prompt'), 'e.g. Agatha Christie, Hercule Poirot', v => asrSet('prompt', v), 'Vocabulary hint')),
    setRow('Hotwords', 'Words Whisper should listen out for. Needs faster-whisper 1.0 or later.', textInput(asrGet('hotwords'), '', v => asrSet('hotwords', v), 'Hotwords')),
    setRow('Pro settings apply to the next captions Afterhours makes.', null, el('div', { class: 'set-actions' },
      el('button', { class: 'btn tonal small', type: 'button', text: 'Re-caption this episode', onclick: () => { if (!S.current) { toast('Start an episode first.'); return; } Recog.start(S.current.ep); renderLyrics(); } }),
      el('button', { class: 'btn tonal small', type: 'button', text: 'Reset to defaults', onclick: () => { settings.asr = {}; saveSettings(); toast('Pro settings reset.'); render(); } }))));
}
function windowGroup() {
  const sub = el('span', { text: 'Checking…' }), ctl = el('div', { class: 'set-actions' });
  const group = setGroup('Window', setRow('Open in its own window', sub, ctl));
  const paint = p => {
    if (!group.isConnected && sub.textContent !== 'Checking…') return;
    ctl.replaceChildren();
    const setup = p.setup || {};
    if (p.native) {
      const sw = el('input', { type: 'checkbox', class: 'switch', role: 'switch', 'aria-label': 'Open in its own window' });
      sw.checked = !!p.window;
      sw.addEventListener('change', () => postJSON('/api/prefs?window=' + (sw.checked ? 1 : 0)).catch(e => toast(e.message, true)));
      if (p.running === 'window') sub.textContent = 'Afterhours is its own app here, using the web view built into your system. No browser is involved, and closing the window quits Afterhours.';
      else {
        sub.textContent = 'The window component is installed. Switch now, or Afterhours opens in its own window the next time you start it.';
        ctl.append(el('button', { class: 'btn primary small', type: 'button', text: 'Switch now', onclick: switchToWindow }));
      }
      ctl.append(sw);
    } else if (setup.state === 'working') {
      sub.textContent = setup.message || 'Installing the window component…';
      setTimeout(() => { if (group.isConnected) getJSON('/api/prefs').then(paint, () => {}); }, 1500);
    } else {
      sub.textContent = setup.state === 'error' ? setup.message : 'Afterhours can run as its own app instead of in your browser, using the web view built into your system. It needs a small window component, installed once.';
      ctl.append(el('button', { class: 'btn primary small', type: 'button', text: setup.state === 'error' ? 'Try again' : 'Install',
        onclick: () => postJSON('/api/window/setup').then(() => getJSON('/api/prefs').then(paint), e => toast(e.message, true)) }));
    }
  };
  getJSON('/api/prefs').then(paint).catch(() => { sub.textContent = ''; });
  return group;
}
function libraryData() {
  const data = {};
  for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); if (k && k.startsWith('ah.') && k !== 'ah.moveOffer') data[k] = localStorage.getItem(k); }
  return data;
}
async function switchToWindow() {
  try {
    const r = await fetch('/api/window/switch', { method: 'POST', headers: { 'X-Afterhours': '1', 'Content-Type': 'application/json' }, body: JSON.stringify(libraryData()) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'Afterhours’ helper isn’t answering. Is afterhours.py still running?');
  } catch (e) { toast(e.message, true); return; }
  $('audio').pause();
  document.body.replaceChildren(el('div', { class: 'empty', style: 'margin-top:30vh' }, el('h2', { text: 'Afterhours is opening in its own window' }), el('p', { text: 'Your library goes with it. You can close this tab.' })));
}
async function offerWindow() {
  const who = await whoami();
  if (who.client !== 'local') return;
  const p = await getJSON('/api/prefs').catch(() => null);
  if (!p || p.native || p.running !== 'browser' || route.name !== 'home' || view.querySelector('.window-card')) return;
  const card = el('section', { class: 'move-card window-card' },
    el('div', { class: 'move-text' }, el('h2', { text: 'Make Afterhours its own app' }),
      el('p', { text: 'Run it in its own window, without a browser. Afterhours installs a small window component once.' })),
    el('div', { class: 'set-actions' },
      el('button', { class: 'btn primary small', type: 'button', text: 'Set it up', onclick: () => postJSON('/api/window/setup').then(() => go('settings'), e => toast(e.message, true)) }),
      el('button', { class: 'btn tonal small', type: 'button', text: 'Not now', onclick: () => { localStorage.setItem('ah.windowOfferOff', '1'); card.remove(); } })));
  const head = view.querySelector('.page-head');
  if (head) head.after(card);
}
async function renderSettings() {
  const seq = renderSeq, who = await whoami();
  if (seq !== renderSeq || route.name !== 'settings') return;
  const local = who.client === 'local';
  view.append(pageHead('Settings', !local ? null : IN_WINDOW ? 'Afterhours runs on this computer, and your library and settings stay on it.' : 'Afterhours runs on this computer. Your library and settings stay in this browser.'));
  view.append(setGroup('Language', setRow('Language', 'Choose the language for Afterhours. Podcast titles and descriptions stay as their publishers wrote them.',
    selectInput([['system', 'System'], ['en', 'English'], ['zh-Hans', '简体中文'], ['ja', '日本語']], settings.language || 'system', v => { settings.language = v; saveSettings(); applyLanguage(); render(); }, 'Language'))));
  const status = el('span', { text: 'Checking…' });
  const proBox = el('div', { class: 'pro-box', hidden: !settings.pro }, proPanel());
  view.append(setGroup('Captions',
    setRow('Show captions', 'The current line above the player and in Now Playing.', switchInput('captions', 'Show captions', on => { settings.captions = on; saveSettings(); syncCC(); renderCaptions(true); })),
    setRow('Auto captions', 'When an episode has no captions, recognise the speech on this computer and caption it.', switchInput('autoCaptions', 'Auto captions', on => setAutoCaptions(on))),
    setRow('Recognition quality', 'Captions come from OpenAI’s Whisper speech models, running on this computer. Fast uses Whisper base, Accurate uses Whisper small and Most accurate uses Whisper medium. Automatic picks base for English shows and small for Japanese and Chinese ones.',
      segmented([['auto', 'Automatic'], ['base', 'Fast'], ['small', 'Accurate'], ['medium', 'Most accurate']], settings.captionModel || 'auto', v => { settings.captionModel = v; saveSettings(); }, 'Recognition quality')),
    setRow('Caption recognition', status),
    setRow('Pro mode', 'Fine-tune the speech recognition: model, language, hardware and decoding.', switchInput('pro', 'Pro mode', on => { settings.pro = on; saveSettings(); proBox.hidden = !on; if (on) animateItems(proBox.querySelectorAll('.set-row'), 10); })),
    setRow('Saved auto captions', 'Captions made on this computer are kept, so they load instantly next time.', el('button', { class: 'btn tonal small', type: 'button', text: 'Delete', onclick: clearAutoCaptions }))));
  view.append(proBox);
  getJSON('/api/captions').then(cap => {
    Recog.cap = cap;
    const v = cap.versions || {};
    if (cap.available) status.replaceChildren(el('span', { class: 'set-ok', text: 'Ready.' }), ' ',
      el('span', { translate: 'no', text: `Whisper · ${cap.engine}${v['faster-whisper'] ? ' ' + v['faster-whisper'] : ''}${v.av ? ` · PyAV ${v.av}` : ''}${v.ffmpeg ? ' · ffmpeg' : ''}` }));
    else if (S.client === 'android') status.textContent = 'Auto captions aren’t available on phones yet. Feed transcripts and caption files still work.';
    else status.replaceChildren('Not set up yet. Quit Afterhours, run this once, then start it again:', el('code', { text: cap.install || 'python3 afterhours.py --setup-captions' }));
  }).catch(() => { status.textContent = 'Afterhours’ helper isn’t answering. Is afterhours.py still running?'; });
  view.append(setGroup('Playback',
    setRow('Play the next episode automatically', null, switchInput('autoNext', 'Play the next episode automatically', on => { settings.autoNext = on; saveSettings(); $('npAuto').setAttribute('aria-pressed', String(on)); }))));
  view.append(setGroup('Appearance',
    setRow('Appearance', 'Follow your system, or always use light or dark.', segmented([['system', 'System'], ['light', 'Light'], ['dark', 'Dark']], settings.theme, v => { settings.theme = v; saveSettings(); applyTheme(); }, 'Appearance')),
    setRow('Animations', 'Fluent-style motion: pages glide in, artwork flies into place, and controls respond as you press them.', switchInput('motion', 'Animations', on => { settings.motion = on; saveSettings(); applyMotion(); toast(on ? 'Animations on.' : 'Animations off.'); })),
    setRow('Mouse halo', 'The soft glow that follows your mouse over lists, menus and buttons.', switchInput('pointerGlow', 'Mouse halo', on => { settings.pointerGlow = on; saveSettings(); applyGlow(); toast(on ? 'Mouse halo on.' : 'Mouse halo off.'); }))));
  if (local) {
    view.append(windowGroup());
    const phoneSwitch = el('input', { type: 'checkbox', class: 'switch', role: 'switch', 'aria-label': 'Use on your phone' });
    const phoneBox = el('div', { class: 'phone-box', hidden: true });
    const paintPhone = info => {
      phoneSwitch.checked = !!info.on;
      phoneBox.hidden = !info.on;
      if (!info.on) return;
      phoneBox.replaceChildren(el('p', { class: 'set-sub', text: 'Open this link on your phone, then add it to your home screen:' }), el('code', { class: 'phone-link', text: info.url }),
        el('div', { class: 'set-actions' },
          el('button', { class: 'btn tonal small', type: 'button', text: 'Copy link', onclick: () => navigator.clipboard.writeText(info.url).then(() => toast('Link copied.'), () => {}) }),
          el('button', { class: 'btn tonal small', type: 'button', text: 'New link', onclick: () => postJSON('/api/phone?renew=1').then(i => { paintPhone(i); toast('The old link no longer works. Use the new one on your phones.'); }, e => toast(e.message, true)) })));
    };
    phoneSwitch.addEventListener('change', () => postJSON('/api/phone?on=' + (phoneSwitch.checked ? 1 : 0))
      .then(i => { paintPhone(i); toast(i.on ? 'Phone access on. Keep this computer awake while you listen.' : 'Phone access off.'); },
        e => { phoneSwitch.checked = false; toast(e.message, true); }));
    getJSON('/api/phone').then(paintPhone).catch(() => {});
    view.append(setGroup('Phone', setRow('Use on your phone', 'Let phones on the same Wi-Fi use Afterhours from this computer, with a private link.', phoneSwitch), phoneBox));
  }
  const missing = library.filter(s => s.status === 'missing').length;
  view.append(setGroup('Library',
    IN_WINDOW ? setRow('Bring over your library', 'Copy your shows, progress and settings from the browser you used before.', el('button', { class: 'btn tonal small', type: 'button', text: 'Bring over', onclick: startMove })) : null,
    setRow('Look for missing shows again', missing ? `${missing} show${missing > 1 ? 's' : ''} couldn’t be found automatically.` : 'Every show on your shelf was found.', el('button', { class: 'btn tonal small', type: 'button', text: 'Look again', disabled: !missing, onclick: retryMissing })),
    setRow('Import OPML', 'Add shows from another podcast app.', el('button', { class: 'btn tonal small', type: 'button', text: 'Import…', onclick: () => $('opmlFile').click() })),
    setRow('Export OPML', 'Save your shows for another podcast app.', el('button', { class: 'btn tonal small', type: 'button', text: 'Export', onclick: exportOPML }))));
  const ver = el('span', { text: `Afterhours ${who.version || ''}`.trim() });
  view.append(setGroup('About', setRow('Version', ver),
    local ? setRow('Quit Afterhours', 'Stops Afterhours on this computer. Open it again whenever you want to listen.', el('button', { class: 'btn tonal small', type: 'button', text: 'Quit', onclick: quitApp })) : null));
  animateItems(view.querySelectorAll('.set-group'), 50);
}
async function quitApp() {
  if (!confirm(tr('Quit Afterhours? Playback stops, and you can open Afterhours again any time.'))) return;
  audio.pause();
  saveProgress(true);
  try { await postJSON('/api/quit'); } catch {}
  document.body.replaceChildren(el('div', { class: 'empty', style: 'margin-top:30vh' }, el('h2', { text: 'Afterhours has stopped' }), el('p', { text: 'You can close this tab. To listen again, open Afterhours.' })));
}
function renderSearch(term) {
  const input = el('input', { type: 'search', value: term || '', placeholder: 'Shows, topics, or a feed link', 'aria-label': 'Search podcasts', enterkeyhint: 'search', autocomplete: 'off', spellcheck: 'false' });
  const form = el('form', { class: 'big-search' }, icon('search'), input);
  form.addEventListener('submit', e => { e.preventDefault(); if (input.value.trim()) handleQuery(input.value.trim()); });
  view.append(pageHead('Search', null), form);
  if (!term) {
    setTimeout(() => input.focus(), 60);
    const topics = [...CATS.map(c => c.keywords[0]), CATALOG.langs.ja.keywords[1], CATALOG.langs.zh.keywords[0]];
    view.append(section('Try a topic', null, el('div', { class: 'chips wrap' }, topics.map(([t]) => el('a', { class: 'chip', translate: 'no', href: '#/search/' + encodeURIComponent(t), text: t })))));
    return;
  }
  if (looksLikeLink(term)) { handleLink(term); return; }
  const grid = el('div', { class: 'grid' }, skeletonCards(8));
  view.append(section(`Results for “${term}”`, null, grid));
  searchAll(term).then(rs => {
    if (!grid.isConnected) return;
    grid.replaceChildren(...(rs.length ? rs.map(r => card(candFromResult(r, 'mine'))) : [el('p', { class: 'row-empty', text: 'No shows found. Try fewer words, or paste the feed link.' })]));
    animateItems(grid.children, 22);
  }).catch(e => { if (grid.isConnected) grid.replaceChildren(el('p', { class: 'row-empty', text: e.message })); });
}
async function searchAll(term) {
  const kana = /[\u3040-\u30ff]/.test(term), han = /[\u3400-\u9fff]/.test(term);
  const out = new Map();
  let err = null;
  for (const c of kana ? ['jp'] : han ? ['tw', 'jp'] : ['gb', 'us']) {
    try { for (const r of await dirSearch(term, c, 0, 25)) if (!out.has(r.feedUrl)) out.set(r.feedUrl, r); } catch (e) { err = e; }
  }
  if (!out.size && err) throw err;
  return [...out.values()];
}
const looksLikeLink = v => /^(https?|feed|pcast|podcast|itpc):\/\//i.test(v) || /^www\./i.test(v) || /podcasts\.apple\.com/i.test(v) || /\.(xml|rss)(\?|$)/i.test(v);
function handleQuery(v) { if (looksLikeLink(v)) handleLink(v); else go('search/' + encodeURIComponent(v)); }
async function handleLink(v) {
  const apple = v.match(/podcasts\.apple\.com\/.*?\bid(\d+)/i);
  try {
    let info;
    if (apple) {
      const rs = await dirLookup([apple[1]], 'gb', 0);
      if (!rs[0]) throw new Error('Apple doesn’t list a public feed for that show.');
      info = rs[0];
    } else {
      let url = v.replace(/^(feed|pcast|podcast|itpc):\/\//i, 'https://');
      if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
      toast('Loading that link…');
      try {
        const { text } = await fetchText(url);
        const parsed = parseFeed(text, url);
        info = { feedUrl: url, title: parsed.show.title, author: parsed.show.author, image: parsed.show.image, description: parsed.show.description, link: parsed.show.link };
        await idb.set('eps:' + url, { t: Date.now(), eps: parsed.episodes.slice(0, 300) });
      } catch {
        toast('That isn’t a feed, so Afterhours is looking through the page for episodes…');
        const res = await crawlSite(url);
        const meta = res.show || {};
        if (res.feedUrl) {
          const { text } = await fetchText(res.feedUrl);
          const parsed = parseFeed(text, res.feedUrl);
          info = { feedUrl: res.feedUrl, title: parsed.show.title, author: parsed.show.author, image: parsed.show.image || meta.image, description: parsed.show.description, link: parsed.show.link || res.pageUrl, site: res.pageUrl, source: 'rss' };
          await idb.set('eps:' + res.feedUrl, { t: Date.now(), eps: parsed.episodes.slice(0, 300) });
        } else {
          const key = 'web:' + res.pageUrl;
          info = { feedUrl: key, title: meta.title || url, author: '', image: meta.image || '', description: meta.description || '', link: res.pageUrl, site: res.pageUrl, source: 'web' };
          await idb.set('eps:' + key, { t: Date.now(), eps: webEpisodes(res, key) });
        }
      }
    }
    const s = addShow(Object.assign({ cat: 'mine' }, info));
    $('searchInput').value = '';
    go('show/' + s.sid);
  } catch (e) { toast(`Couldn’t add that show. ${e.message}`, true); }
}

/* ---------- playback ---------- */
const curDur = () => { const d = audio.duration; return isFinite(d) && d > 0 ? d : (S.current ? S.current.ep.duration || 0 : 0); };
const slimEp = ep => ({ id: ep.id, url: ep.url, type: ep.type, title: ep.title, date: ep.date, duration: ep.duration, image: ep.image, number: ep.number, desc: clip(ep.desc || '', 1600), transcripts: ep.transcripts || [] });
function loadEpisode(ep, show, queue, autoplay) {
  saveProgress(true);
  S.current = { ep, show };
  S.queue = queue && queue.length ? queue : [{ ep, show }];
  S.lastSecond = -1;
  const saved = progress[ep.id];
  S.pendingSeek = saved && !saved.done && saved.p > 10 ? Math.max(0, saved.p - 3) : null;
  audio.src = ep.url;
  audio.defaultPlaybackRate = settings.speed;
  audio.playbackRate = settings.speed;
  try { audio.preservesPitch = true; audio.mozPreservesPitch = true; audio.webkitPreservesPitch = true; } catch {}
  for (const id of ['barSeek', 'npSeek']) $(id).style.setProperty('--buf', '0%');
  recent = [{ ep: slimEp(ep), show, t: Date.now() }, ...recent.filter(r => r.ep.id !== ep.id)].slice(0, 40);
  store.set(KEY.recent, recent);
  store.set(KEY.last, { epId: ep.id });
  updateNowPlaying();
  updateTimes(true);
  loadCaptions(ep);
  if (autoplay) audio.play().catch(err => { if (err && err.name === 'NotAllowedError') toast('Press play to start listening.'); });
  paintAllPills();
  renderSidebar();
}
function updateNowPlaying() {
  const c = S.current;
  $('bar').classList.toggle('idle', !c);
  if (!c) return;
  const img = c.ep.image || c.show.image;
  const lib = showByFeed(c.show.feedUrl);
  const href = '#/show/' + (lib ? lib.sid : c.show.sid);
  $('barTitle').textContent = c.ep.title;
  $('barShow').textContent = c.show.title;
  $('barShow').href = href;
  $('barArt').replaceChildren(art(img, c.show.title, { eager: true }));
  $('npTitle').textContent = c.ep.title;
  $('npShow').textContent = c.show.title;
  $('npShow').href = href;
  $('npArt').replaceChildren(art(img, c.show.title, { eager: true }));
  const bg = $('npBg');
  bg.replaceChildren();
  if (img) { const i = el('img', { alt: '', src: img }); i.addEventListener('error', () => i.remove(), { once: true }); bg.append(i); }
  document.title = `${c.ep.title} | Afterhours`;
  artColor(img).then(hsl => { if (S.current === c && hsl) for (const id of ['bar', 'np', 'capPill']) applyTint($(id), hsl); });
  if ('mediaSession' in navigator) {
    try { navigator.mediaSession.metadata = new MediaMetadata({ title: c.ep.title, artist: c.show.author || c.show.title, album: c.show.title, artwork: img ? [{ src: img, sizes: '512x512' }] : [] }); } catch {}
  }
}
function setPlaying(playing) {
  for (const id of ['barPlay', 'npPlay']) { setIcon($(id), playing ? 'pause' : 'play'); $(id).setAttribute('aria-label', playing ? 'Pause' : 'Play'); }
  $('barPlay').title = playing ? 'Pause (Space)' : 'Play (Space)';
  $('np').classList.toggle('paused', !playing);
  document.body.classList.toggle('is-playing', playing);
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
  paintAllPills();
}
function updateTimes(force) {
  const d = curDur();
  const t = S.pendingSeek != null ? S.pendingSeek : (audio.currentTime || 0);
  const pct = d ? Math.min(100, t / d * 100) : 0;
  if (!S.dragging) for (const id of ['barSeek', 'npSeek']) { $(id).value = Math.round(pct * 10); $(id).style.setProperty('--fill', pct + '%'); }
  $('barProg').style.width = pct + '%';
  const sec = Math.floor(t);
  if (force || sec !== S.lastSecond) {
    S.lastSecond = sec;
    if (!S.dragging) { const a = fmtTime(t), b = '-' + fmtTime(Math.max(0, d - t)); $('barCur').textContent = a; $('npCur').textContent = a; $('barLeft').textContent = b; $('npLeft').textContent = b; }
    for (const id of ['barSeek', 'npSeek']) $(id).setAttribute('aria-valuetext', `${fmtTime(t)} of ${fmtTime(d)}`);
  }
}
function updateBuffered() {
  const d = curDur();
  if (!d || !audio.buffered.length) return;
  let end = 0;
  for (let i = 0; i < audio.buffered.length; i++) if (audio.buffered.start(i) <= audio.currentTime + 1) end = Math.max(end, audio.buffered.end(i));
  for (const id of ['barSeek', 'npSeek']) $(id).style.setProperty('--buf', Math.min(100, end / d * 100) + '%');
}
function updatePosState() {
  if (!('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
  const d = audio.duration;
  if (!isFinite(d) || d <= 0) return;
  try { navigator.mediaSession.setPositionState({ duration: d, playbackRate: audio.playbackRate, position: Math.min(audio.currentTime, d) }); } catch {}
}
function loop() { updateTimes(); renderCaptions(); S.raf = requestAnimationFrame(loop); }
function togglePlay() {
  if (!S.current) return;
  if (audio.paused) audio.play().catch(() => toast('This episode couldn’t start. Try again in a moment.', true));
  else audio.pause();
}
function skip(sec) {
  if (!S.current) return;
  if (S.pendingSeek != null) { S.pendingSeek = Math.max(0, S.pendingSeek + sec); updateTimes(true); return; }
  const d = curDur();
  audio.currentTime = Math.max(0, Math.min(d ? d - 0.5 : Infinity, audio.currentTime + sec));
  updateTimes(true);
  renderCaptions(true);
}
function step(dir) {
  if (!S.current) return;
  if (dir < 0 && audio.currentTime > 5) { audio.currentTime = 0; return; }
  const i = S.queue.findIndex(x => x.ep.id === S.current.ep.id);
  const t = S.queue[i + dir];
  if (t) loadEpisode(t.ep, t.show, S.queue, true);
  else toast(dir > 0 ? 'That’s the last episode in this list.' : 'That’s the first episode in this list.');
}
let lastSave = 0;
function persistProgress() {
  const ids = Object.keys(progress);
  if (ids.length > 4000) ids.sort((a, b) => progress[a].t - progress[b].t).slice(0, ids.length - 3000).forEach(k => delete progress[k]);
  store.set(KEY.progress, progress);
}
function saveProgress(force) {
  const c = S.current;
  if (!c || S.pendingSeek != null) return;
  const d = curDur(), p = audio.currentTime || 0;
  if (!d || p < 1) return;
  progress[c.ep.id] = { p: Math.floor(p), d: Math.round(d), done: d - p < Math.min(45, d * 0.04), t: Date.now() };
  if (force || Date.now() - lastSave > 5000) { lastSave = Date.now(); persistProgress(); paintAllPills(); }
}
function onEnded() {
  const c = S.current;
  if (!c) return;
  progress[c.ep.id] = { p: 0, d: Math.round(curDur()), done: true, t: Date.now() };
  persistProgress();
  paintAllPills();
  if (S.sleep.mode === 'end') { setSleep('off', true); toast('Sleep timer stopped at the end of the episode. Good night.'); return; }
  if (settings.autoNext) {
    const i = S.queue.findIndex(x => x.ep.id === c.ep.id);
    const n = S.queue[i + 1];
    if (n) loadEpisode(n.ep, n.show, S.queue, true);
  }
}

/* ---------- Now Playing ---------- */
function openNp(withLyrics) {
  if (!S.current) return;
  if (withLyrics == null && matchMedia('(max-width: 899px)').matches) withLyrics = false;
  if (withLyrics != null) setLyrics(withLyrics);
  S.npOpen = true;
  const np = $('np');
  np.classList.remove('closing');
  np.hidden = false;
  void np.offsetWidth;
  np.classList.add('open');
  document.body.classList.add('np-open');
  renderLyrics();
  renderCaptions(true);
  S.lastUserScroll = 0;
  flyArt($('barArt'), $('npArt').firstElementChild, false);
  setTimeout(() => { if (S.cueIdx >= 0) highlightLyric(S.cueIdx); $('npClose').focus({ preventScroll: true, focusVisible: false }); }, 60);
}
function closeNp() {
  S.npOpen = false;
  const np = $('np'), art = $('npArt').firstElementChild;
  if (np.classList.contains('open')) flyArt($('barArt'), art, true);
  np.classList.add('closing');
  np.classList.remove('open');
  document.body.classList.remove('np-open');
  closeSpeed();
  setTimeout(() => {
    if (S.npOpen) return;
    np.hidden = true;
    np.classList.remove('closing');
    if (art) { for (const a of art.getAnimations()) a.cancel(); art.style.transformOrigin = ''; }
  }, motionOn() ? 260 : 0);
  renderCaptions(true);
}
function setLyrics(on) {
  S.lyricsOn = on;
  $('np').classList.toggle('lyrics-on', on);
  $('npLyricsBtn').setAttribute('aria-pressed', String(on));
  renderCaptions(true);
  if (on && S.cueIdx >= 0) { S.lastUserScroll = 0; setTimeout(() => highlightLyric(S.cueIdx), 30); }
}

/* ---------- speed, captions, sleep, volume ---------- */
const PRESETS = [0.8, 1, 1.2, 1.5, 1.75, 2, 2.5, 3];
function setSpeed(v, announce) {
  v = Math.round(Math.min(3, Math.max(0.5, v)) * 100) / 100;
  settings.speed = v;
  saveSettings();
  audio.defaultPlaybackRate = v;
  audio.playbackRate = v;
  for (const id of ['barSpeed', 'npSpeed']) $(id).textContent = fmtSpeed(v);
  $('speedVal').textContent = v.toFixed(2) + '×';
  $('speedRange').value = v;
  $('speedRange').style.setProperty('--fill', ((v - 0.5) / 2.5 * 100) + '%');
  for (const b of $('speedPresets').children) b.setAttribute('aria-pressed', String(Math.abs(+b.dataset.v - v) < 0.001));
  if (announce) toast(`Speed ${fmtSpeed(v)}`);
}
let popAnchor = null;
function openSpeed(anchor) {
  const pop = $('speedPop');
  if (!pop.hidden && popAnchor === anchor) { closeSpeed(); return; }
  popAnchor = anchor;
  pop.hidden = false;
  pop.classList.toggle('on-dark', !!anchor.closest('.np'));
  const r = anchor.getBoundingClientRect(), w = pop.offsetWidth, h = pop.offsetHeight;
  const left = Math.min(innerWidth - w - 12, Math.max(12, r.left + r.width / 2 - w / 2));
  let top = r.top - h - 10;
  if (top < 12) top = r.bottom + 10;
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
  for (const b of document.querySelectorAll('[data-speed-btn]')) b.setAttribute('aria-expanded', String(b === anchor));
  $('speedRange').focus();
}
function closeSpeed() {
  $('speedPop').hidden = true;
  popAnchor = null;
  for (const b of document.querySelectorAll('[data-speed-btn]')) b.setAttribute('aria-expanded', 'false');
}
function syncCC() { for (const id of ['barCC', 'npCC']) $(id).setAttribute('aria-pressed', String(settings.captions)); syncSwitches(); }
function toggleCaptions() { settings.captions = !settings.captions; saveSettings(); syncCC(); renderCaptions(true); toast(settings.captions ? 'Captions on.' : 'Captions off.'); }
function toggleTranscript() { if (!S.current) return; if (!S.npOpen) openNp(true); else setLyrics(!S.lyricsOn); }
const SLEEP_STEPS = ['off', 15, 30, 45, 60, 'end'];
function setSleep(mode, quiet) {
  if (S.sleep.fadeBase != null) { audio.volume = S.sleep.fadeBase; S.sleep.fadeBase = null; }
  clearInterval(S.sleep.timer);
  S.sleep.mode = mode;
  if (typeof mode === 'number') { S.sleep.endsAt = Date.now() + mode * 60000; S.sleep.timer = setInterval(sleepTick, 500); }
  if (!quiet) toast(typeof mode === 'number' ? `Sleep timer: ${mode} minutes.` : mode === 'end' ? 'Sleep timer: at the end of this episode.' : 'Sleep timer off.');
  updateSleepLabel();
}
function sleepTick() {
  const left = S.sleep.endsAt - Date.now();
  if (left <= 0) { audio.pause(); setSleep('off', true); toast('Sleep timer paused playback. Good night.'); return; }
  if (left < 10000 && !audio.paused) { if (S.sleep.fadeBase == null) S.sleep.fadeBase = audio.volume; audio.volume = S.sleep.fadeBase * (left / 10000); }
  updateSleepLabel();
}
function updateSleepLabel() {
  const m = S.sleep.mode;
  const mins = typeof m === 'number' ? Math.max(1, Math.ceil((S.sleep.endsAt - Date.now()) / 60000)) : 0;
  const label = mins ? `${mins} min` : m === 'end' ? 'Ep. end' : '';
  for (const [b, l] of [['barSleep', 'barSleepLabel'], ['npSleep', 'npSleepLabel']]) {
    $(l).textContent = label;
    $(l).hidden = !label;
    $(b).setAttribute('aria-pressed', String(m !== 'off'));
    $(b).setAttribute('aria-label', mins ? `Sleep timer, ${mins} minutes left` : m === 'end' ? 'Sleep timer, stops at the end of this episode' : 'Sleep timer, off');
  }
}
function syncVolumeUI() {
  const v = audio.muted ? 0 : audio.volume;
  if (S.sleep.fadeBase == null) {
    settings.volume = audio.volume;
    settings.muted = audio.muted;
    saveSettings();
    for (const id of ['barVol', 'npVol']) { $(id).value = v; $(id).style.setProperty('--fill', v * 100 + '%'); }
  }
  for (const id of ['barMute', 'npMute']) { setIcon($(id), audio.muted || audio.volume === 0 ? 'mute' : 'vol'); $(id).setAttribute('aria-label', audio.muted ? 'Unmute' : 'Mute'); }
}

/* ---------- theme, OPML ---------- */
const THEMES = ['system', 'light', 'dark'];
const THEME_LABEL = { system: 'Appearance: System', light: 'Appearance: Light', dark: 'Appearance: Dark' };
function syncSwitches() {
  for (const i of document.querySelectorAll('input[data-setting]')) i.checked = !!settings[i.dataset.setting];
}
function syncAutoCap() { syncSwitches(); }
function switchInput(key, label, onChange, cls) {
  const i = el('input', { type: 'checkbox', class: 'switch' + (cls ? ' ' + cls : ''), role: 'switch', 'aria-label': label, dataset: { setting: key } });
  i.checked = !!settings[key];
  i.addEventListener('change', () => onChange(i.checked));
  return i;
}
const capSwitch = () => el('label', { class: 'cap-switch' }, switchInput('autoCaptions', 'Auto captions', on => setAutoCaptions(on), 'small on-dark'), el('span', { text: 'Auto captions' }));
async function setAutoCaptions(on, quiet) {
  settings.autoCaptions = on;
  saveSettings();
  syncSwitches();
  const bare = S.current && !S.untimed && !S.cues.some(c => !c.auto);
  if (!on) {
    if (recogActive()) Recog.stop();
    if (bare) S.auto = S.cues.length ? Object.assign({}, S.auto, { state: 'cancelled' }) : { state: 'off' };
    if (!quiet) toast('Auto captions off. Episodes without captions won’t be captioned automatically.');
  } else {
    const cap = Recog.cap || await Recog.init();
    if (!cap.available) {
      if (bare && !S.cues.length) S.auto = { state: 'unavailable' };
      if (!quiet) toast(`Auto captions need a one-time setup. Quit Afterhours and run: ${cap.install || 'python3 afterhours.py --setup-captions'}`, true);
    } else {
      if (!quiet) toast('Auto captions on. Episodes without captions will be captioned on this computer.');
      if (bare && !recogActive() && !(S.auto && S.auto.state === 'done')) Recog.start(S.current.ep);
    }
  }
  renderLyrics();
  paintRecog();
  renderCaptions(true);
}
function applyTheme() {
  if (settings.theme === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = settings.theme;
}
function exportOPML() {
  const ready = library.filter(s => s.feedUrl);
  if (!ready.length) { toast('There’s nothing to export yet.'); return; }
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  const body = ready.map(s => `    <outline type="rss" text="${esc(s.title)}" title="${esc(s.title)}" xmlUrl="${esc(s.feedUrl)}"/>`).join('\n');
  const xml = `<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">\n  <head><title>Afterhours subscriptions</title></head>\n  <body>\n${body}\n  </body>\n</opml>\n`;
  const a = el('a', { href: URL.createObjectURL(new Blob([xml], { type: 'text/x-opml' })), download: 'afterhours-subscriptions.opml' });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
}
async function importOPML(file) {
  const doc = parseXML(await file.text());
  if (!doc) { toast('That file isn’t valid OPML.', true); return; }
  const outlines = [...doc.getElementsByTagName('outline')].filter(o => o.getAttribute('xmlUrl'));
  const fresh = outlines.filter(o => !showByFeed(o.getAttribute('xmlUrl')));
  if (!fresh.length) { toast('No new shows in that file.'); return; }
  for (const o of fresh) addShow({ feedUrl: o.getAttribute('xmlUrl'), title: o.getAttribute('title') || o.getAttribute('text') || o.getAttribute('xmlUrl'), cat: 'mine' }, true);
  toast(`Imported ${fresh.length} shows.`);
  if (route.name === 'library') render();
  refreshAllFeeds();
}

/* ---------- wiring ---------- */
function wireSeek(id) {
  const s = $(id);
  s.addEventListener('input', () => {
    S.dragging = true;
    s.classList.add('dragging');
    const d = curDur(), t = s.value / 1000 * d;
    for (const x of ['barCur', 'npCur']) $(x).textContent = fmtTime(t);
    for (const x of ['barLeft', 'npLeft']) $(x).textContent = '-' + fmtTime(Math.max(0, d - t));
    for (const x of ['barSeek', 'npSeek']) { $(x).style.setProperty('--fill', s.value / 10 + '%'); if (x !== id) $(x).value = s.value; }
  });
  s.addEventListener('change', () => {
    const d = curDur(), t = s.value / 1000 * d;
    S.dragging = false;
    s.classList.remove('dragging');
    if (!d) return;
    if (S.pendingSeek != null) S.pendingSeek = t; else audio.currentTime = t;
    updateTimes(true);
    renderCaptions(true);
  });
}
function wire() {
  window.addEventListener('hashchange', render);
  window.addEventListener('resize', moveIndicators);
  document.addEventListener('pointermove', e => {
    if (settings.pointerGlow === false || e.pointerType === 'touch') return;
    const t = e.target.closest && e.target.closest('.side-shows a, .nav a, .side-tool, .side-switch, .set-row, .ep, .up-card');
    if (!t) return;
    const r = t.getBoundingClientRect();
    t.style.setProperty('--rx', (e.clientX - r.left) + 'px');
    t.style.setProperty('--ry', (e.clientY - r.top) + 'px');
  }, { passive: true });
  view.addEventListener('click', e => {
    const link = e.target.closest('a.tile-link');
    if (!link || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || !motionOn() || !document.startViewTransition) return;
    e.preventDefault();
    transitionTo(link.getAttribute('href').replace(/^#\//, ''), link.querySelector('.art'));
  });
  $('searchForm').addEventListener('submit', e => { e.preventDefault(); const v = $('searchInput').value.trim(); if (v) handleQuery(v); });
  $('autoCapSwitch').addEventListener('change', e => setAutoCaptions(e.target.checked));
  $('opmlFile').addEventListener('change', e => { const f = e.target.files[0]; e.target.value = ''; if (f) importOPML(f); });

  audio.addEventListener('loadedmetadata', () => {
    if (S.pendingSeek != null) { const target = Math.min(S.pendingSeek, Math.max(0, audio.duration - 5)); S.pendingSeek = null; try { audio.currentTime = target; } catch {} }
    audio.playbackRate = settings.speed;
    updateTimes(true);
    updatePosState();
  });
  audio.addEventListener('play', () => { setPlaying(true); cancelAnimationFrame(S.raf); S.raf = requestAnimationFrame(loop); renderSidebar(); });
  audio.addEventListener('pause', () => { setPlaying(false); cancelAnimationFrame(S.raf); saveProgress(true); updateTimes(true); updatePosState(); });
  audio.addEventListener('ended', onEnded);
  audio.addEventListener('timeupdate', () => { if (audio.paused) { updateTimes(); renderCaptions(); } saveProgress(false); });
  audio.addEventListener('seeked', () => { updateTimes(true); renderCaptions(); updatePosState(); });
  audio.addEventListener('progress', updateBuffered);
  audio.addEventListener('ratechange', updatePosState);
  audio.addEventListener('volumechange', syncVolumeUI);
  audio.addEventListener('error', () => {
    if (!S.current || !audio.getAttribute('src')) return;
    setPlaying(false);
    toast('This episode’s audio couldn’t be played. The link may have changed, so try refreshing the show.', true);
  });
  wireSeek('barSeek');
  wireSeek('npSeek');

  for (const id of ['barPlay', 'npPlay']) $(id).addEventListener('click', togglePlay);
  for (const id of ['barBack', 'npBack']) $(id).addEventListener('click', () => skip(-15));
  for (const id of ['barFwd', 'npFwd']) $(id).addEventListener('click', () => skip(30));
  $('npPrev').addEventListener('click', () => step(-1));
  $('npNext').addEventListener('click', () => step(1));
  for (const id of ['barArt', 'barTitle', 'barExpand']) $(id).addEventListener('click', () => openNp());
  $('barLyrics').addEventListener('click', () => openNp(true));
  $('npClose').addEventListener('click', closeNp);
  $('npShow').addEventListener('click', () => closeNp());
  $('npAuto').addEventListener('click', () => { settings.autoNext = !settings.autoNext; saveSettings(); $('npAuto').setAttribute('aria-pressed', String(settings.autoNext)); syncSwitches(); toast(settings.autoNext ? 'The next episode will play automatically.' : 'Autoplay off.'); });
  $('npLyricsBtn').addEventListener('click', () => setLyrics(!S.lyricsOn));
  for (const id of ['barCC', 'npCC']) $(id).addEventListener('click', toggleCaptions);
  for (const id of ['barSleep', 'npSleep']) $(id).addEventListener('click', () => setSleep(SLEEP_STEPS[(SLEEP_STEPS.indexOf(S.sleep.mode) + 1) % SLEEP_STEPS.length]));
  for (const id of ['barSpeed', 'npSpeed']) $(id).addEventListener('click', e => openSpeed(e.currentTarget));
  for (const v of PRESETS) $('speedPresets').append(el('button', { class: 'preset', type: 'button', dataset: { v }, text: fmtSpeed(v), onclick: () => setSpeed(v) }));
  $('speedRange').addEventListener('input', e => setSpeed(+e.target.value));
  $('speedReset').addEventListener('click', () => setSpeed(1));
  document.addEventListener('pointerdown', e => { if (!$('speedPop').hidden && !e.target.closest('#speedPop, [data-speed-btn]')) closeSpeed(); });
  for (const id of ['barVol', 'npVol']) $(id).addEventListener('input', e => { S.sleep.fadeBase = null; audio.volume = +e.target.value; audio.muted = audio.volume === 0; });
  for (const id of ['barMute', 'npMute']) $(id).addEventListener('click', () => { audio.muted = !audio.muted; if (!audio.muted && audio.volume === 0) audio.volume = 0.6; });

  const ly = $('lyrics');
  for (const ev of ['wheel', 'touchmove', 'keydown']) ly.addEventListener(ev, () => { S.lastUserScroll = Date.now(); }, { passive: true });
  ly.addEventListener('click', e => {
    const li = e.target.closest('.lyric');
    if (!li || !S.current) return;
    const c = S.cues[+li.dataset.i];
    if (!c) return;
    S.lastUserScroll = 0;
    if (S.pendingSeek != null) S.pendingSeek = c.start; else audio.currentTime = c.start;
    if (audio.paused) audio.play().catch(() => {});
    renderCaptions(true);
  });
  $('capFile').addEventListener('change', async e => {
    const f = e.target.files[0];
    e.target.value = '';
    if (!f) return;
    if (!S.current) { toast('Start an episode first, then load its caption file.'); return; }
    const text = await f.text(), name = f.name.toLowerCase();
    const type = name.endsWith('.srt') ? 'srt' : name.endsWith('.vtt') ? 'vtt' : name.endsWith('.json') ? 'json' : '';
    applyTranscript(text, type);
    if (!S.cues.length) { toast('That file has no timed captions I can read.', true); loadCaptions(S.current.ep); return; }
    if (text.length < 600000) store.set(KEY.cap(S.current.ep.id), { name: f.name, type, text });
    S.capLoading = false;
    S.capError = '';
    renderLyrics();
    renderCaptions(true);
    toast(`Loaded captions from ${f.name}.`);
  });

  document.addEventListener('keydown', e => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t.closest && t.closest('input:not([type=range]):not([type=checkbox]), textarea, select, [contenteditable="true"]')) { if (e.key === 'Escape') t.blur(); return; }
    const onRange = t.type === 'range';
    switch (e.key) {
      case 'Escape': if (!$('speedPop').hidden) closeSpeed(); else if (S.npOpen) closeNp(); break;
      case '/': e.preventDefault(); if (getComputedStyle($('searchForm')).display !== 'none') $('searchInput').focus(); else go('search'); break;
      case ' ': case 'k': case 'K': if (e.key === ' ' && t.closest && t.closest('button, a, input')) return; e.preventDefault(); togglePlay(); break;
      case 'ArrowLeft': case 'j': case 'J': if (onRange) return; e.preventDefault(); skip(-15); break;
      case 'ArrowRight': case 'l': case 'L': if (onRange) return; e.preventDefault(); skip(30); break;
      case '[': setSpeed(settings.speed - 0.1, true); break;
      case ']': setSpeed(settings.speed + 0.1, true); break;
      case 'c': case 'C': if (S.current) toggleCaptions(); break;
      case 't': case 'T': toggleTranscript(); break;
      case 'm': case 'M': audio.muted = !audio.muted; break;
      case 'N': if (e.shiftKey) step(1); break;
      case 'P': if (e.shiftKey) step(-1); break;
    }
  });
  if ('mediaSession' in navigator) {
    const set = (a, fn) => { try { navigator.mediaSession.setActionHandler(a, fn); } catch {} };
    set('play', () => audio.play());
    set('pause', () => audio.pause());
    set('seekbackward', d => skip(-((d && d.seekOffset) || 15)));
    set('seekforward', d => skip((d && d.seekOffset) || 30));
    set('seekto', d => { if (d.fastSeek && 'fastSeek' in audio) audio.fastSeek(d.seekTime); else audio.currentTime = d.seekTime; updateTimes(true); });
    set('previoustrack', () => step(-1));
    set('nexttrack', () => step(1));
    set('stop', () => audio.pause());
  }
  window.addEventListener('pagehide', () => { saveProgress(true); if (recogActive()) Recog.save(false); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) saveProgress(true); });
}

function init() {
  if (IN_WINDOW && !localStorage.getItem('ah.library') && !sessionStorage.getItem('ah.moveChecked')) {
    // a brand-new window: a library may already be waiting ("Switch now"), so take it before anything starts
    sessionStorage.setItem('ah.moveChecked', '1');
    const go = () => init();
    fetch('/api/move').then(r => r.json()).then(j => {
      if (j && j.data && typeof j.data === 'object') {
        for (const [k, v] of Object.entries(j.data)) if (k.startsWith('ah.') && typeof v === 'string') localStorage.setItem(k, v);
        location.reload();
      } else go();
    }).catch(go);
    return;
  }
  if (IN_WINDOW && !localStorage.getItem('ah.library')) localStorage.setItem('ah.moveOffer', '1');   // a new window: offer to bring the old library
  seedLibrary();
  applyTheme();
  applyMotion();
  applyGlow();
  applyLanguage();
  syncAutoCap();
  Recog.init();
  wire();
  audio.volume = settings.volume;
  audio.muted = settings.muted;
  syncVolumeUI();
  setSpeed(settings.speed);
  syncCC();
  $('npAuto').setAttribute('aria-pressed', String(settings.autoNext));
  setLyrics(true);
  if (!location.hash || location.hash === '#') history.replaceState(null, '', '#/home');
  render();
  const last = store.get(KEY.last, {});
  const r = last.epId && recent.find(x => x.ep.id === last.epId);
  if (r) {
    loadEpisode(r.ep, r.show, [{ ep: r.ep, show: r.show }], false);
    Feeds.cached(r.show).then(c => { if (c && S.current && S.current.ep.id === r.ep.id) { const sorted = settings.sort === 'oldest' ? [...c.eps].reverse() : c.eps; if (sorted.some(e => e.id === r.ep.id)) S.queue = sorted.map(e => ({ ep: e, show: r.show })); } });
  }
  setPlaying(false);
  updateSleepLabel();
  runSetup();
  whoami().then(w => { if ('serviceWorker' in navigator && w.client !== 'android') navigator.serviceWorker.register('/sw.js').catch(() => {}); });
}
init();
})();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()
