#!/usr/bin/env python3
"""
夜更かしラジオ (After Hours): a small, private podcast player.

Run it:    python3 afterhours.py
It opens:  http://127.0.0.1:8765  in your browser

Needs only Python 3.8 or newer, with nothing extra to install.

Why a script and not just a web page: podcast sites don't let a web page read
their feeds directly (browsers block it, a rule called CORS). This script serves
the player and fetches feeds, transcripts and search results on its behalf.
It only listens on your own computer, and your library, progress and settings
stay in your browser on this machine.

Options:
  --port 8765      use a different port (each port keeps its own library)
  --no-browser     don't open the browser automatically
  --verbose        log every request
"""

import argparse
import gzip
import http.server
import json
import os
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib

APP_ID = "afterhours"
VERSION = "1.0"
DEFAULT_PORT = 8765
MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (compatible; AfterHours/%s; personal podcast player)" % VERSION
ACCEPT = ("application/rss+xml, application/xml;q=0.9, text/xml;q=0.9, application/json;q=0.8, "
          "text/vtt;q=0.8, text/plain;q=0.6, */*;q=0.5")

CERT_HELP = ("A secure connection couldn't be checked. On a Mac with Python from python.org, open "
             "your Applications > Python folder and run “Install Certificates.command”, then restart the player.")

PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com data:; img-src * data: blob:; media-src * data: blob:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")


def make_ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi  # optional; helps on some Python installs
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


SSL_CTX = make_ssl_context()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AfterHours/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _host_ok(self):
        # Refuse requests that arrive under another host name (guards against DNS rebinding).
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.split("]")[0] + "]" if host.startswith("[") else host.split(":")[0]
        return name in ("127.0.0.1", "localhost", "[::1]")

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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

    def do_GET(self):
        if not self._host_ok():
            return self._error(403, "This player only answers on 127.0.0.1.")
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8", {"Content-Security-Policy": PAGE_CSP})
        if path == "/api/ping":
            return self._send(200, json.dumps({"app": APP_ID, "version": VERSION}), "application/json")
        if path == "/api/fetch":
            return self._proxy(query)
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._error(404, "Not found.")

    def _proxy(self, query):
        url = (urllib.parse.parse_qs(query).get("url") or [""])[0].strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return self._error(400, "That isn't a web address the player can open.")
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": ACCEPT, "Accept-Encoding": "gzip, deflate"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT, context=SSL_CTX) as resp:
                data = resp.read(MAX_BYTES + 1)
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                final_url = resp.geturl()
        except urllib.error.HTTPError as e:
            return self._error(502, "The site answered with an error (%s)." % e.code)
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
                return self._error(502, CERT_HELP)
            if isinstance(reason, socket.timeout) or "timed out" in str(reason):
                return self._error(504, "The site took too long to answer.")
            return self._error(502, "Couldn't reach the site (%s)." % reason)
        except (socket.timeout, TimeoutError):
            return self._error(504, "The site took too long to answer.")
        except Exception as e:  # keep the helper alive whatever happens upstream
            return self._error(502, "Couldn't load that address (%s)." % e.__class__.__name__)

        if len(data) > MAX_BYTES:
            return self._error(413, "That file is too large to load.")
        try:
            if encoding == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            elif encoding == "deflate":
                try:
                    data = zlib.decompress(data)
                except zlib.error:
                    data = zlib.decompress(data, -zlib.MAX_WBITS)
        except Exception:
            return self._error(502, "The site sent data the player couldn't unpack.")
        if len(data) > MAX_BYTES * 4:
            return self._error(413, "That file is too large to load.")

        safe_url = urllib.parse.quote(final_url, safe=":/?#[]@!$&'()*+,;=%~")
        self._send(200, data, ctype, {
            "X-Final-URL": safe_url,
            # Never let fetched content run as a page on this origin.
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })


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


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="夜更かしラジオ (After Hours): a small, private podcast player.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to use (default %d)" % DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    parser.add_argument("--verbose", action="store_true", help="log every request")
    args = parser.parse_args()
    url = "http://127.0.0.1:%d/" % args.port

    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError:
        if already_running(args.port):
            print("The player is already running at %s" % url)
            if not args.no_browser:
                webbrowser.open(url)
            return
        print("Port %d is in use by another program. Try:  python3 afterhours.py --port %d\n"
              "Each port keeps its own library, so pick one and stick with it." % (args.port, args.port + 1),
              file=sys.stderr)
        sys.exit(1)

    server.verbose = args.verbose
    print("夜更かしラジオ is running at %s\nKeep this window open while you listen. Press Ctrl+C to stop." % url)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped. Good night.")
    finally:
        server.server_close()


PAGE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="referrer" content="no-referrer">
<title>夜更かしラジオ</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23FFC15E'/%3E%3Cstop offset='.6' stop-color='%23F26B7A'/%3E%3Cstop offset='1' stop-color='%23B2418F'/%3E%3C/linearGradient%3E%3C/defs%3E%3Ccircle cx='32' cy='32' r='28' fill='url(%23g)'/%3E%3C/svg%3E">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Shippori+Mincho+B1:wght@600;800&family=Zen+Kaku+Gothic+New:wght@400;500;700&display=swap">
<style>
:root {
  --bg: #F4F2F8; --bg-2: #ECE9F3; --surface: #FFFFFF; --ink: #1D1A3A; --muted: #5E5A83; --faint: #8F8BB0;
  --line: #DDD9EA; --hover: #E5E1F0; --accent: #B42E59; --on-accent: #FFFFFF; --focus: #6A4DDB;
  --panel: #1E1A45; --panel-2: #2A245E; --panel-ink: #F6F2FF; --panel-muted: #B8B1DD; --panel-line: rgba(255,255,255,.13);
  --sun-a: #FFC15E; --sun-b: #F26B7A; --sun-c: #B2418F;
  --font-ui: "Zen Kaku Gothic New", "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Noto Sans JP", "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-display: "Shippori Mincho B1", "Hiragino Mincho ProN", "Yu Mincho", "Noto Serif JP", "Songti SC", Georgia, serif;
  color-scheme: light;
  box-sizing: border-box;
  padding-top: env(safe-area-inset-top, 0px);
  padding-bottom: env(safe-area-inset-bottom, 0px);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #110F26; --bg-2: #0C0A1D; --surface: #1A1740; --ink: #EEEAF8; --muted: #A9A3CC; --faint: #7B75A4;
    --line: #2B2756; --hover: #201C4A; --accent: #FF8AAA; --on-accent: #26081B; --focus: #B7A0FF;
    --panel: #19153C; --panel-2: #262058;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #110F26; --bg-2: #0C0A1D; --surface: #1A1740; --ink: #EEEAF8; --muted: #A9A3CC; --faint: #7B75A4;
  --line: #2B2756; --hover: #201C4A; --accent: #FF8AAA; --on-accent: #26081B; --focus: #B7A0FF;
  --panel: #19153C; --panel-2: #262058;
  color-scheme: dark;
}

