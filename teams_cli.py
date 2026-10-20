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
import argparse, asyncio, base64, datetime, gzip, hashlib, html, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request, uuid

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
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "teams-cli")
CONV_CACHE = os.path.join(CACHE_DIR, "conversations.json")
CHATINFO_CACHE = os.path.join(CACHE_DIR, "chatinfo.json")
PROFILES_CACHE = os.path.join(CACHE_DIR, "profiles.json")
MESSAGES_DIR = os.path.join(CACHE_DIR, "messages")
CACHE_VERSION = 1
DEBUG = False


SECRET_KEYS = ("refresh_token", "access_token", "skype_token", "presence_token", "substrate_token")
PRESENCE = "https://presence.teams.microsoft.com"
SUBSTRATE = "https://substrate.office.com"
_cache = None
_warned = False
_TOKEN_LOCK = threading.RLock()  # token refresh writes state.json; chat lists fetch in a thread pool


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


def keyring_clear(name):
    try:
        subprocess.run(["secret-tool", "clear", "service", "teams-cli", "key", name],
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def _ensure_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _read_cache(path):
    """Parsed cache file, or None when missing, corrupt or written by a newer version."""
    try:
        _ensure_dir(os.path.dirname(path))
    except OSError:
        pass
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("version", 0) > CACHE_VERSION:
        return None
    return d


def _write_cache(path, data):
    """Atomic write with 0600 from creation; a cache failure must never fail the command."""
    tmp = None
    try:
        d = os.path.dirname(path)
        _ensure_dir(d)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({**data, "version": CACHE_VERSION}, f, ensure_ascii=False)
        os.replace(tmp, path)
        tmp = None
        return True
    except OSError as e:
        print(f"warning: cannot write {path}: {e}", file=sys.stderr)
        return False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load():
    """State = non-secret file + tokens from the keyring (or from the 0600 file as a fallback)."""
    global _cache
    if _cache is None:
        try:
            with open(STATE, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        for k in SECRET_KEYS:
            v = keyring_get(k)
            if v:
                st[k] = v
        _cache = st
    return dict(_cache)


def _write_state(st):
    """Atomic 0600 write: an interrupted token refresh must not truncate state.json."""
    _ensure_dir(STATE_DIR)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=STATE_DIR)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, STATE)
        tmp = None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _strip_legacy_state():
    """Drop chats/names/titles from state.json after they reached the cache (keeps fallback tokens)."""
    try:
        with open(STATE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        return
    if any(k in st for k in LEGACY_CACHE_KEYS):
        _write_state({k: v for k, v in st.items() if k not in LEGACY_CACHE_KEYS})


LEGACY_CACHE_KEYS = ("chats", "names", "titles")


def chatinfo():
    """Cached chat ids, titles and peer names; seeded once from the old state.json keys."""
    info = _read_cache(CHATINFO_CACHE)
    if info is None:
        st = load()
        info = {"chats": st.get("chats") or [], "names": st.get("names") or {},
                "titles": st.get("titles") or {}}
        if _write_cache(CHATINFO_CACHE, info):
            _strip_legacy_state()
    info.setdefault("chats", [])
    info.setdefault("names", {})
    info.setdefault("titles", {})
    return info


def update_chatinfo(**kw):
    info = chatinfo()
    info.update(kw)
    info["updated_at"] = time.time()
    return _write_cache(CHATINFO_CACHE, info)


def save(st):
    global _cache, _warned
    _cache = dict(st)
    migrated = True
    if any(st.get(k) for k in LEGACY_CACHE_KEYS):
        migrated = update_chatinfo(**{k: st[k] for k in LEGACY_CACHE_KEYS if st.get(k)})
    data = ({k: v for k, v in st.items() if k not in LEGACY_CACHE_KEYS} if migrated else dict(st))
    for k in SECRET_KEYS:
        if st.get(k) and keyring_put(k, st[k]):
            del data[k]  # only dropped from the file after a verified read-back
        elif st.get(k) and not _warned:
            _warned = True
            print(f"warning: keyring unavailable, tokens kept in {STATE} (mode 0600)", file=sys.stderr)
    _write_state(data)


class TeamsError(Exception):
    """Any failure the caller should show to the user (HTTP errors, bad arguments, not logged in)."""

    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def http(method, url, headers=None, data=None, raw=False, retries=3):
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": UA, **(headers or {})})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                if raw:
                    return r.status, body.decode()
                return r.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            # GETs are safe to retry; a retried POST/PUT could send twice
            if method == "GET" and e.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(2 ** attempt)
                continue
            raise TeamsError(f"HTTP {e.code} {method} {urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path}: {body[:300]}", e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if method == "GET" and attempt + 1 < retries:
                time.sleep(2 ** attempt)
                continue
            u = urllib.parse.urlsplit(url)
            raise TeamsError(f"{method} {u.netloc}{u.path}: {type(e).__name__}: {e}")


def token_request(form):
    form = {"client_id": CLIENT_ID, "scope": SCOPE, **form}
    _, d = http("POST", f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
                {"Content-Type": "application/x-www-form-urlencoded"}, urllib.parse.urlencode(form).encode())
    return d


def cmd_login(a):
    """Non-interactive: `login` prints the sign-in URL; `login --url URL|-` finishes the login."""
    url = sys.stdin.readline().strip() if a.url == "-" else a.url
    if not url:
        auth = (f"https://login.microsoftonline.com/{TENANT}/oauth2/authorize?client_id={CLIENT_ID}"
                f"&response_type=code&display=popup&prompt=select_account&amr_values=mfa"
                f"&redirect_uri={urllib.parse.quote(REDIRECT, safe='')}")
        print(auth, flush=True)
        print("\nAfter signing in you land on a blank page. Copy its full URL and run:\n"
              "  teams_cli.py login --url '<the copied URL>'\n"
              "(GUI: pass `-` instead of the URL to read it from stdin.)", file=sys.stderr)
        return
    m = re.search(r"[?&]code=([^&\s]+)", url)
    if not m:
        raise TeamsError("no code= in the pasted URL; copy the whole address of the blank page")
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
    with _TOKEN_LOCK:
        st = load()
        if not st.get("refresh_token"):
            raise TeamsError("not logged in: run `login` first")
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
    with _TOKEN_LOCK:
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
        if a.get("region"):
            st["region"] = a["region"]
        if DEBUG:
            print("authz keys:", sorted(a), "| chat_host:", st["chat_host"], file=sys.stderr)
        if not st["skype_token"]:
            raise TeamsError("authz returned no skype token")
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
    return http(method, path if path.startswith("http") else host + path, hdr, data)[1]


def chat_get(path):
    return chat_call("GET", path)


def iter_conversations(page_size=200):
    """Yield pages of conversations, newest first; the backwardLink chain is sequential by nature."""
    path = f"/v1/users/ME/conversations?startTime=0&pageSize={page_size}&view=msnp24Equivalent&targetType={TARGETS}"
    while path:
        d = chat_get(path)
        yield [c for c in d.get("conversations") or [] if c["id"] not in ("48:notifications", "48:threads")]
        path = (d.get("_metadata") or {}).get("backwardLink")


def all_conversations(ttl, refresh=False, progress=None):
    """Full conversation list from ~/.cache/teams-cli; refetches when older than ttl seconds or on refresh."""
    cache = _read_cache(CONV_CACHE)
    if cache and not refresh and time.time() - cache.get("fetched_at", 0) < ttl:
        return cache.get("conversations") or []
    convs = []
    for page in iter_conversations():
        convs.extend(page)
        if progress:
            progress(len(convs))
    _write_cache(CONV_CACHE, {"fetched_at": time.time(), "conversations": convs})
    return convs


def resource_token(resource, key):
    """Access token for another resource (presence, substrate), from the same refresh token."""
    with _TOKEN_LOCK:
        st = load()
        if st.get(key) and st.get(key + "_exp", 0) > time.time() + 120:
            return st[key]
        access()  # makes sure a refresh token is there
        st = load()
        d = token_request({"grant_type": "refresh_token", "refresh_token": st["refresh_token"],
                           "scope": resource + "/.default openid profile offline_access"})
        st["refresh_token"] = d.get("refresh_token", st["refresh_token"])
        st[key] = d["access_token"]
        st[key + "_exp"] = time.time() + int(d.get("expires_in", 3000))
        save(st)
        return st[key]


def mt_call(path, body):
    """POST to the Teams middle tier (people search, profiles): teams.microsoft.com/api/mt/<region>/beta/."""
    tok, host = session()
    region = load().get("region") or urllib.parse.urlsplit(host).hostname.split(".")[0]
    hdr = {"Authorization": "Bearer " + access(), "X-Skypetoken": tok, "Accept": "application/json",
           "Content-Type": "application/json", "Origin": "https://teams.microsoft.com",
           "Referer": "https://teams.microsoft.com/"}
    data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return http("POST", f"https://teams.microsoft.com/api/mt/{region}/beta/{path}", hdr, data)[1]


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
               r"(?:<p itemprop=\"preview\">(.*?)</p>)?\s*</blockquote>",
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
        if not n:
            continue
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
                  "users": [u.get("mri") for u in e.get("users") or []]} for e in _prop_list(props, "emotions") if e.get("users")]
    reply = None
    bq = re.search(r'<blockquote[^>]*schema.skype.com/Reply[^>]*itemid="(\d+)"[^>]*>(.*?)</blockquote>', content, re.S)
    if bq:
        who = re.search(r'itemprop="mri"[^>]*itemid="([^"]+)"', bq.group(2))
        pv = re.search(r'itemprop="preview">(.*?)</p>', bq.group(2), re.S)
        reply = {"id": bq.group(1), "from_id": who.group(1) if who else None,
                 "preview": html.unescape(re.sub(r"<[^>]+>", "", pv.group(1))) if pv else None}
    frm = m.get("from") or ""
    return {"id": m.get("id"), "chat_id": cid or m.get("conversationid"), "time": m.get("composetime"),
            "from": m.get("imdisplayname"), "from_id": frm.rsplit("/", 1)[-1] or None,
            "type": m.get("messagetype"), "text": text_of(content, props), "html": content,
            "attachments": att, "mentions": [{"id": x.get("mri"), "name": x.get("displayName")} for x in _prop_list(props, "mentions") if isinstance(x, dict)],
            "reactions": reactions, "reply_to": reply,
            "thread_id": m.get("rootMessageId") if m.get("rootMessageId") != m.get("id") else None,
            "edited": bool(props.get("edittime")), "deleted": bool(props.get("deletetime"))}


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(s):
    """Strip terminal control characters (ANSI/OSC) from server-provided text; keeps \\n and \\t."""
    return _CONTROL.sub("", "" if s is None else str(s))


