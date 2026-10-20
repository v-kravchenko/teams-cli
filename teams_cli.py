#!/usr/bin/env python3
"""Light Microsoft Teams CLI: login, chats, read, send (send is a dry run unless --yes).

Talks to the same private chat service as the Teams web client / purple-teams.
Tokens go to the GNOME keyring (secret-tool); without one, to ~/.config/teams-cli/state.json (0600). Never printed.

  teams_cli.py login [--url URL] # opens the browser; paste the final URL back into the terminal
  teams_cli.py chats [--json]   # list recent conversations
  teams_cli.py read <n|text> [-n 20] [--json]
  teams_cli.py watch [chat ...] [--json]            # live messages via Trouter (needs websockets)
  teams_cli.py send <n|text> "message" [--yes]   # without --yes only prints what would be sent
"""
import argparse, asyncio, base64, gzip, html, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid, webbrowser

CLIENT_ID = "1fec8e78-bce4-4aaf-ab1b-5451cc387264"  # Teams desktop/web public client
REDIRECT = "https://login.microsoftonline.com/common/oauth2/nativeclient"
RESOURCE = "https://api.spaces.skype.com"
SCOPE = RESOURCE + "/.default openid profile offline_access"
TENANT = "organizations"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0 Teams/24165.1410.2974.6689/49")
DEFAULT_CHAT_HOST = "https://apac.ng.msg.teams.microsoft.com"
TARGETS = "Passport|Skype|Lync|Thread|PSTN|Agent"

STATE_DIR = os.path.expanduser("~/.config/teams-cli")
STATE = os.path.join(STATE_DIR, "state.json")
DEBUG = False


SECRET_KEYS = ("refresh_token", "access_token", "skype_token")
_cache = None
_warned = False