*, *::before, *::after { box-sizing: inherit; }
html { height: 100%; background: var(--bg); scroll-padding-top: env(safe-area-inset-top, 0px); }
body {
  height: 100%; margin: 0; background: var(--bg); color: var(--ink);
  font-family: var(--font-ui); font-size: 15px; line-height: 1.55; -webkit-font-smoothing: antialiased;
}
body::before { content: ""; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 30; background: var(--bg); }
button, input { font: inherit; color: inherit; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: .45; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
[hidden] { display: none !important; }
svg { width: 20px; height: 20px; flex: none; }
.sprite { position: absolute; width: 0; height: 0; overflow: hidden; }
.visually-hidden { position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* Layout */
.app { height: 100%; display: grid; grid-template-columns: 276px minmax(0, 1fr) 400px; }
.library { background: var(--bg-2); border-right: 1px solid var(--line); overflow: auto; min-height: 0; padding: 22px 16px 18px; display: flex; flex-direction: column; gap: 16px; }
.episodes { overflow: auto; min-height: 0; padding: 30px clamp(20px, 3.2vw, 44px) 48px; }
.player { background: var(--panel); color: var(--panel-ink); overflow: auto; min-height: 0; border-left: 1px solid rgba(255,255,255,.05); --track: rgba(255,255,255,.16); --track-buf: rgba(255,255,255,.3); --thumb: #fff; --focus: var(--sun-a); }
.full { display: flex; flex-direction: column; gap: 16px; padding: 26px 28px 22px; height: 100%; }
.mini, .full-top { display: none; }

/* Library */
.brand { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 4px; }
.brand h1 { margin: 0; font-family: var(--font-display); font-weight: 800; font-size: 1.32rem; line-height: 1.2; letter-spacing: .02em; white-space: nowrap; }
.brand h1::before { content: ""; display: inline-block; width: .78em; height: .78em; margin-right: .42em; vertical-align: -.04em; border-radius: 50%; background: linear-gradient(var(--sun-a), var(--sun-b) 60%, var(--sun-c)); }
.add { display: flex; align-items: center; gap: 6px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 4px 4px 4px 10px; }
.add:focus-within { border-color: var(--focus); box-shadow: 0 0 0 3px color-mix(in srgb, var(--focus) 18%, transparent); }
.add > svg { width: 18px; height: 18px; color: var(--faint); }
.add input { flex: 1; min-width: 0; border: 0; background: transparent; padding: 7px 2px; outline: none; font-size: .88rem; text-overflow: ellipsis; }
.add input::placeholder { color: var(--faint); }
.add button { flex: none; border: 0; border-radius: 7px; background: var(--ink); color: var(--bg); padding: 6px 10px; font-size: .8rem; font-weight: 500; min-width: 56px; }
.results { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 10px; max-height: 55vh; overflow: auto; }
.results-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
.results-head p { margin: 0; font-size: .82rem; color: var(--muted); }
.results ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.result { display: grid; grid-template-columns: 40px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 6px; border-radius: 8px; }
.result:hover { background: var(--hover); }

.cover { position: relative; display: block; width: 44px; height: 44px; border-radius: 7px; overflow: hidden; background: var(--line); flex: none; }
.cover img { width: 100%; height: 100%; object-fit: cover; display: block; }
.cover.fallback { display: grid; place-items: center; background: linear-gradient(160deg, var(--sun-a), var(--sun-b) 55%, var(--sun-c)); color: #fff; font-family: var(--font-display); font-weight: 800; font-size: 1.1rem; }
.result .cover { width: 40px; height: 40px; }
.cover.big { width: 132px; height: 132px; border-radius: 12px; font-size: 2.6rem; box-shadow: 0 14px 30px -18px rgba(20,14,60,.55); }

.shows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.show-btn { display: grid; grid-template-columns: 44px minmax(0, 1fr); gap: 12px; align-items: center; width: 100%; padding: 7px; border: 0; border-radius: 9px; background: transparent; text-align: left; }
.show-btn:hover { background: var(--hover); }
.show-btn[aria-current="true"] { background: var(--surface); box-shadow: 0 0 0 1px var(--line); }
.show-btn[aria-current="true"] .show-title { color: var(--accent); }
.show-text { display: flex; flex-direction: column; min-width: 0; }
.show-title { font-weight: 700; font-size: .9rem; line-height: 1.3; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.show-author { font-size: .78rem; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.library-empty { padding: 2px 6px; color: var(--muted); font-size: .88rem; }
.library-empty p { margin: 0 0 10px; }
.try { display: flex; flex-wrap: wrap; gap: 6px; }
.try button { border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 4px 11px; font-size: .8rem; }
.try button:hover { border-color: var(--accent); color: var(--accent); }
.library-foot { margin-top: auto; display: flex; gap: 16px; padding: 10px 6px 0; border-top: 1px solid var(--line); }
.text-btn { border: 0; background: none; padding: 0; color: var(--muted); font-size: .8rem; text-decoration: underline; text-decoration-color: var(--line); text-underline-offset: 3px; }
.text-btn:hover { color: var(--ink); }
.text-btn.light { color: var(--panel-muted); text-decoration-color: rgba(255,255,255,.25); }
.text-btn.light:hover { color: var(--panel-ink); }

/* Episodes */
.welcome { max-width: 34rem; margin: 8vh auto 0; }
.welcome h2 { margin: 0 0 12px; font-family: var(--font-display); font-weight: 800; font-size: clamp(1.8rem, 3.4vw, 2.6rem); line-height: 1.15; }
.welcome p { margin: 0 0 24px; color: var(--muted); }
.keys { display: grid; grid-template-columns: auto 1fr; gap: 9px 18px; margin: 0; font-size: .88rem; }
.keys dt { white-space: nowrap; }
.keys dd { margin: 0; color: var(--muted); }
kbd { font-family: var(--font-ui); font-size: .76rem; border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 5px; padding: 1px 6px; background: var(--surface); }
.show-head { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 24px; align-items: start; padding-bottom: 24px; border-bottom: 1px solid var(--line); max-width: 820px; }
.show-head h2 { margin: 2px 0 0; font-family: var(--font-display); font-weight: 800; font-size: clamp(1.55rem, 2.8vw, 2.3rem); line-height: 1.15; }
.byline { margin: 6px 0 0; color: var(--muted); font-size: .92rem; }
.about { margin: 12px 0 0; color: var(--muted); font-size: .9rem; max-width: 68ch; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.head-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.pill { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--line); background: var(--surface); border-radius: 999px; padding: 6px 13px 6px 10px; font-size: .84rem; color: var(--ink); text-decoration: none; }
.pill svg { width: 16px; height: 16px; }
.pill:hover:not(:disabled) { border-color: var(--ink); }
.pill.danger:hover { border-color: var(--accent); color: var(--accent); }
.pill.small { padding: 4px 11px; font-size: .78rem; }
.spin svg { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.list-status { color: var(--muted); padding: 22px 6px; margin: 0; }
.episode-list { list-style: none; margin: 0; padding: 0; max-width: 820px; }
.ep { position: relative; display: grid; grid-template-columns: 40px minmax(0, 1fr); gap: 16px; padding: 18px 6px; border-bottom: 1px solid var(--line); cursor: pointer; }
.ep:hover { background: color-mix(in srgb, var(--hover) 55%, transparent); }
.ep-play { width: 40px; height: 40px; margin-top: 1px; border-radius: 50%; border: 1.5px solid var(--line); background: var(--surface); color: var(--ink); display: grid; place-items: center; }
.ep-play svg { width: 16px; height: 16px; }
.ep:hover .ep-play { border-color: var(--ink); }
.ep[data-current="true"] .ep-play { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
.ep[data-current="true"] .ep-title { color: var(--accent); }
.ep-title { margin: 0; font-size: 1rem; font-weight: 700; line-height: 1.38; }
.ep[data-done="true"] .ep-title { color: var(--muted); font-weight: 500; }
.ep-meta { display: flex; flex-wrap: wrap; gap: 3px 14px; margin: 5px 0 0; font-size: .8rem; color: var(--muted); font-variant-numeric: tabular-nums; }
.ep-meta span:empty { display: none; }
.badge { border: 1px solid currentColor; border-radius: 4px; padding: 0 5px; font-size: .68rem; line-height: 1.5; font-weight: 700; }
.ep-desc { margin: 8px 0 0; max-width: 68ch; color: var(--muted); font-size: .88rem; line-height: 1.55; white-space: pre-line; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.ep.open .ep-desc { display: block; }
.ep-more { margin-top: 4px; color: var(--accent); text-decoration-color: currentColor; }
.ep-bar { position: absolute; left: 62px; bottom: -1px; height: 2px; border-radius: 2px; background: linear-gradient(90deg, var(--sun-a), var(--sun-b)); }
.more-row { padding: 22px 0; text-align: center; }

/* Player */
.np { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 16px; }
.art { position: relative; flex: 1 1 0; min-height: 120px; max-height: 344px; width: auto; max-width: 100%; aspect-ratio: 1 / 1; align-self: center; border-radius: 14px; overflow: hidden; background: var(--panel-2); box-shadow: 0 24px 50px -24px rgba(0,0,0,.7); }
.np-meta { flex: none; }
.player.tr-open .np { flex: none; flex-direction: row; align-items: center; gap: 14px; }
.player.tr-open .art { flex: none; width: 84px; height: 84px; min-height: 0; border-radius: 10px; box-shadow: none; }
.player.tr-open .np-meta { flex: 1 1 auto; min-width: 0; }
.player.tr-open .p-title { font-size: 1.08rem; -webkit-line-clamp: 2; }
.player.tr-open .transcript { flex: 1 1 auto; min-height: 200px; }
.art img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.art-sun { position: absolute; inset: 18%; width: 64%; height: 64%; }
.p-show { margin: 0; color: var(--panel-muted); font-size: .84rem; }
.p-title { margin: 4px 0 0; font-family: var(--font-display); font-weight: 800; font-size: 1.32rem; line-height: 1.3; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.caption { flex: none; min-height: 5.4em; padding: 12px 14px; border-radius: 10px; background: rgba(255,255,255,.05); border: 1px solid var(--panel-line); }
.caption .speaker { display: block; margin-bottom: 2px; font-size: .72rem; color: var(--sun-a); letter-spacing: .02em; }
.caption .line { margin: 0; font-family: var(--font-display); font-weight: 600; font-size: 1.08rem; line-height: 1.55; }
.caption .line.idle { color: var(--panel-muted); }
.caption .next { margin: 6px 0 0; font-size: .86rem; line-height: 1.45; color: var(--panel-muted); opacity: .75; }
.caption .hint { margin: 0; font-size: .84rem; line-height: 1.55; color: var(--panel-muted); }
.scrub { display: flex; flex-direction: column; gap: 2px; }
.times { display: flex; justify-content: space-between; font-size: .76rem; color: var(--panel-muted); font-variant-numeric: tabular-nums; }
.transport { display: flex; align-items: center; justify-content: center; gap: clamp(6px, 1.6vw, 16px); }
.icon-btn { width: 44px; height: 44px; border: 0; border-radius: 50%; background: transparent; display: grid; place-items: center; color: inherit; }
.icon-btn:hover:not(:disabled) { background: rgba(127,127,127,.16); }
.icon-btn svg { width: 24px; height: 24px; }
.icon-btn.small { width: 34px; height: 34px; }
.icon-btn.small svg { width: 19px; height: 19px; }
.skip svg { width: 30px; height: 30px; }
.play-btn { width: 66px; height: 66px; border: 0; border-radius: 50%; display: grid; place-items: center; background: var(--panel-ink); color: var(--panel); box-shadow: 0 10px 28px -12px rgba(242,107,122,.75); transition: transform .15s; }
.play-btn svg { width: 28px; height: 28px; }
.play-btn:hover:not(:disabled) { transform: scale(1.04); }
.buffering .play-btn { animation: pulse 1.2s ease-out infinite; }
@keyframes pulse { from { box-shadow: 0 0 0 0 rgba(255,193,94,.55); } to { box-shadow: 0 0 0 14px rgba(255,193,94,0); } }
.extras { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; container-type: inline-size; }
@container (max-width: 330px) { #trBtn { width: 40px; padding: 0; justify-content: center; } #trBtn span { display: none; } }
.chip { display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 12px; border: 1px solid var(--panel-line); border-radius: 999px; background: transparent; color: var(--panel-ink); font-size: .8rem; font-variant-numeric: tabular-nums; white-space: nowrap; }
.chip svg { width: 17px; height: 17px; }
.chip:hover:not(:disabled) { border-color: rgba(255,255,255,.38); }
.chip[aria-pressed="true"] { background: rgba(255,255,255,.14); border-color: rgba(255,255,255,.3); }
#speedBtn { min-width: 58px; justify-content: center; font-weight: 700; }
.chip.icon-only { width: 40px; padding: 0; justify-content: center; }
.speed-wrap { position: relative; }
.speed-pop { position: absolute; left: 0; bottom: calc(100% + 10px); z-index: 5; width: 264px; padding: 14px; border-radius: 12px; background: var(--panel-2); border: 1px solid var(--panel-line); box-shadow: 0 18px 40px -16px rgba(0,0,0,.6); }
.speed-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }
#speedVal { font-weight: 700; font-size: 1.1rem; font-variant-numeric: tabular-nums; }
.speed-presets { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; margin-top: 10px; }
.preset { border: 1px solid var(--panel-line); background: transparent; color: var(--panel-ink); border-radius: 8px; padding: 5px 0; font-size: .8rem; font-variant-numeric: tabular-nums; }
.preset:hover { border-color: rgba(255,255,255,.38); }
.preset[aria-pressed="true"] { background: var(--panel-ink); color: var(--panel); border-color: var(--panel-ink); }
.vol { display: flex; align-items: center; gap: 6px; margin-top: -6px; }
.vol .range { flex: 1; }
.transcript { display: flex; flex-direction: column; min-height: 0; border-top: 1px solid var(--panel-line); padding-top: 12px; }
.player.tr-open #trList, .player.tr-open .tr-text { flex: 1 1 auto; min-height: 0; max-height: none; }
.tr-head { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; margin-bottom: 8px; }
.tr-head p { margin: 0; font-size: .84rem; color: var(--panel-muted); }
#trList { position: relative; list-style: none; margin: 0; padding: 0; max-height: min(46vh, 420px); overflow: auto; overscroll-behavior: contain; }
.cue { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 10px; padding: 6px 8px; border-radius: 8px; font-size: .88rem; line-height: 1.5; color: var(--panel-muted); cursor: pointer; }
.cue:hover { background: rgba(255,255,255,.05); }
.cue.active { background: rgba(255,255,255,.1); color: var(--panel-ink); }
.cue time { padding-top: 3px; font-size: .72rem; font-variant-numeric: tabular-nums; opacity: .85; }
.cue b { color: var(--sun-a); font-weight: 500; }
.cue-empty { padding: 6px 0; color: var(--panel-muted); font-size: .86rem; }
.tr-text { max-height: min(46vh, 420px); overflow: auto; white-space: pre-line; font-size: .9rem; line-height: 1.65; color: var(--panel-muted); }
.p-foot { margin-top: auto; padding-top: 4px; font-size: .8rem; color: var(--panel-muted); }
.switch { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; }
.switch input { width: 16px; height: 16px; margin: 0; accent-color: var(--sun-b); }

/* Sliders */
.range { -webkit-appearance: none; appearance: none; width: 100%; height: 22px; margin: 0; background: transparent; cursor: pointer; --fill: 0%; --buf: 0%; }
.range:disabled { cursor: default; opacity: .55; }
.range::-webkit-slider-runnable-track { height: 5px; border-radius: 99px; background: linear-gradient(90deg, var(--sun-a), var(--sun-b) 55%, var(--sun-c)) 0 0 / var(--fill) 100% no-repeat, linear-gradient(var(--track-buf), var(--track-buf)) 0 0 / var(--buf) 100% no-repeat, var(--track); }
.range::-moz-range-track { height: 5px; border-radius: 99px; background: linear-gradient(90deg, var(--sun-a), var(--sun-b) 55%, var(--sun-c)) 0 0 / var(--fill) 100% no-repeat, linear-gradient(var(--track-buf), var(--track-buf)) 0 0 / var(--buf) 100% no-repeat, var(--track); }
.range::-webkit-slider-thumb { -webkit-appearance: none; width: 15px; height: 15px; margin-top: -5px; border: 0; border-radius: 50%; background: var(--thumb); box-shadow: 0 0 0 3px rgba(242,107,122,.35); }
.range::-moz-range-thumb { width: 15px; height: 15px; border: 0; border-radius: 50%; background: var(--thumb); box-shadow: 0 0 0 3px rgba(242,107,122,.35); }
.range:focus-visible { outline: none; }
.range:focus-visible::-webkit-slider-thumb { box-shadow: 0 0 0 4px var(--sun-a); }
.range:focus-visible::-moz-range-thumb { box-shadow: 0 0 0 4px var(--sun-a); }
.range.thin::-webkit-slider-runnable-track { height: 4px; }
.range.thin::-moz-range-track { height: 4px; }
.range.thin::-webkit-slider-thumb { width: 13px; height: 13px; margin-top: -4.5px; }

/* Toast */
.toast { position: fixed; left: 50%; bottom: calc(24px + env(safe-area-inset-bottom, 0px)); z-index: 60; max-width: min(92vw, 460px); padding: 10px 16px; border-radius: 10px; background: var(--ink); color: var(--bg); font-size: .86rem; box-shadow: 0 12px 30px -12px rgba(0,0,0,.45); opacity: 0; pointer-events: none; transform: translate(-50%, 16px); transition: opacity .2s, transform .2s; }
.toast.show { opacity: 1; transform: translate(-50%, 0); }
.toast.error { background: var(--accent); color: var(--on-accent); }

/* Medium screens: library becomes a strip above the episodes */
@media (max-width: 1179px) {
  .library { flex-direction: row; flex-wrap: wrap; align-items: center; gap: 10px 16px; padding: 14px 22px; }
  .brand { order: 0; }
  .library-foot { order: 1; margin: 0 0 0 auto; padding: 0; border-top: 0; gap: 14px; }
  .add { order: 2; flex: 1 1 100%; }
  .results { order: 3; flex: 1 1 100%; }
  .shows { order: 4; flex: 1 1 100%; flex-direction: row; overflow-x: auto; gap: 4px; padding-bottom: 2px; scrollbar-width: thin; }
  .library-empty { order: 5; flex: 1 1 100%; }
  .show-btn { grid-template-columns: 1fr; align-items: start; gap: 6px; width: 78px; padding: 5px; }
  .show-btn .cover { width: 68px; height: 68px; }
  .show-author { display: none; }
  .show-title { font-size: .74rem; font-weight: 500; }
}
@media (min-width: 860px) and (max-width: 1179px) {
  .app { grid-template-columns: minmax(0, 1fr) 360px; grid-template-rows: auto minmax(0, 1fr); }
  .full { padding-left: 24px; padding-right: 24px; }
  .chip { padding: 0 10px; gap: 5px; }
  #speedBtn { min-width: 50px; }
  .library { grid-column: 1; grid-row: 1; border-right: 0; border-bottom: 1px solid var(--line); overflow: visible; }
  .episodes { grid-column: 1; grid-row: 2; }
  .player { grid-column: 2; grid-row: 1 / 3; }
}
/* Phones: player docks at the bottom and opens as a sheet */
@media (max-width: 859px) {
  html, body { height: auto; }
  .app { display: block; height: auto; }
  .library { border: 0; padding: 16px 16px 6px; background: var(--bg); }
  .episodes { overflow: visible; padding: 14px 16px calc(96px + env(safe-area-inset-bottom, 0px)); }
  .show-head { grid-template-columns: 96px minmax(0, 1fr); gap: 16px; }
  .cover.big { width: 96px; height: 96px; font-size: 2rem; }
  .player { position: fixed; left: 0; right: 0; bottom: 0; z-index: 40; overflow: visible; box-shadow: 0 -10px 30px -12px rgba(0,0,0,.35); }
  .player .full { display: none; }
  .mini { position: relative; display: grid; grid-template-columns: 44px minmax(0, 1fr) 44px; align-items: center; gap: 12px; padding: 10px 12px calc(10px + env(safe-area-inset-bottom, 0px)) 14px; }
  .player.expanded { top: 0; overflow: auto; overscroll-behavior: contain; }
  .player.expanded .mini { display: none; }
  .player.expanded .full { display: flex; height: 100%; padding: calc(8px + env(safe-area-inset-top, 0px)) 20px calc(20px + env(safe-area-inset-bottom, 0px)); }
  .art { max-height: 340px; }
  .full-top { display: flex; }
  .vol { display: none; }
  body.sheet-open { overflow: hidden; }
  .toast { bottom: calc(84px + env(safe-area-inset-bottom, 0px)); }
}
.mini-meta { display: flex; flex-direction: column; min-width: 0; padding: 0; border: 0; background: none; text-align: left; color: var(--panel-ink); }
.mini-title { font-weight: 700; font-size: .88rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mini-show { font-size: .75rem; color: var(--panel-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mini-progress { position: absolute; left: 0; right: 0; top: 0; height: 2px; background: rgba(255,255,255,.12); }
.mini-progress span { display: block; width: 0; height: 100%; background: linear-gradient(90deg, var(--sun-a), var(--sun-b)); }
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation: none !important; transition: none !important; }
}
</style>
</head>
<body>
<svg class="sprite" aria-hidden="true" focusable="false">
  <defs>
    <symbol id="i-play" viewBox="0 0 24 24"><path fill="currentColor" d="M8 5.6v12.8a1 1 0 0 0 1.52.85l10.2-6.4a1 1 0 0 0 0-1.7L9.52 4.75A1 1 0 0 0 8 5.6z"/></symbol>
    <symbol id="i-pause" viewBox="0 0 24 24"><rect fill="currentColor" x="6.5" y="5" width="4" height="14" rx="1.2"/><rect fill="currentColor" x="13.5" y="5" width="4" height="14" rx="1.2"/></symbol>
    <symbol id="i-prev" viewBox="0 0 24 24"><rect fill="currentColor" x="5" y="6" width="2.2" height="12" rx="1"/><path fill="currentColor" d="M18.5 6.9v10.2a.9.9 0 0 1-1.38.76L9.6 12.76a.9.9 0 0 1 0-1.52l7.52-5.1a.9.9 0 0 1 1.38.76z"/></symbol>
    <symbol id="i-next" viewBox="0 0 24 24"><rect fill="currentColor" x="16.8" y="6" width="2.2" height="12" rx="1"/><path fill="currentColor" d="M5.5 6.9v10.2a.9.9 0 0 0 1.38.76l7.52-5.1a.9.9 0 0 0 0-1.52L6.88 6.14a.9.9 0 0 0-1.38.76z"/></symbol>
    <symbol id="i-vol" viewBox="0 0 24 24"><path fill="currentColor" d="M4 9.2h3.3l4.4-3.7a.6.6 0 0 1 1 .46v12.08a.6.6 0 0 1-1 .46L7.3 14.8H4a1 1 0 0 1-1-1v-3.6a1 1 0 0 1 1-1z"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M15.5 9.2a4 4 0 0 1 0 5.6M18.2 6.6a7.6 7.6 0 0 1 0 10.8"/></symbol>
    <symbol id="i-mute" viewBox="0 0 24 24"><path fill="currentColor" d="M4 9.2h3.3l4.4-3.7a.6.6 0 0 1 1 .46v12.08a.6.6 0 0 1-1 .46L7.3 14.8H4a1 1 0 0 1-1-1v-3.6a1 1 0 0 1 1-1z"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M16 9.5l5 5M21 9.5l-5 5"/></symbol>
    <symbol id="i-cc" viewBox="0 0 24 24"><rect fill="none" stroke="currentColor" stroke-width="1.7" x="3" y="5.5" width="18" height="13" rx="2.5"/><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M10.6 10.3a2.1 2.1 0 1 0 0 3.4M16.6 10.3a2.1 2.1 0 1 0 0 3.4"/></symbol>
    <symbol id="i-lines" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M5 7h14M5 11.5h14M5 16h9"/></symbol>
    <symbol id="i-moon" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M19.5 14.6A7.6 7.6 0 0 1 9.4 4.5a7.6 7.6 0 1 0 10.1 10.1z"/></symbol>
    <symbol id="i-search" viewBox="0 0 24 24"><circle fill="none" stroke="currentColor" stroke-width="1.8" cx="10.5" cy="10.5" r="6"/><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M15 15l4.5 4.5"/></symbol>
    <symbol id="i-refresh" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4.2h-4.2"/></symbol>
    <symbol id="i-sort" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M8 5v14M5 16l3 3 3-3M16 19V5M13 8l3-3 3 3"/></symbol>
    <symbol id="i-trash" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M4.5 7h15M10 7V4.8h4V7M6.5 7l.9 12.2h9.2L17.5 7"/></symbol>
    <symbol id="i-link" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1"/></symbol>
    <symbol id="i-down" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M6 9l6 6 6-6"/></symbol>
    <symbol id="i-close" viewBox="0 0 24 24"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" d="M6.5 6.5l11 11M17.5 6.5l-11 11"/></symbol>
    <symbol id="i-theme" viewBox="0 0 24 24"><circle fill="none" stroke="currentColor" stroke-width="1.7" cx="12" cy="12" r="8"/><path fill="currentColor" d="M12 4a8 8 0 0 1 0 16z"/></symbol>
  </defs>
</svg>

<div class="app" id="app">
  <aside class="library" aria-label="Library">
    <div class="brand">
      <h1>夜更かしラジオ</h1>
      <button class="icon-btn small" id="themeBtn" type="button" aria-label="Theme: follow system" title="Theme: follow system"><svg aria-hidden="true"><use href="#i-theme"/></svg></button>
    </div>
    <form class="add" id="addForm" autocomplete="off">
      <label class="visually-hidden" for="addInput">Search podcasts, or paste a feed or Apple Podcasts link</label>
      <svg aria-hidden="true"><use href="#i-search"/></svg>
      <input id="addInput" type="text" inputmode="search" enterkeyhint="search" spellcheck="false" placeholder="Name or feed link">
      <button type="submit" id="addBtn">Search</button>
    </form>
    <section class="results" id="results" aria-label="Search results" hidden>
      <div class="results-head">
        <p id="resultsTitle"></p>
        <button class="icon-btn small" id="resultsClose" type="button" aria-label="Close search results"><svg aria-hidden="true"><use href="#i-close"/></svg></button>
      </div>
      <ul id="resultsList"></ul>
    </section>
    <ul class="shows" id="shows" aria-label="Your shows"></ul>
    <div class="library-empty" id="libraryEmpty" hidden>
      <p>Your library is empty. Search for a show by name, or paste its RSS feed or Apple Podcasts link.</p>
      <p>Try one from your poster:</p>
      <div class="try" id="tryChips"></div>
    </div>
    <div class="library-foot">
      <button type="button" class="text-btn" id="importBtn">Import OPML</button>
      <button type="button" class="text-btn" id="exportBtn">Export OPML</button>
    </div>
  </aside>

  <main class="episodes" id="episodes" aria-label="Episodes">
    <div class="welcome" id="welcome">
      <h2>Choose a show to begin</h2>
      <p>Search by name or paste a feed link. Episodes appear here, and the player keeps your place, speed and captions for every episode.</p>
      <dl class="keys">
        <dt><kbd>Space</kbd></dt><dd>Play or pause</dd>
        <dt><kbd>←</kbd> <kbd>→</kbd></dt><dd>Back 15 seconds, forward 30 seconds</dd>
        <dt><kbd>[</kbd> <kbd>]</kbd></dt><dd>Slower, faster</dd>
        <dt><kbd>C</kbd> <kbd>T</kbd></dt><dd>Captions, transcript</dd>
        <dt><kbd>M</kbd></dt><dd>Mute</dd>
        <dt><kbd>Shift</kbd> + <kbd>N</kbd> / <kbd>P</kbd></dt><dd>Next or previous episode</dd>
      </dl>
    </div>
    <header class="show-head" id="showHead" hidden>
      <div id="showCover"></div>
      <div>
        <h2 id="showTitle"></h2>
        <p class="byline" id="showAuthor"></p>
        <p class="about" id="showAbout"></p>
        <div class="head-actions">
          <button class="pill" id="refreshBtn" type="button"><svg aria-hidden="true"><use href="#i-refresh"/></svg><span>Refresh</span></button>
          <button class="pill" id="sortBtn" type="button"><svg aria-hidden="true"><use href="#i-sort"/></svg><span>Newest first</span></button>
          <a class="pill" id="siteLink" href="#" target="_blank" rel="noopener noreferrer" hidden><svg aria-hidden="true"><use href="#i-link"/></svg><span>Website</span></a>
          <button class="pill danger" id="removeBtn" type="button"><svg aria-hidden="true"><use href="#i-trash"/></svg><span>Remove</span></button>
        </div>
      </div>
    </header>
    <p class="list-status" id="listStatus" hidden></p>
    <ol class="episode-list" id="episodeList" aria-label="Episode list"></ol>
  </main>

  <section class="player" id="player" aria-label="Now playing">
    <div class="mini" id="mini">
      <div id="miniCover"></div>
      <button class="mini-meta" id="miniExpand" type="button" aria-expanded="false" aria-label="Open the player">
        <span class="mini-title" id="miniTitle">Nothing playing</span>
        <span class="mini-show" id="miniShow"></span>
      </button>
      <button class="icon-btn" id="miniPlay" type="button" aria-label="Play" disabled><svg aria-hidden="true"><use href="#i-play"/></svg></button>
      <div class="mini-progress" aria-hidden="true"><span id="miniBar"></span></div>
    </div>

    <div class="full" id="full">
      <div class="full-top">
        <button class="icon-btn" id="collapseBtn" type="button" aria-label="Close the player"><svg aria-hidden="true"><use href="#i-down"/></svg></button>
      </div>
      <div class="np">
      <div class="art">
        <img id="pCover" alt="" hidden>
        <svg class="art-sun" viewBox="0 0 200 200" aria-hidden="true">
          <defs>
            <linearGradient id="sunGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stop-color="#FFC15E"/><stop offset=".55" stop-color="#F26B7A"/><stop offset="1" stop-color="#B2418F"/>
            </linearGradient>
            <mask id="sunCuts">
              <rect width="200" height="200" fill="#fff"/>
              <rect y="112" width="200" height="4" fill="#000"/><rect y="130" width="200" height="6" fill="#000"/>
              <rect y="150" width="200" height="8" fill="#000"/><rect y="172" width="200" height="10" fill="#000"/>
            </mask>
          </defs>
          <circle cx="100" cy="100" r="92" fill="url(#sunGrad)" mask="url(#sunCuts)"/>
        </svg>
      </div>
      <div class="np-meta">
        <p class="p-show" id="pShow">Nothing playing</p>
        <h2 class="p-title" id="pTitle">Pick an episode to start listening.</h2>
      </div>
      </div>
      <div class="caption" id="caption" hidden></div>
      <div class="scrub">
        <input type="range" class="range" id="seek" min="0" max="1000" step="1" value="0" aria-label="Position in episode" disabled>
        <div class="times"><span id="tCur">0:00</span><span id="tLeft">-0:00</span></div>
      </div>
      <div class="transport">
        <button class="icon-btn" id="prevBtn" type="button" aria-label="Previous episode" title="Previous episode (Shift+P)" disabled><svg aria-hidden="true"><use href="#i-prev"/></svg></button>
        <button class="icon-btn skip" id="backBtn" type="button" aria-label="Back 15 seconds" title="Back 15 seconds (←)" disabled>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" d="M1 4v6h6M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/><text x="12.6" y="15.3" text-anchor="middle" font-size="7.2" font-weight="700" fill="currentColor" font-family="system-ui, sans-serif">15</text></svg>
        </button>
        <button class="play-btn" id="playBtn" type="button" aria-label="Play" title="Play (Space)" disabled><svg aria-hidden="true"><use href="#i-play"/></svg></button>
        <button class="icon-btn skip" id="fwdBtn" type="button" aria-label="Forward 30 seconds" title="Forward 30 seconds (→)" disabled>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" d="M23 4v6h-6M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/><text x="11.4" y="15.3" text-anchor="middle" font-size="7.2" font-weight="700" fill="currentColor" font-family="system-ui, sans-serif">30</text></svg>
        </button>
        <button class="icon-btn" id="nextBtn" type="button" aria-label="Next episode" title="Next episode (Shift+N)" disabled><svg aria-hidden="true"><use href="#i-next"/></svg></button>
      </div>
      <div class="extras">
        <div class="speed-wrap">
          <button class="chip" id="speedBtn" type="button" aria-haspopup="dialog" aria-expanded="false" aria-controls="speedPop" title="Playback speed ([ and ])">1×</button>
          <div class="speed-pop" id="speedPop" role="dialog" aria-label="Playback speed" hidden>
            <div class="speed-row"><span id="speedVal">1.00×</span><button class="text-btn light" id="speedReset" type="button">Reset to 1×</button></div>
            <input type="range" class="range thin" id="speedRange" min="0.5" max="3" step="0.05" value="1" aria-label="Playback speed">
            <div class="speed-presets" id="speedPresets"></div>
          </div>
        </div>
        <button class="chip icon-only" id="ccBtn" type="button" aria-pressed="true" aria-label="Captions" title="Captions (C)"><svg aria-hidden="true"><use href="#i-cc"/></svg></button>
        <button class="chip" id="trBtn" type="button" aria-pressed="false" aria-label="Transcript" title="Transcript (T)"><svg aria-hidden="true"><use href="#i-lines"/></svg><span>Transcript</span></button>
        <button class="chip" id="sleepBtn" type="button" aria-pressed="false" title="Sleep timer: press to cycle 15, 30, 45, 60 minutes, end of episode, off"><svg aria-hidden="true"><use href="#i-moon"/></svg><span id="sleepLabel">Sleep</span></button>
      </div>
      <div class="vol">
        <button class="icon-btn small" id="muteBtn" type="button" aria-label="Mute" title="Mute (M)"><svg aria-hidden="true"><use href="#i-vol"/></svg></button>
        <input type="range" class="range thin" id="volume" min="0" max="1" step="0.01" value="1" aria-label="Volume">
      </div>
      <section class="transcript" id="transcript" aria-label="Transcript" hidden>
        <div class="tr-head"><p id="trTitle">Transcript</p><button class="text-btn light" id="trLoad" type="button">Load a caption file</button></div>
        <ol id="trList"></ol>
        <div class="tr-text" id="trText" hidden></div>
      </section>
      <div class="p-foot">
        <label class="switch"><input type="checkbox" id="autoNext"><span>Play the next episode automatically</span></label>
      </div>
      <audio id="audio" preload="metadata"></audio>
    </div>
  </section>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<input type="file" id="capFile" accept=".vtt,.srt,.json,text/vtt,application/x-subrip,application/json" hidden>
<input type="file" id="opmlFile" accept=".opml,.xml,text/xml,text/x-opml" hidden>

<script>
(() => {
'use strict';

/* ---------- helpers ---------- */
const $ = id => document.getElementById(id);
const audio = $('audio');
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
const isNarrow = () => matchMedia('(max-width: 859px)').matches;

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch { return false; } },
  del(k) { try { localStorage.removeItem(k); } catch {} }
};
function fnv(str) { let h = 0x811c9dc5; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193); } return (h >>> 0).toString(36); }
const KEY = {
  shows: 'ah.shows', settings: 'ah.settings', progress: 'ah.progress', last: 'ah.last',
  cache: u => 'ah.cache.' + fnv(u), cap: id => 'ah.cap.' + fnv(id)
};

function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
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
function icon(name) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('aria-hidden', 'true');
  const u = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  u.setAttribute('href', '#i-' + name);
  s.append(u);
  return s;
}
function setIcon(btn, name) { const u = btn && btn.querySelector('use'); if (u) u.setAttribute('href', '#i-' + name); }
const firstChar = t => (Array.from((t || '').trim())[0] || '♪').toUpperCase();
function cover(src, title, cls = 'cover') {
  const wrap = el('span', { class: cls });
  const fallback = () => { wrap.replaceChildren(el('span', { text: firstChar(title) })); wrap.classList.add('fallback'); };
  if (src) {
    const img = el('img', { src, alt: '', loading: 'lazy', decoding: 'async' });
    img.addEventListener('error', fallback, { once: true });
    wrap.append(img);
  } else fallback();
  return wrap;
}
function fmtTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}` : `${m}:${String(sec).padStart(2, '0')}`;
}
function fmtDur(s) {
  if (!s) return '';
  const m = Math.round(s / 60);
  if (m < 60) return `${Math.max(1, m)} min`;
  const h = Math.floor(m / 60), r = m % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}
const dShort = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' });
const dLong = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
const fmtDate = ms => !ms ? '' : (new Date(ms).getFullYear() === new Date().getFullYear() ? dShort : dLong).format(ms);
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
  toastTimer = setTimeout(() => t.classList.remove('show'), isError ? 6500 : 3200);
}

/* ---------- state ---------- */
const DEFAULTS = { speed: 1, volume: 1, muted: false, captions: true, autoNext: true, theme: 'system', sort: 'newest' };
const settings = Object.assign({}, DEFAULTS, store.get(KEY.settings, {}));
let shows = store.get(KEY.shows, []);
let progress = store.get(KEY.progress, {});
const S = {
  showUrl: null, episodes: [], view: [], shown: 80, refreshing: null,
  queue: [], current: null, pendingSeek: null,
  cues: [], untimed: '', cueIdx: -2, capLoading: false, capError: '', activeCue: null,
  dragging: false, lastSecond: -1, raf: 0, lastUserScroll: 0,
  sleep: { mode: 'off', endsAt: 0, timer: 0, fadeBase: null }
};
const saveSettings = () => store.set(KEY.settings, settings);
const saveShows = () => store.set(KEY.shows, shows);
const currentShow = () => shows.find(s => s.feedUrl === S.showUrl) || null;
const sortList = eps => settings.sort === 'oldest' ? [...eps].reverse() : [...eps];

/* ---------- network (through the local helper, which avoids browser CORS limits) ---------- */
const VIA_HELPER = location.protocol === 'http:' || location.protocol === 'https:';
function decodeBytes(buf, type) {
  const head = new TextDecoder('latin1').decode(new Uint8Array(buf, 0, Math.min(buf.byteLength, 300)));
  const xmlEnc = (head.match(/<\?xml[^>]*encoding=["']([^"']+)["']/i) || [])[1];
  const httpEnc = (type.match(/charset=["']?([^;"']+)/i) || [])[1];
  const enc = (xmlEnc || httpEnc || 'utf-8').trim().toLowerCase();
  try { return new TextDecoder(enc).decode(buf); } catch { return new TextDecoder('utf-8').decode(buf); }
}
async function fetchText(url) {
  let res;
  try { res = await fetch(VIA_HELPER ? '/api/fetch?url=' + encodeURIComponent(url) : url, { cache: 'no-store' }); }
  catch { throw new Error(VIA_HELPER ? 'The player’s helper isn’t answering. Is afterhours.py still running?' : 'The browser blocked that request. Open the player by running afterhours.py.'); }
  if (!res.ok) {
    let msg = '';
    try { msg = (await res.json()).error || ''; } catch {}
    throw new Error(msg || `The site answered with an error (${res.status}).`);
  }
  const type = res.headers.get('Content-Type') || '';
  return { text: decodeBytes(await res.arrayBuffer(), type), type };
}

/* ---------- feed parsing ---------- */
const NS = {
  itunes: ['itunes.com'], podcast: ['podcastindex'], content: ['purl.org/rss/1.0/modules/content'],
  media: ['search.yahoo.com/mrss'], dc: ['purl.org/dc/elements']
};
function inNS(n, key) {
  if (!key) return !n.prefix;
  if ((n.prefix || '').toLowerCase() === key) return true;
  const uri = (n.namespaceURI || '').toLowerCase();
  return NS[key].some(s => uri.includes(s));
}
function kidsNamed(parent, local, ns) {
  const out = [];
  if (!parent) return out;
  for (const n of parent.children) if (n.localName === local && inNS(n, ns)) out.push(n);
  return out;
}
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
    if (!url) {
      const mc = kidsNamed(item, 'content', 'media').find(m => /audio|video/i.test(attr(m, 'type') + attr(m, 'medium')));
      url = attr(mc, 'url'); type = attr(mc, 'type');
    }
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
      desc: clip(htmlToText(txt(kid(item, 'encoded', 'content')) || txt(kid(item, 'description')) || txt(kid(item, 'summary', 'itunes'))), 2400),
      transcripts: kidsNamed(item, 'transcript', 'podcast')
        .map(t => ({ url: abs(attr(t, 'url'), feedUrl), type: attr(t, 'type').toLowerCase(), lang: attr(t, 'language') }))
        .filter(t => t.url)
    });
  }
  episodes.sort((a, b) => b.date - a.date);
  return { show, episodes };
}

/* ---------- captions and transcripts ---------- */
function parseClock(s) {
  const parts = String(s).trim().replace(',', '.').split(':');
  let sec = 0;
  for (const p of parts) sec = sec * 60 + parseFloat(p);
  return sec;
}
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
  const score = t => {
    const u = t.url.toLowerCase().split('?')[0], ty = t.type;
    if (ty.includes('vtt') || u.endsWith('.vtt')) return 5;
    if (ty.includes('srt') || ty.includes('subrip') || u.endsWith('.srt')) return 4;
    if (ty.includes('json') || u.endsWith('.json')) return 3;
    if (ty.includes('html') || u.endsWith('.html')) return 2;
    return 1;
  };
  return [...list].sort((a, b) => score(b) - score(a))[0];
}
async function loadCaptions(ep) {
  S.cues = []; S.untimed = ''; S.cueIdx = -2; S.capError = ''; S.capLoading = false;
  const saved = store.get(KEY.cap(ep.id), null);
  if (saved && saved.text) applyTranscript(saved.text, saved.type);
  else {
    const t = pickTranscript(ep.transcripts);
    if (t) {
      S.capLoading = true;
      renderTranscript(); renderCaption(true);
      try {
        const res = await fetchText(t.url);
        if (!S.current || S.current.ep.id !== ep.id) return;
        applyTranscript(res.text, t.type || res.type);
      } catch {
        if (S.current && S.current.ep.id === ep.id) S.capError = 'This episode’s captions couldn’t be loaded.';
      }
      if (!S.current || S.current.ep.id !== ep.id) return;
      S.capLoading = false;
    }
  }
  renderTranscript();
  renderCaption(true);
}
function findCue(t) {
  const c = S.cues;
  let lo = 0, hi = c.length - 1, ans = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (c[mid].start <= t) { ans = mid; lo = mid + 1; } else hi = mid - 1; }
  return ans;
}
function captionHint() {
  if (S.capLoading) return el('p', { class: 'hint', text: 'Loading captions…' });
  const msg = S.capError || (S.untimed ? 'This transcript isn’t timed, so it appears in the transcript panel.' : 'No captions for this episode.');
  return el('p', { class: 'hint' }, msg + ' ', S.untimed ? null : el('button', { class: 'text-btn light', type: 'button', text: 'Load a .vtt or .srt file', onclick: () => $('capFile').click() }));
}
function renderCaption(force) {
  const box = $('caption');
  if (!S.current || !settings.captions) { box.hidden = true; return; }
  box.hidden = false;
  if (!S.cues.length) { if (force) box.replaceChildren(captionHint()); return; }
  const t = S.pendingSeek != null ? S.pendingSeek : audio.currentTime;
  const found = findCue(t);
  let i = found;
  if (i >= 0 && t > S.cues[i].end + 1.2) i = -1;
  if (i === S.cueIdx && !force) return;
  S.cueIdx = i;
  const cur = i >= 0 ? S.cues[i] : null;
  const next = S.cues[(i >= 0 ? i : found) + 1];
  box.replaceChildren(
    cur && cur.speaker ? el('span', { class: 'speaker', text: cur.speaker }) : null,
    el('p', { class: cur ? 'line' : 'line idle', text: cur ? cur.text : '…' }),
    next ? el('p', { class: 'next', text: next.text }) : null
  );
  highlightCue(i);
}
function renderTranscript() {
  const list = $('trList'), box = $('trText');
  list.replaceChildren();
  box.hidden = true;
  S.activeCue = null;
  if (S.cues.length) {
    list.hidden = false;
    const frag = document.createDocumentFragment();
    let prevSpeaker = '';
    S.cues.forEach((c, i) => {
      const who = c.speaker && c.speaker !== prevSpeaker ? el('b', { text: c.speaker + '  ' }) : null;
      prevSpeaker = c.speaker || prevSpeaker;
      frag.append(el('li', { class: 'cue', dataset: { i } }, el('time', { text: fmtTime(c.start) }), el('span', {}, who, c.text)));
    });
    list.append(frag);
    $('trTitle').textContent = `Transcript, ${S.cues.length} lines`;
  } else if (S.untimed) {
    list.hidden = true;
    box.hidden = false;
    box.textContent = S.untimed;
    $('trTitle').textContent = 'Transcript';
  } else {
    list.hidden = false;
    list.append(el('li', { class: 'cue-empty', text: !S.current ? 'Play an episode to see its transcript.' : S.capLoading ? 'Loading…' : 'No transcript for this episode. You can load your own caption file.' }));
    $('trTitle').textContent = 'Transcript';
  }
  if (S.cueIdx >= 0) highlightCue(S.cueIdx);
}
function highlightCue(i) {
  if (S.activeCue) S.activeCue.classList.remove('active');
  S.activeCue = null;
  if (i < 0) return;
  const li = $('trList').children[i];
  if (!li || !li.classList.contains('cue')) return;
  li.classList.add('active');
  S.activeCue = li;
  if (!$('transcript').hidden && Date.now() - S.lastUserScroll > 4000) {
    const list = $('trList');
    list.scrollTo({ top: li.offsetTop - list.clientHeight / 2 + li.clientHeight / 2, behavior: reduceMotion ? 'auto' : 'smooth' });
  }
}

/* ---------- library ---------- */
function cacheEpisodes(feedUrl, eps) {
  const data = { fetchedAt: Date.now(), episodes: eps.slice(0, 500) };
  if (store.set(KEY.cache(feedUrl), data)) return;
  data.episodes = eps.slice(0, 120).map(e => Object.assign({}, e, { desc: clip(e.desc, 400) }));
  if (store.set(KEY.cache(feedUrl), data)) return;
  for (const s of shows) if (s.feedUrl !== feedUrl) store.del(KEY.cache(s.feedUrl));
  store.set(KEY.cache(feedUrl), data);
}
let busyCount = 0;
function addLabel() {
  $('addBtn').disabled = busyCount > 0;
  $('addBtn').textContent = busyCount > 0 ? 'Loading…' : (looksLikeLink($('addInput').value.trim()) ? 'Add' : 'Search');
}
function busy(on) {
  busyCount = Math.max(0, busyCount + (on ? 1 : -1));
  addLabel();
}
async function subscribe(feedUrl, opts = {}) {
  const existing = shows.find(s => s.feedUrl === feedUrl);
  if (existing) { if (!opts.quiet) { selectShow(feedUrl); toast('That show is already in your library.'); } return true; }
  busy(true);
  try {
    const { text } = await fetchText(feedUrl);
    const parsed = parseFeed(text, feedUrl);
    const show = Object.assign({ feedUrl, addedAt: Date.now() }, parsed.show);
    if (!show.image && opts.image) show.image = opts.image;
    shows.unshift(show);
    saveShows();
    cacheEpisodes(feedUrl, parsed.episodes);
    if (opts.quiet) renderShows();
    else { selectShow(feedUrl, { episodes: parsed.episodes }); toast(`Added ${show.title}.`); }
    return true;
  } catch (e) {
    if (!opts.quiet) toast(`Couldn’t add that show. ${e.message}`, true);
    return false;
  } finally { busy(false); }
}
function renderShows() {
  const ul = $('shows');
  ul.replaceChildren();
  $('libraryEmpty').hidden = shows.length > 0;
  for (const s of shows) {
    ul.append(el('li', {}, el('button', {
      class: 'show-btn', type: 'button', title: s.title,
      'aria-current': s.feedUrl === S.showUrl ? 'true' : 'false',
      onclick: () => selectShow(s.feedUrl)
    }, cover(s.image, s.title), el('span', { class: 'show-text' }, el('span', { class: 'show-title', text: s.title }), el('span', { class: 'show-author', text: s.author || '' })))));
  }
}
function selectShow(feedUrl, opts = {}) {
  const changed = S.showUrl !== feedUrl;
  S.showUrl = feedUrl;
  if (changed) S.shown = 80;
  const cached = store.get(KEY.cache(feedUrl), null);
  S.episodes = opts.episodes || (cached && cached.episodes) || [];
  store.set(KEY.last, Object.assign(store.get(KEY.last, {}), { showUrl: feedUrl }));
  renderShows(); renderShowHead(); renderEpisodes();
  if (changed) { $('episodes').scrollTop = 0; if (isNarrow()) $('showHead').scrollIntoView({ block: 'start' }); }
  if (!opts.episodes && (!cached || Date.now() - cached.fetchedAt > 30 * 60 * 1000)) refreshShow(feedUrl, true);
}
async function refreshShow(feedUrl, quiet) {
  S.refreshing = feedUrl;
  $('refreshBtn').classList.add('spin');
  if (!S.episodes.length) renderEpisodes();
  try {
    const { text } = await fetchText(feedUrl);
    const { show: meta, episodes } = parseFeed(text, feedUrl);
    const s = shows.find(x => x.feedUrl === feedUrl);
    if (s) { Object.assign(s, { title: meta.title, author: meta.author, image: meta.image || s.image, description: meta.description, link: meta.link }); saveShows(); }
    cacheEpisodes(feedUrl, episodes);
    if (S.showUrl === feedUrl) { S.episodes = episodes; renderShows(); renderShowHead(); renderEpisodes(); }
    if (!quiet) toast('This show is up to date.');
  } catch (e) {
    toast(`Couldn’t refresh. ${e.message}`, true);
  } finally {
    if (S.refreshing === feedUrl) S.refreshing = null;
    $('refreshBtn').classList.remove('spin');
    if (S.showUrl === feedUrl && !S.episodes.length) renderEpisodes();
  }
}
function renderShowHead() {
  const show = currentShow();
  $('welcome').hidden = !!show;
  $('showHead').hidden = !show;
  if (!show) { $('episodeList').replaceChildren(); $('listStatus').hidden = true; return; }
  $('showCover').replaceChildren(cover(show.image, show.title, 'cover big'));
  $('showTitle').textContent = show.title;
  $('showAuthor').textContent = show.author || '';
  $('showAbout').textContent = show.description || '';
  $('sortBtn').querySelector('span').textContent = settings.sort === 'oldest' ? 'Oldest first' : 'Newest first';
  const site = $('siteLink');
  site.hidden = !/^https?:\/\//i.test(show.link || '');
  if (!site.hidden) site.href = show.link;
}
function stateText(pr) {
  if (!pr) return '';
  if (pr.done) return 'Played';
  if (pr.p > 30 && pr.d) return `${fmtDur(pr.d - pr.p)} left`;
  return '';
}
function setBar(bar, pr) {
  const pct = pr && !pr.done && pr.d ? Math.min(100, pr.p / pr.d * 100) : 0;
  bar.style.width = pct + '%';
  bar.hidden = pct < 0.5;
}
function episodeRow(ep, i) {
  const pr = progress[ep.id];
  const li = el('li', { class: 'ep', dataset: { id: ep.id, i, done: pr && pr.done ? 'true' : 'false' } });
  const bar = el('span', { class: 'ep-bar', 'aria-hidden': 'true' });
  setBar(bar, pr);
  li.append(
    el('button', { class: 'ep-play', type: 'button', 'aria-label': `Play ${ep.title}` }, icon('play')),
    el('div', { class: 'ep-body' },
      el('h3', { class: 'ep-title', text: ep.title }),
      el('p', { class: 'ep-meta' },
        el('span', { text: fmtDate(ep.date) }),
        el('span', { text: ep.number ? `Episode ${ep.number}` : '' }),
        el('span', { text: fmtDur(ep.duration) }),
        ep.transcripts && ep.transcripts.length ? el('span', { class: 'badge', title: 'Has captions', text: 'CC' }) : null,
        el('span', { class: 'ep-state', text: stateText(pr) })
      ),
      ep.desc ? el('p', { class: 'ep-desc', text: ep.desc }) : null,
      ep.desc && ep.desc.length > 150 ? el('button', { class: 'text-btn ep-more', type: 'button', 'aria-expanded': 'false', text: 'More' }) : null
    ),
    bar
  );
  return li;
}
function renderEpisodes() {
  const ol = $('episodeList');
  ol.replaceChildren();
  const show = currentShow();
  if (!show) return;
  S.view = sortList(S.episodes);
  const status = $('listStatus');
  if (!S.view.length) {
    status.hidden = false;
    status.textContent = S.refreshing === show.feedUrl ? 'Loading episodes…' : 'This feed has no playable episodes.';
    return;
  }
  status.hidden = true;
  appendRows(0, Math.min(S.view.length, S.shown));
}
function appendRows(from, to) {
  const ol = $('episodeList');
  const frag = document.createDocumentFragment();
  for (let i = from; i < to; i++) frag.append(episodeRow(S.view[i], i));
  const more = ol.querySelector('.more-row');
  if (more) more.remove();
  ol.append(frag);
  if (to < S.view.length) {
    const left = S.view.length - to;
    ol.append(el('li', { class: 'more-row' }, el('button', {
      class: 'pill', type: 'button', text: `Show ${Math.min(80, left)} more of ${left}`,
      onclick: () => { S.shown = to + 80; appendRows(to, Math.min(S.view.length, S.shown)); }
    })));
  }
  markPlayingRow();
}
function markPlayingRow() {
  const id = S.current && S.current.ep.id;
  const playing = !audio.paused;
  for (const li of $('episodeList').children) {
    if (!li.dataset.id) continue;
    const on = li.dataset.id === id;
    li.dataset.current = on ? 'true' : 'false';
    const b = li.querySelector('.ep-play');
    setIcon(b, on && playing ? 'pause' : 'play');
    b.setAttribute('aria-label', `${on && playing ? 'Pause' : 'Play'} ${li.querySelector('.ep-title').textContent}`);
  }
}
function updateRow(id) {
  for (const li of $('episodeList').children) {
    if (li.dataset.id !== id) continue;
    const pr = progress[id];
    li.dataset.done = pr && pr.done ? 'true' : 'false';
    li.querySelector('.ep-state').textContent = stateText(pr);
    setBar(li.querySelector('.ep-bar'), pr);
  }
}

/* ---------- search ---------- */
const looksLikeLink = v => /^(https?|feed|pcast|podcast|itpc):\/\//i.test(v) || /^www\./i.test(v) || /podcasts\.apple\.com/i.test(v) || /\.(xml|rss)(\?|$)/i.test(v);
async function searchPodcasts(term) {
  const kana = /[\u3040-\u30ff]/.test(term), han = /[\u3400-\u9fff]/.test(term);
  const countries = kana ? ['jp', 'us'] : han ? ['tw', 'jp', 'us'] : ['gb', 'us'];
  const settled = await Promise.allSettled(countries.map(c =>
    fetchText(`https://itunes.apple.com/search?media=podcast&entity=podcast&limit=15&country=${c}&term=${encodeURIComponent(term)}`)
      .then(r => JSON.parse(r.text).results || [])));
  if (settled.every(r => r.status === 'rejected')) throw settled[0].reason;
  const out = new Map();
  for (const r of settled) if (r.status === 'fulfilled') for (const it of r.value) if (it.feedUrl && !out.has(it.collectionId)) out.set(it.collectionId, it);
  return [...out.values()];
}
function showResults(title, items) {
  $('results').hidden = false;
  $('resultsTitle').textContent = title;
  const ul = $('resultsList');
  ul.replaceChildren();
  for (const it of items) {
    const inLib = shows.some(s => s.feedUrl === it.feedUrl);
    ul.append(el('li', { class: 'result' },
      cover(it.artworkUrl600 || it.artworkUrl100, it.collectionName),
      el('span', { class: 'show-text' }, el('span', { class: 'show-title', text: it.collectionName }), el('span', { class: 'show-author', text: it.artistName || '' })),
      el('button', {
        class: 'pill small', type: 'button', disabled: inLib, text: inLib ? 'Added' : 'Add',
        onclick: async e => {
          const btn = e.currentTarget;
          btn.disabled = true; btn.textContent = 'Adding…';
          const ok = await subscribe(it.feedUrl, { image: it.artworkUrl600 });
          btn.textContent = ok ? 'Added' : 'Add';
          btn.disabled = ok;
        }
      })));
  }
}
async function runSearch(term) {
  showResults(`Searching for “${term}”…`, []);
  busy(true);
  try {
    const items = await searchPodcasts(term);
    showResults(items.length ? `Results for “${term}”` : `No shows found for “${term}”. Try fewer words, or paste the feed link.`, items);
  } catch (e) {
    showResults(`Search didn’t work. ${e.message}`, []);
  } finally { busy(false); }
}
async function handleAdd(value) {
  const v = value.trim();
  if (!v) return;
  const apple = v.match(/podcasts\.apple\.com\/.*?\bid(\d+)/i);
  if (apple) {
    busy(true);
    try {
      const r = await fetchText(`https://itunes.apple.com/lookup?id=${apple[1]}&entity=podcast`);
      const it = (JSON.parse(r.text).results || []).find(x => x.feedUrl);
      if (!it) throw new Error('Apple doesn’t list a public feed for that show.');
      if (await subscribe(it.feedUrl, { image: it.artworkUrl600 })) { $('addInput').value = ''; addLabel(); }
    } catch (e) { toast(`Couldn’t add that show. ${e.message}`, true); }
    finally { busy(false); }
    return;
  }
  if (looksLikeLink(v)) {
    let url = v.replace(/^(feed|pcast|podcast|itpc):\/\//i, 'https://');
    if (!/^https?:\/\//i.test(url)) url = 'https://' + url;
    if (await subscribe(url)) { $('addInput').value = ''; addLabel(); }
    return;
  }
  runSearch(v);
}

/* ---------- OPML ---------- */
function exportOPML() {
  if (!shows.length) { toast('Your library is empty, so there’s nothing to export yet.'); return; }
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
  const body = shows.map(s => `    <outline type="rss" text="${esc(s.title)}" title="${esc(s.title)}" xmlUrl="${esc(s.feedUrl)}"/>`).join('\n');
  const xml = `<?xml version="1.0" encoding="UTF-8"?>\n<opml version="2.0">\n  <head><title>夜更かしラジオ subscriptions</title></head>\n  <body>\n${body}\n  </body>\n</opml>\n`;
  const a = el('a', { href: URL.createObjectURL(new Blob([xml], { type: 'text/x-opml' })), download: 'afterhours-subscriptions.opml' });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1500);
}
async function importOPML(file) {
  const doc = parseXML(await file.text());
  if (!doc) { toast('That file isn’t valid OPML.', true); return; }
  const urls = [...new Set([...doc.getElementsByTagName('outline')].map(o => o.getAttribute('xmlUrl')).filter(Boolean))];
  const fresh = urls.filter(u => !shows.some(s => s.feedUrl === u));
  if (!fresh.length) { toast('No new shows in that file.'); return; }
  toast(`Importing ${fresh.length} shows…`);
  let ok = 0;
  for (const u of fresh) if (await subscribe(u, { quiet: true })) ok++;
  if (!S.showUrl && shows.length) selectShow(shows[0].feedUrl);
  toast(`Imported ${ok} of ${fresh.length} shows.`, ok < fresh.length);
}

/* ---------- playback ---------- */
const curDur = () => { const d = audio.duration; return isFinite(d) && d > 0 ? d : (S.current ? S.current.ep.duration || 0 : 0); };
function loadEpisode(ep, show, queue, autoplay) {
  saveProgress(true);
  S.current = { ep, show };
  S.queue = queue && queue.length ? queue : [ep];
  S.lastSecond = -1;
  const saved = progress[ep.id];
  S.pendingSeek = saved && !saved.done && saved.p > 10 ? Math.max(0, saved.p - 3) : null;
  audio.src = ep.url;
  audio.defaultPlaybackRate = settings.speed;
  audio.playbackRate = settings.speed;
  try { audio.preservesPitch = true; audio.mozPreservesPitch = true; audio.webkitPreservesPitch = true; } catch {}
  $('seek').style.setProperty('--buf', '0%');
  updateNowPlaying();
  updateTimes(true);
  store.set(KEY.last, Object.assign(store.get(KEY.last, {}), { playShow: show.feedUrl, epId: ep.id }));
  loadCaptions(ep);
  if (autoplay) audio.play().catch(err => { if (err && err.name === 'NotAllowedError') toast('Press play to start listening.'); });
  markPlayingRow();
}
function updateNowPlaying() {
  const c = S.current;
  const img = c ? (c.ep.image || c.show.image) : '';
  $('pShow').textContent = c ? c.show.title : 'Nothing playing';
  $('pTitle').textContent = c ? c.ep.title : 'Pick an episode to start listening.';
  $('miniTitle').textContent = c ? c.ep.title : 'Nothing playing';
  $('miniShow').textContent = c ? c.show.title : '';
  const pc = $('pCover');
  if (img) { pc.hidden = false; pc.src = img; } else { pc.hidden = true; pc.removeAttribute('src'); }
  $('miniCover').replaceChildren(cover(img, c ? c.show.title : ''));
  for (const id of ['seek', 'playBtn', 'backBtn', 'fwdBtn', 'prevBtn', 'nextBtn', 'miniPlay']) $(id).disabled = !c;
  document.title = c ? `${c.ep.title} | 夜更かしラジオ` : '夜更かしラジオ';
  if ('mediaSession' in navigator && c) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: c.ep.title, artist: c.show.author || c.show.title, album: c.show.title,
        artwork: img ? [{ src: img, sizes: '512x512' }] : []
      });
    } catch {}
  }
}
function setPlaying(playing) {
  for (const id of ['playBtn', 'miniPlay']) {
    setIcon($(id), playing ? 'pause' : 'play');
    $(id).setAttribute('aria-label', playing ? 'Pause' : 'Play');
  }
  $('playBtn').title = playing ? 'Pause (Space)' : 'Play (Space)';
  if ('mediaSession' in navigator) navigator.mediaSession.playbackState = playing ? 'playing' : 'paused';
  markPlayingRow();
}
function updateTimes(force) {
  const d = curDur();
  const t = S.pendingSeek != null ? S.pendingSeek : (audio.currentTime || 0);
  const pct = d ? Math.min(100, t / d * 100) : 0;
  if (!S.dragging) { $('seek').value = Math.round(pct * 10); $('seek').style.setProperty('--fill', pct + '%'); }
  $('miniBar').style.width = pct + '%';
  const sec = Math.floor(t);
  if (force || sec !== S.lastSecond) {
    S.lastSecond = sec;
    if (!S.dragging) {
      $('tCur').textContent = fmtTime(t);
      $('tLeft').textContent = '-' + fmtTime(Math.max(0, d - t));
    }
    $('seek').setAttribute('aria-valuetext', `${fmtTime(t)} of ${fmtTime(d)}`);
  }
}
function updateBuffered() {
  const d = curDur();
  if (!d || !audio.buffered.length) return;
  let end = 0;
  for (let i = 0; i < audio.buffered.length; i++) if (audio.buffered.start(i) <= audio.currentTime + 1) end = Math.max(end, audio.buffered.end(i));
  $('seek').style.setProperty('--buf', Math.min(100, end / d * 100) + '%');
}
function updatePosState() {
  if (!('mediaSession' in navigator) || !navigator.mediaSession.setPositionState) return;
  const d = audio.duration;
  if (!isFinite(d) || d <= 0) return;
  try { navigator.mediaSession.setPositionState({ duration: d, playbackRate: audio.playbackRate, position: Math.min(audio.currentTime, d) }); } catch {}
}
function loop() { updateTimes(); renderCaption(); S.raf = requestAnimationFrame(loop); }
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
  renderCaption(true);
}
function step(dir) {
  if (!S.current) return;
  if (dir < 0 && audio.currentTime > 5) { audio.currentTime = 0; return; }
  const i = S.queue.findIndex(e => e.id === S.current.ep.id);
  const target = S.queue[i + dir];
  if (target) loadEpisode(target, S.current.show, S.queue, true);
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
  if (force || Date.now() - lastSave > 5000) { lastSave = Date.now(); persistProgress(); updateRow(c.ep.id); }
}
function onEnded() {
  const c = S.current;
  if (!c) return;
  progress[c.ep.id] = { p: 0, d: Math.round(curDur()), done: true, t: Date.now() };
  persistProgress();
  updateRow(c.ep.id);
  if (S.sleep.mode === 'end') { setSleep('off', true); toast('Sleep timer stopped at the end of the episode. Good night.'); return; }
  if (settings.autoNext) {
    const i = S.queue.findIndex(e => e.id === c.ep.id);
    const n = S.queue[i + 1];
    if (n) loadEpisode(n, c.show, S.queue, true);
  }
}