def ts(s):
    return clean((s or "")[:19].replace("T", " "))


def ams_url_ok(url):
    """True only for https AMS urls: the skype token is sent to whatever host is here."""
    try:
        u = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "asm.skype.com" or host.endswith(".asm.skype.com"))


def msgs_of(cid, n=10):
    d = chat_get(f"/v1/users/ME/conversations/{urllib.parse.quote(cid, safe='')}/messages"
                 f"?startTime=1&pageSize={n}&view=msnp24Equivalent&targetType={TARGETS}")
    return d.get("messages", [])


REAL_TYPES = ("Text", "RichText/Html", "RichText")


def iter_msgs(cid, page=50, max_pages=40):
    """Messages newest first, following backwardLink page by page."""
    path = (f"/v1/users/ME/conversations/{urllib.parse.quote(cid, safe='')}/messages"
            f"?startTime=1&pageSize={page}&view=msnp24Equivalent&targetType={TARGETS}")
    for _ in range(max_pages):
        d = chat_get(path)
        yield from sorted(d.get("messages", []), key=lambda m: int(m.get("id") or 0), reverse=True)
        link = (d.get("_metadata") or {}).get("backwardLink")
        if not link or not d.get("messages"):
            return
        u = urllib.parse.urlparse(link)
        path = u.path + "?" + u.query


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


def horizon_of(c):
    h = ((c.get("properties") or {}).get("consumptionhorizon") or "").split(";")[0]
    return int(h) if h.isdigit() else 0


def chat_state(c, me):
    """Newest real message and unread count (capped at one page), for chats that need it."""
    last = c.get("lastMessage") or {}
    hz = horizon_of(c)
    lid = int(last.get("id") or 0)
    mine = me in (last.get("from") or "")
    maybe_unread = hz and lid > hz and not mine
    if last.get("content") and not maybe_unread:
        return last, 0, False
    page = sorted((m for m in msgs_of(c["id"], 30) if m.get("messagetype") in REAL_TYPES),
                  key=lambda m: int(m["id"]), reverse=True)
    newest = page[0] if page else last
    unread = sum(1 for m in page if int(m["id"]) > hz and me not in (m.get("from") or "")) if hz else 0
    return newest, unread, bool(page) and len(page) >= 30 and unread == len(page)