def keyring_get(name):
    try:
        r = subprocess.run(["secret-tool", "lookup", "service", "teams-cli", "key", name],
                           capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 and r.stdout else None
    except (OSError, subprocess.SubprocessError):
        return None


def keyring_put(name, value):
    """Store one token (secret-tool truncates values at 8 KB, so one item per token) and read it back."""
    try:
        r = subprocess.run(["secret-tool", "store", "--label=teams-cli " + name, "service", "teams-cli", "key", name],
                           input=value, capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and keyring_get(name) == value
    except (OSError, subprocess.SubprocessError):
        return False


def load():
    """State = non-secret file + tokens from the keyring (or from the 0600 file as a fallback)."""
    global _cache
    if _cache is None:
        try:
            st = json.load(open(STATE))
        except (OSError, ValueError):
            st = {}
        for k in SECRET_KEYS:
            v = keyring_get(k)
            if v:
                st[k] = v
        _cache = st
    return dict(_cache)


def save(st):
    global _cache, _warned
    _cache = dict(st)
    data = dict(st)
    for k in SECRET_KEYS:
        if st.get(k) and keyring_put(k, st[k]):
            del data[k]  # only dropped from the file after a verified read-back
        elif st.get(k) and not _warned:
            _warned = True
            print(f"warning: keyring unavailable, tokens kept in {STATE} (mode 0600)", file=sys.stderr)
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def http(method, url, headers=None, data=None, raw=False):
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            if raw:
                return r.status, body.decode()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        sys.exit(f"HTTP {e.code} {method} {urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path}: {body[:300]}")


def token_request(form):
    form = {"client_id": CLIENT_ID, "scope": SCOPE, **form}
    _, d = http("POST", f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                {"Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(form).encode())
    return d


def cmd_login(a):
    url = a.url
    if not url:
        auth = (f"https://login.microsoftonline.com/{TENANT}/oauth2/authorize?client_id={CLIENT_ID}"
                f"&response_type=code&display=popup&prompt=select_account&amr_values=mfa"
                f"&redirect_uri={urllib.parse.quote(REDIRECT, safe='')}")
        opened = False
        try:
            opened = webbrowser.open(auth)
        except webbrowser.Error:
            pass
        if not opened:
            print("Open this URL in a browser:\n\n" + auth + "\n")
        print("1. Sign in to Microsoft in the browser" + (" window that just opened." if opened else "."))
        print("2. You land on a blank page. Copy its full URL from the address bar (Ctrl+L, Ctrl+C).")
        if not sys.stdin.isatty():
            print("3. No terminal input here; run:  teams_cli.py login --url '<copied url>'")
            return
        print("3. Paste it here and press Enter.\n")
        url = input("URL: ").strip()
    m = re.search(r"[?&]code=([^&\s]+)", url)
    if not m:
        sys.exit("no code= in the pasted URL; copy the whole address of the blank page")
    d = token_request({"grant_type": "authorization_code", "code": urllib.parse.unquote(m.group(1)),
                       "redirect_uri": REDIRECT})
    st = load()
    st.update({"refresh_token": d["refresh_token"], "access_token": d["access_token"],
               "access_exp": time.time() + int(d.get("expires_in", 3000))})
    st.pop("skype_token", None)
    save(st)
    _, host = session()
    print("Logged in. Chat service:", host)
    print("Try:  teams_cli.py chats")


def access():
    """OAuth access token for api.spaces.skype.com; refreshes (and rotates the refresh token) as needed."""
    st = load()
    if not st.get("refresh_token"):
        sys.exit("not logged in: run `login` first")
    if st.get("access_token") and st.get("access_exp", 0) > time.time() + 120:
        return st["access_token"]
    d = token_request({"grant_type": "refresh_token", "refresh_token": st["refresh_token"]})
    st["refresh_token"] = d.get("refresh_token", st["refresh_token"])
    st["access_token"] = d["access_token"]
    st["access_exp"] = time.time() + int(d.get("expires_in", 3000))
    save(st)
    return st["access_token"]


def session():
    """Return (skype_token, chat_host); refreshes and caches as needed."""
    st = load()
    if st.get("skype_token") and st.get("skype_exp", 0) > time.time() + 120:
        return st["skype_token"], st.get("chat_host", DEFAULT_CHAT_HOST)
    acc = access()
    st = load()
    _, a = http("POST", "https://teams.microsoft.com/api/authsvc/v1.0/authz",
                {"Authorization": "Bearer " + acc, "Accept": "application/json; ver=1.0",
                 "Content-Length": "0"})
    tokens = a.get("tokens") or a.get("skypeToken") or {}
    st["skype_token"] = tokens.get("skypeToken") or tokens.get("skypetoken")
    st["skype_exp"] = time.time() + int(tokens.get("expiresIn", 3000)) - 60
    gtms = a.get("regionGtms") or {}
    st["chat_host"] = (gtms.get("chatService") or DEFAULT_CHAT_HOST).rstrip("/")
    if DEBUG:
        print("authz keys:", sorted(a), "| chat_host:", st["chat_host"], file=sys.stderr)
    if not st["skype_token"]:
        sys.exit("authz returned no skype token")
    save(st)
    return st["skype_token"], st["chat_host"]


def chat_call(method, path, body=None):
    tok, host = session()
    hdr = {"X-Skypetoken": tok, "BehaviorOverride": "redirectAs404", "Accept": "application/json; ver=1.0;",
           "Origin": "https://teams.microsoft.com", "Referer": "https://teams.microsoft.com/"}
    data = None
    if body is not None:
        hdr["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    return http(method, host + path, hdr, data)[1]


def chat_get(path):
    return chat_call("GET", path)


def my_oid():
    """Object id of the signed-in user, from the skype token (JWT)."""
    p = session()[0].split(".")[1]
    c = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    return (c.get("skypeid") or "").split(":")[-1]


def _prop_list(props, key):
    v = (props or {}).get(key)
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def text_of(content, props=None):
    """Plain text of a message: replies, images, emoji, files, cards and reactions are rendered inline."""
    s = content or ""
    s = re.sub(r"<blockquote[^>]*Reply[^>]*>.*?<strong[^>]*>(.*?)</strong>.*?"
               r"(?:<p itemprop=\"preview\">(.*?)</p>)?</blockquote>",
               lambda m: f"> {m.group(1)}: {m.group(2) or ''}\n", s, flags=re.S)
    s = re.sub(r"<img[^>]*AMSImage[^>]*>", "[зображення]", s)
    s = re.sub(r"<img[^>]*Emoji[^>]*alt=\"([^\"]*)\"[^>]*>", r"\1", s)
    s = re.sub(r"<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"[\1]", s)
    s = re.sub(r"<br\s*/?>|</p>|</li>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"[ \t]*\n\s*\n(\s*\n)*", "\n\n", html.unescape(s)).strip()
    for f in _prop_list(props, "files"):
        s += f"\n[файл: {f.get('fileName') or f.get('title') or '?'}]"
    for c in _prop_list(props, "cards"):
        s += f"\n[картка: {c.get('content', {}).get('title') or c.get('contentType') or '?'}]" \
            if isinstance(c, dict) else "\n[картка]"
    for e in _prop_list(props, "emotions"):
        n = len(e.get("users") or [])
        s += f"\n  ↳ {e.get('key')}×{n}"
    return s.strip()


def msg_text(m):
    return text_of(m.get("content"), m.get("properties"))


def msg_json(m, cid=None):
    """One message in the stable JSON shape shared by read/watch/send."""
    props = m.get("properties") or {}
    content = m.get("content") or ""
    att = [{"kind": "image", "url": u} for u in re.findall(r'<img[^>]*AMSImage[^>]*src="([^"]+)"', content)]
    att += [{"kind": "image", "url": u} for u in re.findall(r'<img[^>]*src="([^"]+)"[^>]*AMSImage', content)]
    att += [{"kind": "file", "name": f.get("fileName") or f.get("title"), "url": f.get("objectUrl") or f.get("fileInfo", {}).get("fileUrl")}
            for f in _prop_list(props, "files") if isinstance(f, dict)]
    att += [{"kind": "card", "title": (c.get("content") or {}).get("title") if isinstance(c, dict) else None}
            for c in _prop_list(props, "cards")]
    reactions = [{"key": e.get("key"), "count": len(e.get("users") or []),
                  "users": [u.get("mri") for u in e.get("users") or []]} for e in _prop_list(props, "emotions")]
    reply = None
    q = _prop_list(props, "qtdMsgs")
    if q and isinstance(q[0], dict):
        reply = {"id": str(q[0].get("messageId")), "from_id": q[0].get("sender")}
        pv = re.search(r'itemprop="preview">(.*?)</p>', content, re.S)
        reply["preview"] = html.unescape(re.sub(r"<[^>]+>", "", pv.group(1))) if pv else None
    frm = m.get("from") or ""
    return {"id": m.get("id"), "chat_id": cid or m.get("conversationid"), "time": m.get("composetime"),
            "from": m.get("imdisplayname"), "from_id": frm.rsplit("/", 1)[-1] or None,
            "type": m.get("messagetype"), "text": text_of(content), "html": content,
            "attachments": att, "reactions": reactions, "reply_to": reply,
            "edited": bool(props.get("edittime")), "deleted": bool(props.get("deletetime"))}


def ts(s):
    return (s or "")[:19].replace("T", " ")


def msgs_of(cid, n=10):
    d = chat_get(f"/v1/users/ME/conversations/{urllib.parse.quote(cid, safe='')}/messages"
                 f"?startTime=1&pageSize={n}&view=msnp24Equivalent&targetType={TARGETS}")
    return d.get("messages", [])


def peer_name(cid, names):
    """Name of the other person in a 1:1 chat (cached in state)."""
    if cid in names:
        return names[cid]
    me = my_oid()
    for m in msgs_of(cid, 50):
        if m.get("imdisplayname") and me not in (m.get("from") or ""):
            names[cid] = m["imdisplayname"]
            return names[cid]
    return None


def chat_kind(c):
    i = c["id"]
    if i == "48:notes":
        return "self"
    tp = c.get("threadProperties") or {}
    if tp.get("threadType") == "space" or tp.get("spaceThreadTopic"):
        return "channel"
    return "1:1" if i.endswith("@unq.gbl.spaces") else "group"


def cmd_chats(a):
    d = chat_get("/v1/users/ME/conversations?startTime=0&pageSize=50&view=msnp24Equivalent&targetType=" + TARGETS)
    convs = [c for c in d.get("conversations", []) if c["id"] not in ("48:notifications", "48:threads")]
    st = load()
    st["chats"] = [c["id"] for c in convs]
    names = st.setdefault("names", {})
    rows = []
    for i, c in enumerate(convs):
        tp = c.get("threadProperties") or {}
        last = c.get("lastMessage") or {}
        topic = tp.get("topic") or tp.get("spaceThreadTopic")
        if c["id"] == "48:notes":
            title = "Notes (me)"
        elif topic:
            title = topic
        else:
            nm = peer_name(c["id"], names) if c["id"].endswith("@unq.gbl.spaces") else None
            title = f"[1:1] {nm}" if nm else "[chat]"
        text = msg_text(last)
        preview = f"{last.get('imdisplayname') or '?'}: {text}" if text else f"{last.get('imdisplayname') or '?'}: <{last.get('messagetype', '?')}>"
        rows.append({"n": i, "id": c["id"], "kind": chat_kind(c), "title": title, "time": last.get("composetime"),
                     "from": last.get("imdisplayname"), "preview": preview})
        st.setdefault("titles", {})[c["id"]] = title
    save(st)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    for r in rows:
        print(f"{r['n']:>3}  {ts(r['time']):19}  {r['title'][:42]:42}  {r['preview'][:60]!r}")


def resolve(sel):
    st = load()
    ids = st.get("chats") or []
    if sel.isdigit() and int(sel) < len(ids):
        return ids[int(sel)]
    hits = [i for i in ids if sel in i]
    if len(hits) == 1:
        return hits[0]
    sys.exit("unknown chat; run `chats` first and pass its number" if not hits else "ambiguous id fragment")


def cmd_read(a):
    cid = resolve(a.chat)
    msgs = [m for m in msgs_of(cid, max(a.n * 3, 30)) if m.get("messagetype") in ("Text", "RichText/Html", "RichText")]
    msgs = sorted(msgs, key=lambda m: m.get("composetime", ""))[-a.n:]
    if a.json:
        print(json.dumps([msg_json(m, cid) for m in msgs], ensure_ascii=False, indent=1))
        return
    for m in msgs:
        print(f"[{ts(m.get('composetime'))}] {m.get('imdisplayname') or '?'}: {msg_text(m)}")


def cmd_send(a):
    cid = resolve(a.chat)
    title = load().get("titles", {}).get(cid, cid)
    text = a.text
    if not a.yes:
        if a.json:
            print(json.dumps({"sent": False, "dry_run": True, "chat_id": cid, "text": text}, ensure_ascii=False))
            return
        print(f"DRY RUN. Would send to {title!r}:\n{text}\n\n(add --yes to really send)")
        return
    me = my_oid()
    self_name = next((m["imdisplayname"] for m in msgs_of(cid, 30)
                      if m.get("imdisplayname") and me in (m.get("from") or "")), "")
    body = {"clientmessageid": str(int(time.time() * 1000)),
            "content": html.escape(text).replace("\n", "<br>"),
            "messagetype": "RichText/Html", "contenttype": "text", "imdisplayname": self_name}
    r = chat_call("POST", f"/v1/users/ME/conversations/{urllib.parse.quote(cid, safe='')}/messages", body)
    if a.json:
        print(json.dumps({"sent": True, "chat_id": cid, "client_id": body["clientmessageid"], "text": text}, ensure_ascii=False))
        return
    print(f"sent to {title!r}", "" if DEBUG else "", (r or {}) if DEBUG else "")


TROUTER_TTL = 86400
TROUTER_TC = {"cv": "2024.23.01.2", "ua": "TeamsCDL", "hr": "", "v": "49/25113001312"}
REGISTRAR = "https://teams.microsoft.com/registrar/prod/V2/registrations"


def endpoint_id():
    """Stable endpoint id: a new one per run would hit 'Endpoint limit exceeded'."""
    st = load()
    if not st.get("endpoint"):
        st["endpoint"] = str(uuid.uuid4())
        save(st)
    return st["endpoint"]


def trouter_register(surl):
    body = {"clientDescription": {"appId": "TeamsCDLWebWorker", "aesKey": "", "languageId": "en-US",
                                  "platform": "edge", "templateKey": "TeamsCDLWebWorker_2.1",
                                  "platformUIVersion": TROUTER_TC["v"]},
            "registrationId": endpoint_id(), "nodeId": "",
            "transports": {"TROUTER": [{"context": "", "path": surl, "ttl": TROUTER_TTL}]}}
    http("POST", REGISTRAR, {"Content-Type": "application/json", "X-Skypetoken": session()[0],
                             "Authorization": "Bearer " + access()}, json.dumps(body).encode())


def trouter_urls():
    """Steps 1-2 of the Trouter handshake; returns (ws_url, connectparams, surl)."""
    tok = session()[0]
    ep = endpoint_id()
    _, info = http("POST", f"https://go.trouter.teams.microsoft.com/v4/a?epid={urllib.parse.quote(ep)}",
                   {"X-Skypetoken": tok, "Content-Length": "0"})
    sio = info.get("socketio") or "https://go.trouter.teams.microsoft.com/"
    qs = "".join(f"{k}={urllib.parse.quote(v)}&" for k, v in info["connectparams"].items())
    qs += "tc=" + urllib.parse.quote(json.dumps(TROUTER_TC, separators=(",", ":"))) + "&"
    qs += f"con_num={int(time.time() * 1000)}_1&epid={urllib.parse.quote(ep)}&"
    if info.get("ccid"):
        qs += "ccid=" + urllib.parse.quote(info["ccid"]) + "&"
    qs += "auth=true&timeout=40&"
    _, hs = http("GET", f"{sio}socket.io/1/?v=v4&{qs}", {"X-Skypetoken": tok}, raw=True)
    sid = hs.split(":")[0]
    ws_url = sio.replace("https://", "wss://", 1) + f"socket.io/1/websocket/{sid}?v=v4&{qs}"
    return ws_url, info["connectparams"], info["surl"]


def decode_event(req):
    """Trouter request -> event body dict (handles gzip / cp / gp wrappers)."""
    hdr = req.get("headers") or {}
    body = req.get("body") or ""
    if hdr.get("X-Microsoft-Skype-Content-Encoding") == "gzip":
        body = gzip.decompress(base64.b64decode(body)).decode()
    obj = json.loads(body) if body else {}
    if "cp" in obj:
        obj = json.loads(gzip.decompress(base64.b64decode(obj["cp"])).decode())
    elif "gp" in obj:
        obj = json.loads(base64.b64decode(obj["gp"]).decode())
    return obj


def emit(res, kind, want, as_json, seen):
    cid = res.get("conversationid") or (res.get("conversationLink") or "").rsplit("/", 1)[-1]
    if want and cid not in want:
        return
    mid = (res.get("id"), res.get("version"))
    if mid in seen:
        return
    seen.add(mid)
    st = load()
    title = st.get("titles", {}).get(cid) or st.get("names", {}).get(cid) or cid[-24:]
    text = msg_text(res) or f"<{res.get('messagetype', '?')}>"
    who = res.get("imdisplayname") or "?"
    if as_json:
        print(json.dumps({**msg_json(res, cid), "chat": title, "event": kind}, ensure_ascii=False), flush=True)
    else:
        mark = " (edited)" if kind == "MessageUpdate" else ""
        print(f"[{ts(res.get('composetime'))}] {title} | {who}: {text}{mark}", flush=True)


async def watch_once(want, as_json, seen):
    try:
        import websockets
    except ImportError:
        sys.exit("watch needs the websockets package: pip install --user websockets")
    ws_url, cparams, surl = await asyncio.to_thread(trouter_urls)
    async with websockets.connect(ws_url, additional_headers={"X-Skypetoken": session()[0], "User-Agent": UA},
                                  max_size=2**24, ping_interval=None) as ws:
        counter = 1
        tasks = []

        async def pinger():
            nonlocal counter
            while True:
                await asyncio.sleep(30)
                counter += 1
                await ws.send(f'5:{counter}+::{{"name":"ping"}}')

        async def reregister():
            while True:
                await asyncio.sleep(TROUTER_TTL - 600)
                await asyncio.to_thread(trouter_register, surl)

        async for frame in ws:
            if DEBUG:
                print("ws frame:", frame[:3], len(frame), file=sys.stderr)
            if frame.startswith("1::"):
                auth = {"name": "user.authenticate", "args": [{"headers": {
                    "X-Ms-Test-User": "False", "Authorization": "Bearer " + access(), "X-MS-Migration": "True"},
                    "connectparams": cparams}]}
                await ws.send("5:::" + json.dumps(auth))
                await asyncio.to_thread(trouter_register, surl)
                tasks = [asyncio.create_task(pinger()), asyncio.create_task(reregister())]
                print("connected to Trouter, waiting for messages (Ctrl-C to stop)", file=sys.stderr, flush=True)
            elif frame.startswith("2::"):
                await ws.send("2::")
            elif frame.startswith("3:::"):
                req = json.loads(frame.split(":", 3)[3])
                await ws.send("3:::" + json.dumps({"id": req.get("id"), "status": 200, "body": ""}))
                if (req.get("url") or "").endswith("/messaging"):
                    ev = decode_event(req)
                    if ev.get("type") == "EventMessage" and ev.get("resourceType") in ("NewMessage", "MessageUpdate"):
                        emit(ev["resource"], ev["resourceType"], want, as_json, seen)
            elif frame.startswith("5:") and "trouter.message_loss" in frame:
                await asyncio.to_thread(trouter_register, surl)
        for t in tasks:
            t.cancel()


def cmd_watch(a):
    want = {resolve(c) for c in a.chats}
    seen = set()
    delay = 2
    while True:
        try:
            asyncio.run(watch_once(want, a.json, seen))
            delay = 2
        except KeyboardInterrupt:
            return
        except SystemExit:
            raise
        except Exception as e:
            print(f"connection lost ({type(e).__name__}: {str(e)[:120]}); retry in {delay}s", file=sys.stderr, flush=True)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return
        delay = min(delay * 2, 60)


def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--debug", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--url", help="redirect URL pasted after browser sign-in")
    lg.set_defaults(fn=cmd_login)
    c = sub.add_parser("chats")
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_chats)
    r = sub.add_parser("read")
    r.add_argument("chat")
    r.add_argument("-n", type=int, default=20)
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_read)
    w = sub.add_parser("watch", help="live incoming messages via Trouter websocket")
    w.add_argument("chats", nargs="*", help="chat numbers/id fragments to follow (default: all)")
    w.add_argument("--json", action="store_true", help="one JSON object per line")
    w.set_defaults(fn=cmd_watch)
    sd = sub.add_parser("send")
    sd.add_argument("chat")
    sd.add_argument("text")
    sd.add_argument("--json", action="store_true")
    sd.add_argument("--yes", action="store_true", help="really send (default is a dry run)")
    sd.set_defaults(fn=cmd_send)
    a = ap.parse_args()
    DEBUG = a.debug
    try:
        a.fn(a)
    except SystemExit as e:
        if getattr(a, "json", False) and isinstance(e.code, str):
            print(json.dumps({"error": e.code}, ensure_ascii=False))
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