/* ---------- speed, captions, sleep, volume ---------- */
const PRESETS = [0.8, 1, 1.2, 1.5, 1.75, 2, 2.5, 3];
function setSpeed(v, announce) {
  v = Math.round(Math.min(3, Math.max(0.5, v)) * 100) / 100;
  settings.speed = v;
  saveSettings();
  audio.defaultPlaybackRate = v;
  audio.playbackRate = v;
  $('speedBtn').textContent = fmtSpeed(v);
  $('speedVal').textContent = v.toFixed(2) + '×';
  $('speedRange').value = v;
  $('speedRange').style.setProperty('--fill', ((v - 0.5) / 2.5 * 100) + '%');
  for (const b of $('speedPresets').children) b.setAttribute('aria-pressed', String(Math.abs(+b.dataset.v - v) < 0.001));
  if (announce) toast(`Speed ${fmtSpeed(v)}`);
}
function openSpeed(open) {
  $('speedPop').hidden = !open;
  $('speedBtn').setAttribute('aria-expanded', String(open));
  if (open) $('speedRange').focus();
}
function toggleCaptions() {
  settings.captions = !settings.captions;
  saveSettings();
  $('ccBtn').setAttribute('aria-pressed', String(settings.captions));
  S.cueIdx = -2;
  renderCaption(true);
  toast(settings.captions ? 'Captions on.' : 'Captions off.');
}
function toggleTranscript(force) {
  const panel = $('transcript');
  const open = force != null ? force : panel.hidden;
  panel.hidden = !open;
  $('player').classList.toggle('tr-open', open);
  $('trBtn').setAttribute('aria-pressed', String(open));
  if (open) { S.lastUserScroll = 0; if (S.cueIdx >= 0) highlightCue(S.cueIdx); }
}
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
  if (left < 10000 && !audio.paused) {
    if (S.sleep.fadeBase == null) S.sleep.fadeBase = audio.volume;
    audio.volume = S.sleep.fadeBase * (left / 10000);
  }
  updateSleepLabel();
}
function updateSleepLabel() {
  const m = S.sleep.mode;
  const mins = typeof m === 'number' ? Math.max(1, Math.ceil((S.sleep.endsAt - Date.now()) / 60000)) : 0;
  $('sleepLabel').textContent = mins ? `${mins} min` : m === 'end' ? 'Ep. end' : 'Sleep';
  $('sleepBtn').setAttribute('aria-pressed', String(m !== 'off'));
  $('sleepBtn').setAttribute('aria-label', mins ? `Sleep timer, ${mins} minutes left` : m === 'end' ? 'Sleep timer, stops at the end of this episode' : 'Sleep timer, off');
}
function syncVolumeUI() {
  const v = audio.muted ? 0 : audio.volume;
  if (S.sleep.fadeBase == null) {
    settings.volume = audio.volume;
    settings.muted = audio.muted;
    saveSettings();
    $('volume').value = v;
    $('volume').style.setProperty('--fill', v * 100 + '%');
  }
  setIcon($('muteBtn'), audio.muted || audio.volume === 0 ? 'mute' : 'vol');
  $('muteBtn').setAttribute('aria-label', audio.muted ? 'Unmute' : 'Mute');
}