def chat_title(c, names, fetch=True):
    tp = c.get("threadProperties") or {}
    topic = tp.get("topic") or tp.get("spaceThreadTopic")
    if c["id"] == "48:notes":
        return "Notes (me)"
    if topic:
        return topic
    nm = names.get(c["id"])
    if nm is None and fetch and c["id"].endswith("@unq.gbl.spaces"):
        nm = peer_name(c["id"], names)  # one request for an uncached 1:1; never do this for the full list
    return f"[1:1] {nm}" if nm else "[chat]"


def chat_row(c, names, me, last=None, unread=None, fetch_names=True, n=None, unread_capped=None):
    """One row for chats/all_chats; `last` defaults to the cached lastMessage. No requests unless fetch_names."""
    last = last or c.get("lastMessage") or {}
    hz = horizon_of(c)
    text = msg_text(last)
    preview = (f"{last.get('imdisplayname') or '?'}: {text}" if text
               else f"{last.get('imdisplayname') or '?'}: <{last.get('messagetype', '?')}>")
    if unread is None:  # None = unknown (no consumptionhorizon in the list payload)
        unread = bool(int(last.get("id") or 0) > hz and me not in (last.get("from") or "")) if hz else None
    row = {"id": c["id"], "kind": chat_kind(c), "title": chat_title(c, names, fetch_names),
           "time": last.get("composetime"), "from": last.get("imdisplayname"), "preview": preview,
           "last_id": last.get("id"), "unread": unread}
    if unread_capped is not None:
        row["unread_capped"] = unread_capped
    row["read_up_to"] = str(hz) if hz else None
    if n is not None:
        row = {"n": n, **row}
    return row


def _msg_cache_path(cid):
    return os.path.join(MESSAGES_DIR, hashlib.sha256(cid.encode()).hexdigest()[:32] + ".json")


def cache_messages(cid, rows):
    """Remember the newest page of a chat as an offline fallback."""
    _write_cache(_msg_cache_path(cid), {"chat_id": cid, "fetched_at": time.time(), "messages": rows[-50:]})


def cached_messages(cid):
    d = _read_cache(_msg_cache_path(cid))
    if not d or d.get("chat_id") != cid or not isinstance(d.get("messages"), list):
        return None
    return d


def update_cached_message(cid, row):
    """Fold a watch event into an existing message cache (no cache, no write)."""
    d = cached_messages(cid)
    if d is None:
        return
    msgs = [m for m in d["messages"] if m.get("id") != row.get("id")] + [row]
    msgs.sort(key=lambda m: int(m.get("id") or 0))
    d["messages"] = msgs[-50:]
    d["fetched_at"] = time.time()
    _write_cache(_msg_cache_path(cid), d)


def list_chats():
    """Recent conversations, newest first; also refreshes the cached chat numbers and titles."""
    from concurrent.futures import ThreadPoolExecutor
    d = chat_get("/v1/users/ME/conversations?startTime=0&pageSize=50&view=msnp24Equivalent&targetType=" + TARGETS)
    convs = [c for c in d.get("conversations", []) if c["id"] not in ("48:notifications", "48:threads")]
    info = chatinfo()
    names = info["names"]
    me = my_oid()
    with ThreadPoolExecutor(8) as ex:
        states = list(ex.map(lambda c: chat_state(c, me), convs))
    rows = []
    for i, (c, (last, unread, capped)) in enumerate(zip(convs, states)):
        row = chat_row(c, names, me, last, unread, n=i, unread_capped=capped)
        rows.append(row)
        info["titles"][c["id"]] = row["title"]
    update_chatinfo(chats=[c["id"] for c in convs], names=names, titles=info["titles"])
    return rows


def list_all_chats(refresh=False):
    """Every conversation (chats, channels of all teams) from the cached full list; `unread` is a boolean."""
    convs = all_conversations(900, refresh,
                              progress=lambda n: print(f"  fetched {n} conversations", file=sys.stderr))
    names = chatinfo()["names"]
    me = my_oid()
    return [chat_row(c, names, me, fetch_names=False) for c in convs]


def channel_tree(convs):
    """Teams -> channels from the conversation list: [{id, name, channels: [{id, name, kind}]}]."""
    teams, team_names = {}, {}
    for c in convs:
        tp = c.get("threadProperties") or {}
        gid = tp.get("groupId")
        if not gid or "@thread.tacv2" not in c.get("id", ""):
            continue
        if tp.get("spaceType") == "standard":  # the team's General channel carries the team name
            kind, name = "general", tp.get("topic") or "General"
            team_names[gid] = tp.get("spaceThreadTopic") or tp.get("topic") or f"team {gid[:8]}"
        elif tp.get("spaceId"):
            kind, name = "channel", tp.get("topic") or tp.get("topicThreadTopic") or c["id"]
        else:
            kind, name = "private", tp.get("spaceThreadTopic") or tp.get("topic") or c["id"]
        teams.setdefault(gid, []).append({"id": c["id"], "name": name, "kind": kind})
    rows = [{"id": gid, "name": team_names.get(gid) or f"team {gid[:8]}",
             "channels": sorted(ch, key=lambda x: (x["kind"] != "general", x["name"].casefold()))}
            for gid, ch in teams.items()]
    return sorted(rows, key=lambda t: t["name"].casefold())


def cmd_teams(a):
    convs = all_conversations(86400, a.refresh,
                              progress=lambda n: print(f"  fetched {n} conversations", file=sys.stderr))
    tree = channel_tree(convs)
    if a.json:
        print(json.dumps(tree, ensure_ascii=False, indent=1))
        return
    for t in tree:
        print(clean(t["name"]))
        for ch in t["channels"]:
            print(f"  {clean(ch['name'])}" + ("  (private)" if ch["kind"] == "private" else ""))


def cmd_chats(a):
    if a.all:
        rows = list_all_chats(a.refresh)
    else:
        rows = list_chats()
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    if a.ids:
        for r in rows:
            n = f"{r['n']:>3}" if "n" in r else "   "
            print(f"{n}  {clean(r['id'])}  {clean(r['title'])}")
        return
    for r in rows:
        unread = r["unread"]
        if unread is True:
            badge = "•"
        elif isinstance(unread, int) and unread:
            badge = f"({unread}{'+' if r.get('unread_capped') else ''})"
        else:
            badge = ""
        prev = clean(r["preview"]).replace("\n", " ")
        n = f"{r['n']:>3}" if "n" in r else "   "
        print(f"{n}  {ts(r['time']):19}  {clean(r['title'])[:36]:36} {badge:>5}  {prev[:60]!r}")


