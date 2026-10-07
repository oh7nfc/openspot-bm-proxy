#!/usr/bin/env python3
"""Web proxy that gives an old SharkRF openSPOT's status page working BrandMeister rows and buttons.

The openSPOT web UI (main.min.js) calls BrandMeister's retired v1 API from the *browser*:
    https://api.brandmeister.network/v1.0/repeater/?action=PROFILE&q=<DMR ID>   (and write calls)
This proxy serves the UI unchanged from the device except for two URLs, which are rewritten to
/bmapi/ on the proxy. /bmapi/ answers in the v1 shape the page expects, using BrandMeister API v2.
Browse to http://<proxy>:<port>/ instead of the device. Stdlib only.

API keys: the openSPOT firmware only stores short keys, but v2 keys are long JWTs. In the openSPOT's
"API key" field enter a short *key name* (letters, digits, - and _). The proxy reads the real token
from <keys-dir>/<name> and sends it to BrandMeister; the token never reaches the browser or the device.
Reading the profile needs no key. The reflector change has no v2 equivalent and reports an error.
"""
import argparse
import base64
import gzip
import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REFRESH_SEC = 60  # how often the Status page reloads the BrandMeister rows
JS_REWRITES = [
    (b'srf_bmapiurl="https://api.brandmeister.network/v1.0/repeater/"', b'srf_bmapiurl="/bmapi/"'),
    (b'"https://api.brandmeister.network/v1.0/auth/validate-token.php?token="', b'"/bmapi/validate?token="'),
    # The old "Last heard" page (?page=lh&Master=<id>) now answers 403. The new site has no per-master view,
    # but #/lh?ContextID=<device id> shows one hotspot's calls, and the page already knows its DMR ID.
    (b'"https://brandmeister.network/?page=lh&Master="+a.split("/")[1].split(" ")[0]',
     b'"https://brandmeister.network/#/lh?ContextID="+srf_bm_dmrid'),
    # The page loads the BrandMeister rows once, when the connection changes or after its own writes, so TGs
    # changed elsewhere (or a profile fetch that came back empty) stay stale. Reload them periodically while
    # the Status page is showing.
    (b"function srf_handle_status_connected_to_change(){",
     b'setInterval(function(){if(!document.hidden&&$("#page-status").hasClass("active")'
     b'&&!$(".page-status-bm-rows").hasClass("hidden")){srf_handle_status_connected_to_change()}},'
     + str(REFRESH_SEC * 1000).encode() + b");function srf_handle_status_connected_to_change(){"),
]
# The "Get key" button opens brandmeister.network/?page=profile-api (now 403). Send it to the proxy's own
# help page, which explains the key name and links to BrandMeister's new API Keys page.
HTML_REWRITES = [
    (b"window.open('https://brandmeister.network/?page=profile-api', '_blank')",
     b"window.open('/help/api-key', '_blank')"),
]
REWRITES = {"main.min.js_gz": JS_REWRITES, "main.html_gz": HTML_REWRITES}
HELP_FRAME = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;color:#222}
code{background:#eee;padding:.1em .35em;border-radius:3px}pre{background:#eee;padding:.7em;overflow-x:auto}
@media(prefers-color-scheme:dark){body{background:#16181c;color:#e4e6ea}code,pre{background:#2a2e35}a{color:#7ab7ff}}</style>
</head><body>
<p style="text-align:right"><a href="?lang={other}">{switch}</a></p>
{body}
</body></html>"""
# (title, switch-link text, body) per language. {keys_dir} is filled in when the page is served.
HELP_TEXT = {
    "en": ("BrandMeister API key", "Suomeksi", """<h1>BrandMeister API key</h1>
<p>This openSPOT page is served through a small proxy that talks to the current BrandMeister API (v2).
The old key page on brandmeister.network is gone, and the openSPOT can only store a short key,
so the real key stays on the proxy server and this field holds only a <b>key name</b>.</p>
<h2>1. Create a key</h2>
<p>Log in at <a href="https://brandmeister.network/#/profile/api" target="_blank" rel="noopener">
brandmeister.network &rarr; profile &rarr; API Keys</a> and create a key. It is a long token that
starts with <code>eyJ</code>. Anyone holding it can change your hotspots' talkgroups, so keep it private.</p>
<h2>2. Put it on the proxy server</h2>
<p>As root-only files in <code>{keys_dir}</code>, one file per key, the file name being the key name
(letters, digits, <code>-</code> and <code>_</code>, up to 32 characters). Type the token there yourself:</p>
<pre>sudo -u openspotproxy tee {keys_dir}/mykey &gt;/dev/null
sudo chmod 600 {keys_dir}/mykey</pre>
<p>Paste the token, press Enter, then Ctrl-D.</p>
<h2>3. Enter the key name here</h2>
<p>Type <code>mykey</code> (the file name, not the token) in the <b>API key</b> field, press Save, then
Check key. Pick a name that is hard to guess: anyone on your network who knows it can use the key
through this proxy.</p>
<h2>What works</h2>
<p>Add and remove static talkgroups, drop dynamic talkgroups, drop the current call. Changing the
reflector is not possible, because BrandMeister API v2 has no such call.</p>"""),
    "fi": ("BrandMeisterin API-avain", "English", """<h1>BrandMeisterin API-avain</h1>
<p>Tämä openSPOT-sivu tulee pienen välityspalvelimen (proxyn) kautta, joka keskustelee BrandMeisterin
nykyisen API:n (v2) kanssa. BrandMeisterin vanha avainsivu on poistunut, eikä openSPOT pysty tallentamaan
kuin lyhyen avaimen. Siksi oikea avain pysyy proxy-palvelimella, ja tähän kenttään kirjoitetaan vain
<b>avaimen nimi</b>.</p>
<h2>1. Luo avain</h2>
<p>Kirjaudu osoitteessa <a href="https://brandmeister.network/#/profile/api" target="_blank" rel="noopener">
brandmeister.network &rarr; profiili &rarr; API Keys</a> ja luo avain. Se on pitkä merkkijono, joka alkaa
merkeillä <code>eyJ</code>. Avaimen haltija voi muuttaa hotspottiesi puheryhmiä, joten pidä avain
salassa.</p>
<h2>2. Tallenna avain proxy-palvelimelle</h2>
<p>Tiedostona hakemistoon <code>{keys_dir}</code>, jota vain root ja palvelun käyttäjä voivat lukea.
Yksi tiedosto per avain, ja tiedoston nimi on avaimen nimi (kirjaimia, numeroita sekä <code>-</code> ja
<code>_</code>, enintään 32 merkkiä). Kirjoita avain itse:</p>
<pre>sudo -u openspotproxy tee {keys_dir}/mykey &gt;/dev/null
sudo chmod 600 {keys_dir}/mykey</pre>
<p>Liitä avain, paina Enter ja sitten Ctrl-D.</p>
<h2>3. Kirjoita avaimen nimi tähän</h2>
<p>Kirjoita <code>mykey</code> (tiedoston nimi, ei itse avain) <b>API key</b> -kenttään, paina Save ja
sitten Check key. Valitse nimi, jota ei voi arvata: kuka tahansa verkossasi, joka tietää nimen, voi
käyttää avainta tämän proxyn kautta.</p>
<h2>Mikä toimii</h2>
<p>Staattisten puheryhmien lisäys ja poisto, dynaamisten puheryhmien pudotus ja käynnissä olevan
puhelun pudotus. Reflektorin vaihtaminen ei onnistu, koska BrandMeisterin API v2:ssa ei ole sellaista
kutsua.</p>"""),
}
BM_V2 = "https://api.brandmeister.network/v2/device/"
HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "proxy-connection", "upgrade"}
CACHE_SEC = 30
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
JSON = "application/json"


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def tg_list(items):
    out, seen = [], set()
    for it in items or []:
        tg = it.get("talkgroup") if isinstance(it, dict) else it
        try:
            tg = int(tg)  # v2 sends "2400", v1 sent 2400
        except (TypeError, ValueError):
            continue
        if tg not in seen:
            seen.add(tg)
            out.append({"talkgroup": tg})
    return out


def v1_profile(v2):
    # v2 has no reflector field; "-" is shown when active is null.
    return {"staticSubscriptions": tg_list(v2.get("staticSubscriptions")),
            "dynamicSubscriptions": tg_list(v2.get("dynamicSubscriptions")),
            "reflector": {"active": None}}


def jwt_expired(token):
    """True if the token is not a JWT or its exp claim is in the past. No signature check."""
    try:
        part = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return "exp" in claims and claims["exp"] < time.time()
    except (IndexError, ValueError):
        return True


class Proxy:
    def __init__(self, a):
        self.device = a.device
        self.keys_dir = a.keys_dir
        self.cache = {}  # dmr id -> (expiry, v1 profile)
        self.lock = threading.Lock()
        self.dev_slots = threading.Semaphore(3)  # the device's HTTP server is small

    def token(self, name):
        if not name or not NAME_RE.match(name):
            return None
        try:
            with open(os.path.join(self.keys_dir, name)) as f:
                return f.read().strip() or None
        except OSError:
            return None

    def bm(self, method, path, token=None, body=None):
        """One BrandMeister v2 call. Returns (status, bytes). Never logs the token."""
        headers = {"User-Agent": "dmrlink-openspot-proxy", "Accept": JSON}
        if token:
            headers["Authorization"] = "Bearer " + token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = JSON
        try:
            r = urllib.request.urlopen(urllib.request.Request(BM_V2 + path, data, headers, method=method), timeout=10)
            return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def profile(self, dmr_id):
        now = time.time()
        with self.lock:
            hit = self.cache.get(dmr_id)
            if hit and hit[0] > now:
                return hit[1]
        status, raw = self.bm("GET", dmr_id + "/profile")
        if status == 404:
            log(f"bm: device {dmr_id} not known to BrandMeister, returning empty profile")
            return v1_profile({})  # not cached: the device may show up at BrandMeister any moment
        elif status == 200:
            v1 = v1_profile(json.loads(raw))
            log(f"bm: profile {dmr_id} static={[t['talkgroup'] for t in v1['staticSubscriptions']]} "
                f"dynamic={[t['talkgroup'] for t in v1['dynamicSubscriptions']]}")
        else:
            raise RuntimeError(f"BrandMeister returned {status}")
        with self.lock:
            self.cache[dmr_id] = (now + CACHE_SEC, v1)
        return v1

    def forget(self, dmr_id):
        with self.lock:
            self.cache.pop(dmr_id, None)


def make_handler(px):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def reply(self, code, body=b"", headers=(), ctype=None):
            self.send_response(code)
            if ctype:
                self.send_header("Content-Type", ctype)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def jreply(self, code, obj):
            self.reply(code, json.dumps(obj).encode(), headers=[("Cache-Control", "no-store")], ctype=JSON)

        def key_name(self):
            """The 'API key' the page sends is a key name: Basic base64('<name>:')."""
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return None
            try:
                return base64.b64decode(auth[6:]).decode().split(":")[0]
            except (ValueError, UnicodeDecodeError):
                return None

        def write(self, label, dmr_id, method, path, body=None):
            """Run one authenticated v2 write and answer in the v1 style the page expects."""
            token = px.token(self.key_name())
            if not token:
                log(f"bm: {label} {dmr_id} refused: unknown key name")
                return self.jreply(401, {"code": "ERROR", "message": "unknown key name"})
            status, raw = px.bm(method, path, token, body)
            px.forget(dmr_id)
            log(f"bm: {label} on {dmr_id} -> {status}")
            if 200 <= status < 300:
                return self.jreply(200, {"code": "OK", "message": f"OK ({label})"})  # the page shows message
            log(f"bm: error body: {raw[:300].decode('utf8', 'replace')!r}")
            self.reply(status, raw or b"{}", ctype=JSON)

        def do_bmapi(self):
            u = urlsplit(self.path)
            sub = u.path[len("/bmapi/"):]
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            form = {}
            if self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
            action = q.get("action", "")

            if sub == "validate" and self.command == "GET":
                tok = px.token(q.get("token"))
                if not tok or jwt_expired(tok):
                    return self.jreply(401, {"error": "no usable key under that name"})
                return self.jreply(200, {"ok": True})

            dmr_id = q.get("q") or q.get("id") or ""
            if not dmr_id.isdigit():
                return self.jreply(400, {"error": "missing or bad DMR ID"})
            slot = form.get("timeslot") or q.get("slot") or "0"
            tg = form.get("talkgroup", "")

            if sub in ("", "index.php") and self.command == "GET" and action.upper() == "PROFILE":
                try:
                    return self.jreply(200, px.profile(dmr_id))
                except Exception as e:  # network or JSON failure: the page retries on error
                    log(f"bm: v2 lookup failed: {e}")
                    return self.jreply(502, {"error": "upstream"})
            if self.command == "POST" and sub == "talkgroup/" and slot.isdigit() and tg.isdigit():
                if action == "ADD":
                    return self.write(f"add TG {tg} slot {slot}", dmr_id, "POST", f"{dmr_id}/talkgroup",
                                      # the live API wants "group"; its published spec says "talkgroup"
                                      {"group": int(tg), "talkgroup": int(tg), "slot": int(slot)})
                if action == "DEL":
                    return self.write(f"remove TG {tg} slot {slot}", dmr_id, "DELETE",
                                      f"{dmr_id}/talkgroup/{slot}/{tg}")
            if self.command == "POST" and slot.isdigit():
                if sub == "setRepeaterTarantool.php" and action == "dropDynamicGroups":
                    return self.write(f"drop dynamic TGs slot {slot}", dmr_id, "GET",
                                      f"{dmr_id}/action/dropDynamicGroups/{slot}")
                if sub == "setRepeaterDbus.php" and action == "dropCallRoute":
                    return self.write(f"drop call slot {slot}", dmr_id, "GET",
                                      f"{dmr_id}/action/dropCallRoute/{slot}")
            if sub == "reflector/setActiveReflector.php":
                return self.jreply(501, {"code": "ERROR", "message": "BrandMeister API v2 has no reflector call"})
            self.jreply(400, {"error": "unsupported"})

        def help_lang(self):
            """?lang=fi|en wins, else the first language in Accept-Language that we have, else English."""
            q = parse_qs(urlsplit(self.path).query).get("lang", [""])[0]
            if q in HELP_TEXT:
                return q
            for part in self.headers.get("Accept-Language", "").split(","):
                tag = part.split(";")[0].strip().lower()[:2]
                if tag in HELP_TEXT:
                    return tag
            return "en"

        def do_proxy(self):
            path = urlsplit(self.path).path
            if path == "/":
                # The device's loader page redirects the browser straight to the device; skip it.
                return self.reply(302, headers=[("Location", "/main.html_gz")])
            if path.startswith("/bmapi/"):
                return self.do_bmapi()
            if path == "/help/api-key":
                lang = self.help_lang()
                title, switch, body = HELP_TEXT[lang]
                page = HELP_FRAME  # plain replaces: the CSS in the frame contains braces
                for key, val in (("{lang}", lang), ("{title}", title), ("{switch}", switch),
                                 ("{other}", "fi" if lang == "en" else "en"), ("{body}", body)):
                    page = page.replace(key, val)
                page = page.replace("{keys_dir}", px.keys_dir)
                return self.reply(200, page.encode(), headers=[("Vary", "Accept-Language")],
                                  ctype="text/html; charset=utf-8")
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            rewrites = next((rw for name, rw in REWRITES.items() if path.endswith(name)), None)
            skip = HOP | {"if-none-match", "if-modified-since"} if rewrites else HOP
            # A browser that cached the file before it was rewritten would get a 304 from the device and
            # keep the old copy, so rewritten files are always fetched in full.
            fwd = {k: v for k, v in self.headers.items() if k.lower() not in skip}
            me = self.headers.get("Host", "")
            fwd["Host"] = px.device
            for h in ("Origin", "Referer"):
                if h in fwd:
                    fwd[h] = fwd[h].replace(me, px.device)
            with px.dev_slots:
                try:
                    c = http.client.HTTPConnection(px.device, 80, timeout=15)
                    c.request(self.command, self.path, body, fwd)
                    r = c.getresponse()
                    data = r.read()
                    heads = r.getheaders()
                    code = r.status
                    c.close()
                except (OSError, http.client.HTTPException) as e:
                    log(f"device: {self.command} {self.path} failed: {e}")
                    return self.reply(502, b"openSPOT unreachable")
            if rewrites and code == 200:
                enc = r.getheader("Content-Encoding", "")
                text = gzip.decompress(data) if enc == "gzip" else data
                for old, new in rewrites:
                    if old in text:
                        text = text.replace(old, new)
                    else:
                        log(f"WARNING: {old[:48]!r} not found in {path} (firmware changed?)")
                data = text
                heads = [(k, v) for k, v in heads if k.lower() not in ("content-encoding", "etag")]
                heads.append(("Cache-Control", "no-cache"))  # revalidate; no ETag, so always a full reply
            out = [(k, v) for k, v in heads if k.lower() not in HOP]
            self.reply(code, data, out)

        do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = do_proxy

    return H


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--device", required=True, help="openSPOT address, e.g. 192.168.1.50")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--keys-dir", default="/etc/openspot-bm-proxy/keys",
                   help="directory of BrandMeister v2 tokens, one file per key name")
    a = p.parse_args()
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(Proxy(a)))
    srv.daemon_threads = True
    log(f"openSPOT proxy http://{a.bind}:{a.port}/ -> http://{a.device}/ (keys in {a.keys_dir})")
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