/* ---------- theme ---------- */
const THEMES = ['system', 'light', 'dark'];
const THEME_LABEL = { system: 'Theme: follow system', light: 'Theme: light', dark: 'Theme: dark' };
function applyTheme() {
  if (settings.theme === 'system') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = settings.theme;
  $('themeBtn').setAttribute('aria-label', THEME_LABEL[settings.theme]);
  $('themeBtn').title = THEME_LABEL[settings.theme];
}

/* ---------- mobile sheet ---------- */
function setExpanded(on) {
  $('player').classList.toggle('expanded', on);
  document.body.classList.toggle('sheet-open', on);
  $('miniExpand').setAttribute('aria-expanded', String(on));
  if (on) $('collapseBtn').focus();
}

/* ---------- wiring ---------- */
function wire() {
  $('addInput').addEventListener('input', addLabel);
  $('addForm').addEventListener('submit', e => { e.preventDefault(); handleAdd($('addInput').value); });
  $('resultsClose').addEventListener('click', () => { $('results').hidden = true; });
  $('importBtn').addEventListener('click', () => $('opmlFile').click());
  $('exportBtn').addEventListener('click', exportOPML);
  $('opmlFile').addEventListener('change', e => { const f = e.target.files[0]; e.target.value = ''; if (f) importOPML(f); });
  $('themeBtn').addEventListener('click', () => {
    settings.theme = THEMES[(THEMES.indexOf(settings.theme) + 1) % THEMES.length];
    saveSettings(); applyTheme(); toast(THEME_LABEL[settings.theme] + '.');
  });

  $('refreshBtn').addEventListener('click', () => { if (S.showUrl) refreshShow(S.showUrl, false); });
  $('sortBtn').addEventListener('click', () => { settings.sort = settings.sort === 'oldest' ? 'newest' : 'oldest'; saveSettings(); renderShowHead(); renderEpisodes(); });
  $('removeBtn').addEventListener('click', () => {
    const s = currentShow();
    if (!s || !confirm(`Remove “${s.title}” from your library?`)) return;
    shows = shows.filter(x => x.feedUrl !== s.feedUrl);
    saveShows();
    store.del(KEY.cache(s.feedUrl));
    S.showUrl = null;
    if (shows.length) selectShow(shows[0].feedUrl);
    else { S.episodes = []; renderShows(); renderShowHead(); }
    toast(`Removed ${s.title}.`);
  });

  $('episodeList').addEventListener('click', e => {
    const li = e.target.closest('.ep');
    if (!li) return;
    const more = e.target.closest('.ep-more');
    if (more) {
      const open = li.classList.toggle('open');
      more.textContent = open ? 'Less' : 'More';
      more.setAttribute('aria-expanded', String(open));
      return;
    }
    if (!e.target.closest('.ep-play') && String(window.getSelection())) return;
    const ep = S.view[+li.dataset.i];
    if (!ep) return;
    if (S.current && S.current.ep.id === ep.id) { togglePlay(); return; }
    loadEpisode(ep, currentShow(), S.view, true);
  });

  audio.addEventListener('loadedmetadata', () => {
    if (S.pendingSeek != null) {
      const target = Math.min(S.pendingSeek, Math.max(0, audio.duration - 5));
      S.pendingSeek = null;
      try { audio.currentTime = target; } catch {}
    }
    audio.playbackRate = settings.speed;
    updateTimes(true);
    updatePosState();
  });
  audio.addEventListener('play', () => { setPlaying(true); cancelAnimationFrame(S.raf); S.raf = requestAnimationFrame(loop); });
  audio.addEventListener('pause', () => { setPlaying(false); cancelAnimationFrame(S.raf); saveProgress(true); updateTimes(true); updatePosState(); });
  audio.addEventListener('ended', onEnded);
  audio.addEventListener('timeupdate', () => { if (audio.paused) { updateTimes(); renderCaption(); } saveProgress(false); });
  audio.addEventListener('seeked', () => { updateTimes(true); renderCaption(); updatePosState(); });
  audio.addEventListener('progress', updateBuffered);
  audio.addEventListener('ratechange', updatePosState);
  audio.addEventListener('waiting', () => $('player').classList.add('buffering'));
  audio.addEventListener('playing', () => $('player').classList.remove('buffering'));
  audio.addEventListener('volumechange', syncVolumeUI);
  audio.addEventListener('error', () => {
    if (!S.current || !audio.getAttribute('src')) return;
    $('player').classList.remove('buffering');
    setPlaying(false);
    toast('This episode’s audio couldn’t be played. The link may have changed, so try refreshing the show.', true);
  });

  const seek = $('seek');
  seek.addEventListener('input', () => {
    S.dragging = true;
    const d = curDur(), t = seek.value / 1000 * d;
    $('tCur').textContent = fmtTime(t);
    $('tLeft').textContent = '-' + fmtTime(Math.max(0, d - t));
    seek.style.setProperty('--fill', seek.value / 10 + '%');
  });
  seek.addEventListener('change', () => {
    const d = curDur();
    const t = seek.value / 1000 * d;
    S.dragging = false;
    if (!d) return;
    if (S.pendingSeek != null) S.pendingSeek = t; else audio.currentTime = t;
    updateTimes(true);
    renderCaption(true);
  });

  $('playBtn').addEventListener('click', togglePlay);
  $('miniPlay').addEventListener('click', togglePlay);
  $('backBtn').addEventListener('click', () => skip(-15));
  $('fwdBtn').addEventListener('click', () => skip(30));
  $('prevBtn').addEventListener('click', () => step(-1));
  $('nextBtn').addEventListener('click', () => step(1));
  $('miniExpand').addEventListener('click', () => setExpanded(true));
  $('collapseBtn').addEventListener('click', () => setExpanded(false));

  for (const v of PRESETS) $('speedPresets').append(el('button', { class: 'preset', type: 'button', dataset: { v }, text: fmtSpeed(v), onclick: () => setSpeed(v) }));
  $('speedBtn').addEventListener('click', () => openSpeed($('speedPop').hidden));
  $('speedRange').addEventListener('input', e => setSpeed(+e.target.value));
  $('speedReset').addEventListener('click', () => setSpeed(1));
  document.addEventListener('pointerdown', e => { if (!$('speedPop').hidden && !e.target.closest('.speed-wrap')) openSpeed(false); });

  $('ccBtn').addEventListener('click', toggleCaptions);
  $('trBtn').addEventListener('click', () => toggleTranscript());
  $('trLoad').addEventListener('click', () => $('capFile').click());
  $('sleepBtn').addEventListener('click', () => setSleep(SLEEP_STEPS[(SLEEP_STEPS.indexOf(S.sleep.mode) + 1) % SLEEP_STEPS.length]));
  $('volume').addEventListener('input', e => { S.sleep.fadeBase = null; audio.volume = +e.target.value; audio.muted = audio.volume === 0; });
  $('muteBtn').addEventListener('click', () => { audio.muted = !audio.muted; if (!audio.muted && audio.volume === 0) audio.volume = 0.6; });
  $('autoNext').addEventListener('change', e => { settings.autoNext = e.target.checked; saveSettings(); });

  const tr = $('trList');
  for (const ev of ['wheel', 'touchmove', 'keydown']) tr.addEventListener(ev, () => { S.lastUserScroll = Date.now(); }, { passive: true });
  tr.addEventListener('click', e => {
    const li = e.target.closest('.cue');
    if (!li || li.dataset.i == null || !S.current) return;
    const c = S.cues[+li.dataset.i];
    if (!c) return;
    S.lastUserScroll = 0;
    if (S.pendingSeek != null) S.pendingSeek = c.start; else audio.currentTime = c.start;
    if (audio.paused) audio.play().catch(() => {});
    renderCaption(true);
  });

  $('capFile').addEventListener('change', async e => {
    const f = e.target.files[0];
    e.target.value = '';
    if (!f) return;
    if (!S.current) { toast('Start an episode first, then load its caption file.'); return; }
    const text = await f.text();
    const name = f.name.toLowerCase();
    const type = name.endsWith('.srt') ? 'srt' : name.endsWith('.vtt') ? 'vtt' : name.endsWith('.json') ? 'json' : '';
    applyTranscript(text, type);
    if (!S.cues.length) { toast('That file has no timed captions I can read.', true); loadCaptions(S.current.ep); return; }
    if (text.length < 600000) store.set(KEY.cap(S.current.ep.id), { name: f.name, type, text });
    S.capLoading = false; S.capError = '';
    renderTranscript();
    renderCaption(true);
    toast(`Loaded captions from ${f.name}.`);
  });

  document.addEventListener('keydown', e => {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey) return;
    const t = e.target;
    if (t.closest && t.closest('input:not([type=range]):not([type=checkbox]), textarea, select, [contenteditable="true"]')) {
      if (e.key === 'Escape') t.blur();
      return;
    }
    const onRange = t.type === 'range';
    switch (e.key) {
      case 'Escape': openSpeed(false); if (isNarrow()) setExpanded(false); break;
      case ' ': case 'k': case 'K':
        if (e.key === ' ' && t.closest && t.closest('button, a, input')) return;
        e.preventDefault(); togglePlay(); break;
      case 'ArrowLeft': case 'j': case 'J': if (onRange) return; e.preventDefault(); skip(-15); break;
      case 'ArrowRight': case 'l': case 'L': if (onRange) return; e.preventDefault(); skip(30); break;
      case '[': setSpeed(settings.speed - 0.1, true); break;
      case ']': setSpeed(settings.speed + 0.1, true); break;
      case 'c': case 'C': toggleCaptions(); break;
      case 't': case 'T': toggleTranscript(); break;
      case 'm': case 'M': audio.muted = !audio.muted; break;
      case 'N': if (e.shiftKey) step(1); break;
      case 'P': if (e.shiftKey) step(-1); break;
    }
  });

  if ('mediaSession' in navigator) {
    const set = (action, fn) => { try { navigator.mediaSession.setActionHandler(action, fn); } catch {} };
    set('play', () => audio.play());
    set('pause', () => audio.pause());
    set('seekbackward', d => skip(-((d && d.seekOffset) || 15)));
    set('seekforward', d => skip((d && d.seekOffset) || 30));
    set('seekto', d => { if (d.fastSeek && 'fastSeek' in audio) audio.fastSeek(d.seekTime); else audio.currentTime = d.seekTime; updateTimes(true); });
    set('previoustrack', () => step(-1));
    set('nexttrack', () => step(1));
    set('stop', () => audio.pause());
  }

  window.addEventListener('pagehide', () => saveProgress(true));
  document.addEventListener('visibilitychange', () => { if (document.hidden) saveProgress(true); });
  $('pCover').addEventListener('error', () => { $('pCover').hidden = true; });
}