def people(cid):
    """id -> display name for everyone seen speaking in the chat (recent history)."""
    out_ = {}
    for i, m in enumerate(iter_msgs(cid, max_pages=3)):
        if m.get("imdisplayname"):
            out_.setdefault((m.get("from") or "").rsplit("/", 1)[-1], m["imdisplayname"])
    return out_


def build_mentions(cid, text, queries):
    """Replace `@query` in text with Mention spans; returns (html, mentions property list)."""
    content = html.escape(text).replace("\n", "<br>")
    if not queries:
        return content, []
    ppl = people(cid)
    mentions = []
    for q in queries:
        hits = {i: n for i, n in ppl.items() if q.lower() in n.lower()}
        if len(hits) != 1:
            raise TeamsError(f"mention {q!r}: " + ("no one with that name seen in this chat" if not hits
                                           else "ambiguous: " + ", ".join(hits.values())))
        (mri, name), = hits.items()
        token = "@" + html.escape(q)
        pat = re.compile(re.escape(token), re.I)
        if not pat.search(content):
            raise TeamsError(f"mention {q!r}: write @{q} in the text where the mention should go")
        idx = len(mentions)
        content = pat.sub(lambda _: f'<span itemtype="http://schema.skype.com/Mention" itemscope="" itemid="{idx}">{html.escape(name)}</span>', content, count=1)
        mentions.append({"@type": "http://schema.skype.com/Mention", "itemid": idx, "mri": mri,
                         "mentionType": "person", "displayName": name})
    return content, mentions


def chat_members(cid):
    """{chat_id, member_count, members: [{id, name, role}]}; the list may be partial for large chats."""
    d = chat_get(f"/v1/threads/{urllib.parse.quote(cid, safe='')}?view=msnp24Equivalent")
    seen = {}
    for m in msgs_of(cid, 100):
        if m.get("imdisplayname"):
            seen.setdefault((m.get("from") or "").rsplit("/", 1)[-1], m["imdisplayname"])
    rows = [{"id": x["id"], "name": seen.get(x["id"]), "role": x.get("role")} for x in d.get("members", [])]
    known = {r["id"] for r in rows}
    rows += [{"id": i, "name": n, "role": None} for i, n in seen.items() if i not in known]
    names = profiles([r["id"] for r in rows if not r["name"]])
    for r in rows:
        r["name"] = r["name"] or names.get(r["id"])
    total = (d.get("rosterSummary") or {}).get("memberCount") or len(d.get("members", []))
    return {"chat_id": cid, "member_count": total, "members": rows}


def cmd_members(a):
    res = chat_members(resolve(a.chat))
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    print(f"{res['member_count']} members (list may be partial for large chats)")
    for r in res["members"]:
        print(f"  {clean(r['name']) or '?':40} {clean(r['role'] or ''):6} {clean(r['id'])}")


def mri_of(x):
    """'me', an object id or a full mri -> mri."""
    if x == "me":
        x = my_oid()
    return x if x.startswith("8:") or x.startswith("28:") else "8:orgid:" + x


def one_to_one_id(mri):
    """1:1 chat id: both object ids, sorted, joined by '_'."""
    a, b = sorted([my_oid(), mri.split(":")[-1]])
    return f"19:{a}_{b}@unq.gbl.spaces"


PROFILES_TTL = 7 * 86400


def profiles(mris):
    """mri -> display name via the directory; names are cached for 7 days, only missing/stale are fetched."""
    mris = [m for m in dict.fromkeys(mris) if m.startswith("8:orgid:")]
    cache = _read_cache(PROFILES_CACHE) or {"names": {}}
    names = cache.setdefault("names", {})
    now = time.time()
    out_, todo = {}, []
    for m in mris:
        e = names.get(m)
        if isinstance(e, dict) and now - e.get("at", 0) < PROFILES_TTL:
            out_[m] = e.get("name")
        else:
            todo.append(m)
    for i in range(0, len(todo), 500):
        d = mt_call("users/fetchShortProfile?isMailAddress=false&canBeSmtpAddress=false&enableGuest=true"
                    "&includeIBBarredUsers=true&skypeTeamsInfo=true&includeBots=true", todo[i:i + 500])
        for u in (d or {}).get("value", []):
            if u.get("mri"):
                names[u["mri"]] = {"name": u.get("displayName"), "at": now}
                out_[u["mri"]] = u.get("displayName")
    if todo:
        cache["updated_at"] = now
        _write_cache(PROFILES_CACHE, cache)
    return out_


def search_people(query, n=10):
    """Directory search by name or email: [{id, name, email, title, department, chat_id}]."""
    d = mt_call("users/searchV2?includeDLs=true&includeBots=true&enableGuest=true&source=newChat&skypeTeamsInfo=true",
                query)  # the body is the raw text, not JSON
    return [{"id": u.get("mri"), "name": u.get("displayName"), "email": u.get("email") or u.get("mail"),
             "title": u.get("jobTitle"), "department": u.get("department"),
             "chat_id": one_to_one_id(u["mri"]) if (u.get("mri") or "").startswith("8:orgid:") else None}
            for u in (d or {}).get("value", [])][:n]


def cmd_people(a):
    rows = search_people(a.query, a.n)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    for r in rows:
        print(f"{clean(r['name']) or '?':40} {clean(r['email'] or ''):36} {clean(r['department'] or '')}")
        print(f"{'':40} {clean(r['id'])}  1:1: {clean(r['chat_id'])}")


def get_presence(who):
    """Availability of people ('me', object ids or mris): [{id, availability, activity, out_of_office, ...}]."""
    mris = [mri_of(x) for x in who or ["me"]]
    hdr = {"Authorization": "Bearer " + resource_token(PRESENCE, "presence_token"), "Accept": "application/json",
           "Content-Type": "application/json", "x-ms-client-user-agent": "Teams-V2-Desktop", "x-ms-correlation-id": "1"}
    rows = []
    for i in range(0, len(mris), 650):
        _, d = http("POST", PRESENCE + "/v1/presence/getpresence/", hdr,
                    json.dumps([{"mri": m} for m in mris[i:i + 650]]).encode())
        for x in d or []:
            p = x.get("presence") or {}
            rows.append({"id": x.get("mri"), "availability": p.get("availability"), "activity": p.get("activity"),
                         "out_of_office": bool((p.get("calendarData") or {}).get("isOutOfOffice")),
                         "last_active": p.get("lastActiveTime"), "device": p.get("deviceType")})
    return rows


def cmd_presence(a):
    rows = get_presence(a.who)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    names = profiles([r["id"] for r in rows])
    for r in rows:
        ooo = "  out of office" if r["out_of_office"] else ""
        print(f"{clean(names.get(r['id']) or r['id']):40} {clean(r['availability'] or '?'):14} {clean(r['activity'] or '')}{ooo}")


def _addr(x):
    if not isinstance(x, dict):
        return x
    e = x.get("EmailAddress") or x
    return e.get("Name") or e.get("Address")


def search_messages(query, n=20, skip=0):
    """Server-side message search in all chats: {total, results: [{id, chat_id, thread_id, chat, from, time, text}]}."""
    body = {"EntityRequests": [{"EntityType": "Message", "ContentSources": ["Teams"], "Query": {"QueryString": query},
                                "From": skip, "Size": n}],
            "Cvid": str(uuid.uuid4()), "Scenario": {"Name": "powerbar"}}
    _, d = http("POST", SUBSTRATE + "/search/api/v2/query",
                {"Authorization": "Bearer " + resource_token(SUBSTRATE, "substrate_token"),
                 "Accept": "application/json", "Content-Type": "application/json"}, json.dumps(body).encode())
    sets = [r for es in (d or {}).get("EntitySets", []) for r in es.get("ResultSets", [])]
    total = sum(r.get("Total", 0) for r in sets)
    rows = []
    for h in (h for r in sets for h in r.get("Results", [])):
        s = h.get("Source") or {}
        where = " / ".join(x for x in (s.get("TeamName"), s.get("ChannelName")) if x) or s.get("Subject") or s.get("DisplayTo")
        conv, _, root = (s.get("ClientConversationId") or "").partition(";messageid=")
        sent = s.get("DateTimeSent")
        mid = None
        if sent:  # message id = send time in ms
            mid = str(int(datetime.datetime.fromisoformat(sent.replace("Z", "+00:00")).timestamp() * 1000 + 0.5))
        rows.append({"id": mid, "chat_id": conv or None, "thread_id": root if root and "@thread.tacv2" in conv else None, "chat": where, "from": _addr(s.get("From") or s.get("Sender")),
                     "time": s.get("DateTimeSent") or s.get("DateTimeReceived"),
                     "text": s.get("Preview") or h.get("HitHighlightedSummary")})
    return {"total": total, "results": rows}


def cmd_search(a):
    res = search_messages(a.query, a.n)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return
    print(f"{res['total']} matches, showing {len(res['results'])}")
    for r in res["results"]:
        print(f"[{ts(r['time'])}] {clean(r['chat'])[:30]:30} {clean(r['from']) or '?'}: "
              f"{clean(r['text']).replace(chr(10), ' ')[:90]}")
        print(f"{'':22}{clean(r['chat_id'])}")


def resolve(sel):
    if ":" in sel:  # a full chat id needs no cache lookup
        return sel
    ids = chatinfo()["chats"]
    if sel.isdigit() and int(sel) < len(ids):
        return ids[int(sel)]
    hits = [i for i in ids if sel in i]
    if len(hits) == 1:
        return hits[0]
    raise TeamsError("unknown chat; run `chats` first and pass its number" if not hits else "ambiguous id fragment")


def resolve_write(sel):
    """Writes need the exact chat id: list numbers shift whenever a chat gets new activity."""
    if ":" not in sel or sel.isdigit():
        raise TeamsError("write commands need the full chat id, not a list number (see `chats --ids` or `chats --json`)")
    return sel


def read_messages(cid, n=10, before=None, refresh=False):
    """Recent messages (oldest first) in the msg_json shape; falls back to the cached page offline."""
    try:
        msgs = []
        for m in iter_msgs(cid):
            if m.get("messagetype") in REAL_TYPES and (before is None or int(m["id"]) < before):
                msgs.append(m)
                if len(msgs) >= n:
                    break
        msgs.reverse()
        rows = [msg_json(m, cid) for m in msgs]
    except TeamsError as e:
        cached = cached_messages(cid) if not refresh and before is None else None
        if cached is None:
            raise
        age = time.strftime("%Y-%m-%d %H:%M", time.localtime(cached.get("fetched_at", 0)))
        print(f"warning: {clean(e)}; showing cache from {age}", file=sys.stderr)
        return cached["messages"][-n:]
    if before is None and rows:
        cache_messages(cid, rows)
    return rows


def cmd_read(a):
    rows = read_messages(resolve(a.chat), a.n, int(a.before) if a.before else None, a.refresh)
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    for r in rows:
        print(f"[{ts(r['time'])}] {clean(r['from']) or '?'}: {clean(r['text']) or ('(deleted)' if r['deleted'] else '')}")


def prepare_send(cid, text="", reply_to=None, mentions=(), image=None):
    """Validate a send and build its content: {content, mentions, reply_to, image}."""
    if not text and not image:
        raise TeamsError("nothing to send: give text and/or --image")
    content, built = build_mentions(cid, text, mentions)
    if reply_to:
        q = next((m for m in iter_msgs(cid, max_pages=5) if m.get("id") == reply_to), None)
        if not q:
            raise TeamsError(f"message {reply_to} not found in recent history of this chat")
        preview = html.escape(text_of(q.get("content"))[:200])
        content = (f'<blockquote itemscope="" itemtype="http://schema.skype.com/Reply" itemid="{q["id"]}">'
                   f'<strong itemprop="mri" itemid="{(q.get("from") or "").rsplit("/", 1)[-1]}">{html.escape(q.get("imdisplayname") or "")}</strong>'
                   f'<span itemprop="time" itemid="{q["id"]}"></span><p itemprop="preview">{preview}</p></blockquote><p>{content}</p>')
    if image:
        if os.path.splitext(image)[1].lower() not in IMG_TYPES:
            raise TeamsError(f"unsupported image type; supported: {', '.join(IMG_TYPES)}")
        if not os.path.isfile(image):
            raise TeamsError(f"no such file: {image}")
    return {"content": content, "mentions": built, "reply_to": reply_to, "image": image}