const TRY = [
  ['Primer', 'Primer Maximum Fun'], ['Composer of the Week', 'Composer of the Week'], ['In Our Time', 'In Our Time BBC'],
  ['Shedunnit', 'Shedunnit'], ['Mindscape', 'Sean Carroll Mindscape'], ['Jazz Night in America', 'Jazz Night in America'],
  ['Rebuild', 'Rebuild.fm'], ['ゆる言語学ラジオ', 'ゆる言語学ラジオ']
];

function init() {
  applyTheme();
  wire();
  audio.volume = settings.volume;
  audio.muted = settings.muted;
  syncVolumeUI();
  setSpeed(settings.speed);
  $('ccBtn').setAttribute('aria-pressed', String(settings.captions));
  $('autoNext').checked = settings.autoNext;
  for (const [label, term] of TRY) $('tryChips').append(el('button', { type: 'button', text: label, onclick: () => { $('addInput').value = term; runSearch(term); } }));

  renderShows();
  const last = store.get(KEY.last, {});
  const start = shows.some(s => s.feedUrl === last.showUrl) ? last.showUrl : (shows[0] && shows[0].feedUrl);
  if (start) selectShow(start); else renderShowHead();

  if (last.epId && last.playShow) {
    const show = shows.find(s => s.feedUrl === last.playShow);
    const cached = store.get(KEY.cache(last.playShow), null);
    const ep = cached && cached.episodes.find(e => e.id === last.epId);
    if (show && ep) loadEpisode(ep, show, sortList(cached.episodes), false);
  }
  updateNowPlaying();
  setPlaying(false);
  renderTranscript();
  renderCaption(true);
}
init();
})();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    main()