def send_prepared(cid, p, text=""):
    """POST a prepared message (uploads the image if any): {chat_id, id, client_id, text}."""
    content = p["content"]
    if p["image"]:
        _, tag = upload_image(cid, p["image"])
        if p["reply_to"]:
            content += f"<p>{tag}</p>"
        else:
            content = (f"<p>{content}</p>" if text else "") + f"<p>{tag}</p>"
    body = {"clientmessageid": str(int(time.time() * 1000)),
            "content": content,
            "messagetype": "RichText/Html", "contenttype": "text", "imdisplayname": self_name(cid)}
    if p["mentions"]:
        body["properties"] = {"mentions": json.dumps(p["mentions"], ensure_ascii=False)}
    r = chat_call("POST", f"/v1/users/ME/conversations/{urllib.parse.quote(cid, safe='')}/messages", body)
    return {"chat_id": cid, "id": str((r or {}).get("OriginalArrivalTime") or "") or None,
            "client_id": body["clientmessageid"], "text": text}


def cmd_send(a):
    cid = resolve_write(a.chat)
    title = chatinfo()["titles"].get(cid, cid)
    text = a.text or ""
    p = prepare_send(cid, text, a.reply_to, a.mention or [], a.image)
    if not a.yes:
        extra = {"reply_to": p["reply_to"], "mentions": [m["displayName"] for m in p["mentions"]], "image": p["image"]}
        if a.json:
            print(json.dumps({"sent": False, "dry_run": True, "chat_id": cid, "text": text, **extra}, ensure_ascii=False))
            return
        notes = "".join(f"\n  {k}: {clean(v)}" for k, v in extra.items() if v)
        print(f"DRY RUN. Would send to {clean(title)!r}:\n{clean(text)}{notes}\n\n(add --yes to really send)")
        return
    res = send_prepared(cid, p, text)
    if a.json:
        print(json.dumps({"sent": True, **res}, ensure_ascii=False))
        return
    print(f"sent to {clean(title)!r}", "" if DEBUG else "", res if DEBUG else "")


AMS = "https://eu-api.asm.skype.com"
IMG_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


def ams_headers(extra=None):
    return {"Authorization": "skype_token " + session()[0], **(extra or {})}


def upload_image(cid, path):
    """Upload an image to AMS; returns (object id, image tag html)."""
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMG_TYPES:
        raise TeamsError(f"unsupported image type {ext!r}; supported: {', '.join(IMG_TYPES)}")
    data = open(path, "rb").read()
    _, obj = http("POST", AMS + "/v1/objects", ams_headers({"Content-Type": "application/json"}),
                  json.dumps({"type": "pish/image", "permissions": {cid: ["read"]},
                              "filename": os.path.basename(path)}).encode())
    oid = obj["id"]
    http("PUT", f"{AMS}/v1/objects/{oid}/content/imgpsh", ams_headers({"Content-Type": IMG_TYPES[ext]}), data)
    tag = (f'<img src="{AMS}/v1/objects/{oid}/views/imgo" itemtype="http://schema.skype.com/AMSImage" '
           f'itemscope="{ext[1:]}" alt="{html.escape(os.path.basename(path))}" id="x_{oid}" itemid="{oid}">')
    return oid, tag


def download_attachments(cid, mid, out="."):
    """Save the image attachments of one message; SharePoint files come back as urls."""
    mid = os.path.basename(mid)
    m = next((m for m in iter_msgs(cid, max_pages=10) if m.get("id") == mid), None)
    if not m:
        raise TeamsError(f"message {mid} not found in recent history of this chat")
    os.makedirs(out, exist_ok=True)
    saved = []
    for i, att in enumerate(msg_json(m, cid)["attachments"]):
        if att["kind"] == "image" and ams_url_ok(att.get("url")):
            req = urllib.request.Request(att["url"], headers={"User-Agent": UA, **ams_headers()})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(r.headers.get("Content-Type"), ".jpg")
            path = os.path.join(out, f"{mid}_{i}{ext}")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            saved.append({"kind": "image", "path": path})
        elif att["kind"] == "image":
            saved.append({"kind": "image", "url": att.get("url"),
                          "note": "not an AMS image: open the url in a browser"})
        elif att["kind"] == "file":
            saved.append({"kind": "file", "name": att.get("name"), "url": att.get("url"), "note": "SharePoint file: open the url in a browser (not downloadable via this API)"})
    return saved


def cmd_download(a):
    saved = download_attachments(resolve(a.chat), a.id, a.out)
    if a.json:
        print(json.dumps(saved, ensure_ascii=False, indent=1))
    else:
        for x in saved:
            if x.get("path"):
                print(clean(x["path"]))
            else:
                print(f"{clean(x.get('name'))}  {clean(x.get('url'))}".strip())


def self_name(cid):
    me = my_oid()
    return next((m["imdisplayname"] for m in msgs_of(cid, 30)
                 if m.get("imdisplayname") and me in (m.get("from") or "")), "")


def msg_path(cid, mid, tail=""):
    return f"/v1/users/ME/conversations/{urllib.parse.quote(cid, safe='')}/messages/{mid}{tail}"


def write_cmd(a, what, fn):
    """Dry run unless --yes; JSON or text result."""
    cid = resolve_write(a.chat)
    title = chatinfo()["titles"].get(cid, cid)
    if not a.yes:
        if a.json:
            print(json.dumps({"done": False, "dry_run": True, "action": what, "chat_id": cid,
                              "id": getattr(a, "id", None)}, ensure_ascii=False))
        else:
            print(f"DRY RUN. Would {clean(what)} in {clean(title)!r} (add --yes to do it)")
        return
    fn(cid)
    if a.json:
        print(json.dumps({"done": True, "action": what, "chat_id": cid, "id": getattr(a, "id", None)}, ensure_ascii=False))
    else:
        print(f"done: {clean(what)} in {clean(title)!r}")


def edit_message(cid, mid, text):
    body = {"content": html.escape(text).replace("\n", "<br>"), "messagetype": "RichText/Html",
            "contenttype": "text", "imdisplayname": self_name(cid)}
    chat_call("PUT", msg_path(cid, mid), body)


def delete_message(cid, mid):
    chat_call("DELETE", msg_path(cid, mid, "?behavior=softDelete"))


def react(cid, mid, emoji, remove=False):
    path = msg_path(cid, mid, "/properties?name=emotions")
    if remove:
        chat_call("DELETE", path, {"emotions": {"key": emoji}})
    else:
        chat_call("PUT", path, {"emotions": {"key": emoji, "value": int(time.time() * 1000)}})


def mark_read(cid, mid=None):
    """Mark everything up to `mid` (default: newest message) read; returns the message id."""
    mid = mid or next((m["id"] for m in iter_msgs(cid, page=10, max_pages=1)), None)
    if not mid:
        raise TeamsError("nothing to mark as read")
    now = int(time.time() * 1000)
    chat_call("PUT", f"/v1/users/ME/conversations/{urllib.parse.quote(cid, safe='')}/properties?name=consumptionhorizon",
              {"consumptionhorizon": f"{mid};{now};{now}"})
    return mid


def cmd_edit(a):
    write_cmd(a, f"edit message {a.id} -> {a.text!r}", lambda cid: edit_message(cid, a.id, a.text))


def cmd_delete(a):
    write_cmd(a, f"delete message {a.id}", lambda cid: delete_message(cid, a.id))


def cmd_react(a):
    write_cmd(a, f"{'remove' if a.remove else 'add'} reaction {a.emoji} on {a.id}",
              lambda cid: react(cid, a.id, a.emoji, a.remove))


def cmd_mark_read(a):
    def run(cid):
        a.id = mark_read(cid, a.id)
    write_cmd(a, f"mark read up to {a.id or 'newest'}", run)


def clear_cache():
    shutil.rmtree(CACHE_DIR, ignore_errors=True)


def cmd_cache(a):
    clear_cache()
    print(f"cache cleared: {CACHE_DIR}")


def cmd_logout(a):
    """Remove tokens (keyring and the state fallback) and the local cache."""
    for k in SECRET_KEYS:
        keyring_clear(k)
    st = load()
    for k in list(SECRET_KEYS) + ["access_exp", "skype_exp", "presence_token_exp", "substrate_token_exp"]:
        st.pop(k, None)
    save(st)
    clear_cache()
    print("logged out; tokens and cache removed")


TROUTER_TTL = 86400
SILENCE_LIMIT = 180
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


def dump_event(ev):
    """TEAMS_DUMP=<path>: append raw Trouter events (full message text) with 0600 perms."""
    path = os.environ.get("TEAMS_DUMP")
    if not path:
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False)[:3000] + "\n")
    except OSError as e:
        print(f"warning: cannot write TEAMS_DUMP: {e}", file=sys.stderr)


def out(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def status(state, **kw):
    """Watch status event (connected, stalled, disconnected, ...)."""
    return {"event": "status", "state": state, **kw}


def emit_read(res, want, horizons):
    """ConversationUpdate -> `read` event when the read marker moved."""
    cid = res.get("id")
    hz = horizon_of(res)
    if not hz or (want and cid not in want) or horizons.get(cid) == hz:
        return None
    horizons[cid] = hz
    return {"event": "read", "chat_id": cid, "read_up_to": str(hz)}


def emit(res, kind, want, seen):
    """Trouter message -> event dict, or None when it is filtered out or already seen."""
    cid = res.get("conversationid") or (res.get("conversationLink") or "").rsplit("/", 1)[-1]
    if want and cid not in want:
        return None
    mt = res.get("messagetype") or ""
    props = res.get("properties") or {}
    base = msg_json(res, cid)
    if mt.startswith("Control/"):
        if mt not in ("Control/Typing", "Control/ClearTyping"):
            return None
        return {"event": "typing" if mt == "Control/Typing" else "typing_stop", "chat_id": cid,
                "from": base["from"], "from_id": base["from_id"], "time": base["time"]}
    mid = (res.get("id"), res.get("version"))
    if mid in seen:
        return None
    seen.add(mid)
    info = chatinfo()
    title = info["titles"].get(cid) or info["names"].get(cid) or cid[-24:]
    if mt not in REAL_TYPES:
        return {**base, "chat": title, "event": "system"}
    if props.get("deletetime"):
        event = "message_deleted"
    else:
        event = "message" if kind == "NewMessage" else "message_updated"
    update_cached_message(cid, base)
    return {**base, "chat": title, "event": event}


def cli_event(as_json):
    """Watch events the CLI way: one JSON line each, or the human lines (status to stderr)."""
    marks = {"message": "", "message_updated": " (edited)", "message_deleted": " (deleted)"}

    def on_event(ev):
        kind = ev["event"]
        if as_json:
            out(ev)
        elif kind == "status":
            print(f"[{clean(ev['state'])}] " + " ".join(f"{k}={clean(v)}" for k, v in ev.items() if k not in ("event", "state")),
                  file=sys.stderr, flush=True)
        elif kind in marks:
            text = clean(ev["text"]) or f"<{clean(ev['type'])}>"
            print(f"[{ts(ev['time'])}] {clean(ev['chat'])} | {clean(ev['from']) or '?'}: {text}{marks[kind]}", flush=True)
    return on_event


async def watch_once(want, on_event, seen, horizons):
    try:
        import websockets
    except ImportError:
        import site
        site.addsitedir(site.getusersitepackages())  # `python3 -I` hides user site-packages
        try:
            import websockets
        except ImportError:
            raise TeamsError("watch needs the websockets package: pip install --user websockets")
    ws_url, cparams, surl = await asyncio.to_thread(trouter_urls)
    async with websockets.connect(ws_url, additional_headers={"X-Skypetoken": session()[0], "User-Agent": UA},
                                  max_size=2**24, ping_interval=None) as ws:
        counter = 1
        tasks = []
        last = time.monotonic()

        async def pinger():
            nonlocal counter
            while True:
                await asyncio.sleep(30)
                counter += 1
                await ws.send(f'5:{counter}+::{{"name":"ping"}}')

        async def reregister():
            while True:
                await asyncio.sleep(TROUTER_TTL - 600)
                try:
                    await asyncio.to_thread(trouter_register, surl)
                except Exception as e:
                    on_event(status("error", detail=f"reregister failed: {type(e).__name__}: {str(e)[:100]}"))
                    await ws.close()
                    return

        async def watchdog():
            while True:
                await asyncio.sleep(20)
                if time.monotonic() - last > SILENCE_LIMIT:
                    on_event(status("stalled", detail=f"no frames for {SILENCE_LIMIT}s"))
                    await ws.close()
                    return

        async for frame in ws:
            last = time.monotonic()
            if DEBUG:
                print("ws frame:", frame[:3], len(frame), file=sys.stderr)
            if frame.startswith("1::"):
                auth = {"name": "user.authenticate", "args": [{"headers": {
                    "X-Ms-Test-User": "False", "Authorization": "Bearer " + access(), "X-MS-Migration": "True"},
                    "connectparams": cparams}]}
                await ws.send("5:::" + json.dumps(auth))
                await asyncio.to_thread(trouter_register, surl)
                tasks = [asyncio.create_task(t()) for t in (pinger, reregister, watchdog)]
                on_event(status("connected"))
            elif frame.startswith("2::"):
                await ws.send("2::")
            elif frame.startswith("3:::"):
                req = json.loads(frame.split(":", 3)[3])
                await ws.send("3:::" + json.dumps({"id": req.get("id"), "status": 200, "body": ""}))
                if (req.get("url") or "").endswith("/messaging"):
                    ev = decode_event(req)
                    if ev.get("type") == "EventMessage" and ev.get("resourceType") in ("NewMessage", "MessageUpdate"):
                        e = emit(ev["resource"], ev["resourceType"], want, seen)
                    elif ev.get("resourceType") == "ConversationUpdate":
                        e = emit_read(ev["resource"], want, horizons)
                    elif os.environ.get("TEAMS_DUMP"):
                        dump_event(ev)
                        e = None
                    else:
                        e = None
                    if e:
                        on_event(e)
            elif frame.startswith("5:") and "trouter.message_loss" in frame:
                await asyncio.to_thread(trouter_register, surl)
        for t in tasks:
            t.cancel()


def cmd_watch(a):
    want = {resolve(c) for c in a.chats}
    seen, horizons = set(), {}
    on_event = cli_event(a.json)
    delay = 2
    while True:
        try:
            asyncio.run(watch_once(want, on_event, seen, horizons))
            delay = 2
            on_event(status("disconnected", retry_in=delay))
        except KeyboardInterrupt:
            return
        except TeamsError as e:
            on_event(status("auth_failed", detail=str(e)))
            raise
        except Exception as e:
            on_event(status("disconnected", detail=f"{type(e).__name__}: {str(e)[:120]}", retry_in=delay))
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
    lg.add_argument("--url", help="redirect URL pasted after browser sign-in ('-' reads it from stdin)")
    lg.set_defaults(fn=cmd_login)
    c = sub.add_parser("chats")
    c.add_argument("--json", action="store_true")
    c.add_argument("--ids", action="store_true", help="print full chat ids (needed by write commands)")
    c.add_argument("--all", action="store_true", help="every conversation incl. channels (cached; unread is a dot)")
    c.add_argument("--refresh", action="store_true", help="with --all: refetch the full conversation list")
    c.set_defaults(fn=cmd_chats)
    tm = sub.add_parser("teams", help="teams -> channels tree (cached for 24 h)")
    tm.add_argument("--refresh", action="store_true", help="refetch the conversation list")
    tm.add_argument("--json", action="store_true")
    tm.set_defaults(fn=cmd_teams)
    r = sub.add_parser("read")
    r.add_argument("chat")
    r.add_argument("-n", type=int, default=20)
    r.add_argument("--before", help="only messages older than this message id (for paging up)")
    r.add_argument("--refresh", action="store_true", help="ignore the cached page and fetch from the server")
    r.add_argument("--json", action="store_true")
    r.set_defaults(fn=cmd_read)
    mb = sub.add_parser("members", help="participants of a chat (partial for large chats)")
    mb.add_argument("chat")
    mb.add_argument("--json", action="store_true")
    mb.set_defaults(fn=cmd_members)
    pp = sub.add_parser("people", help="search the directory by name or email")
    pp.add_argument("query")
    pp.add_argument("-n", type=int, default=10)
    pp.add_argument("--json", action="store_true")
    pp.set_defaults(fn=cmd_people)
    pr = sub.add_parser("presence", help="availability of people (default: me)")
    pr.add_argument("who", nargs="*", help="'me', object ids or mris (8:orgid:...)")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(fn=cmd_presence)
    se = sub.add_parser("search", help="server-side search of messages in all chats")
    se.add_argument("query")
    se.add_argument("-n", type=int, default=20)
    se.add_argument("--json", action="store_true")
    se.set_defaults(fn=cmd_search)
    dl = sub.add_parser("download", help="save image attachments of a message (files: prints the SharePoint url)")
    dl.add_argument("chat")
    dl.add_argument("id")
    dl.add_argument("-o", "--out", default=".")
    dl.add_argument("--json", action="store_true")
    dl.set_defaults(fn=cmd_download)
    w = sub.add_parser("watch", help="live incoming messages via Trouter websocket")
    w.add_argument("chats", nargs="*", help="chat numbers/id fragments to follow (default: all)")
    w.add_argument("--json", action="store_true", help="one JSON object per line")
    w.set_defaults(fn=cmd_watch)
    sd = sub.add_parser("send")
    sd.add_argument("chat")
    sd.add_argument("text", nargs="?", default="")
    sd.add_argument("--image", metavar="PATH", help="attach an image (png/jpg/gif/webp)")
    sd.add_argument("--mention", action="append", metavar="NAME",
                    help="part of a name seen in the chat; put @NAME in the text (repeatable)")
    sd.add_argument("--reply-to", metavar="ID", help="quote this message id")
    sd.add_argument("--json", action="store_true")
    sd.add_argument("--yes", action="store_true", help="really send (default is a dry run)")
    sd.set_defaults(fn=cmd_send)
    for name, fn, extra in (("edit", cmd_edit, ("id", "text")), ("delete", cmd_delete, ("id",)),
                            ("react", cmd_react, ("id", "emoji")), ("mark-read", cmd_mark_read, ())):
        sp = sub.add_parser(name)
        sp.add_argument("chat")
        for x in extra:
            sp.add_argument(x)
        if name == "mark-read":
            sp.add_argument("id", nargs="?", help="message id (default: newest)")
        if name == "react":
            sp.add_argument("--remove", action="store_true")
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--yes", action="store_true", help="really do it (default is a dry run)")
        sp.set_defaults(fn=fn)
    lo = sub.add_parser("logout", help="remove stored tokens and the local cache")
    lo.set_defaults(fn=cmd_logout)
    ca = sub.add_parser("cache", help="local cache maintenance")
    ca.add_argument("action", nargs="?", default="clear", choices=["clear"])
    ca.set_defaults(fn=cmd_cache)
    a = ap.parse_args()
    DEBUG = a.debug
    try:
        a.fn(a)
    except TeamsError as e:
        msg = clean(str(e))
        if getattr(a, "json", False):
            print(json.dumps({"error": msg}, ensure_ascii=False))
            sys.exit(1)
        sys.exit(msg)


if __name__ == "__main__":
    main()
