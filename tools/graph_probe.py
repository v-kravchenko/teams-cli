#!/usr/bin/env python3
"""Read-only probe: reuse Teams' Graph token via CDP, GET /me/chats?$top=5. Never prints the token."""
import asyncio, base64, json, time, urllib.request, urllib.error
import websockets

JS = """(async()=>{const r=await window.teamsForLinuxReactHandler.acquireToken('https://graph.microsoft.com',{});
return r&&r.success?{ok:true,token:r.token}:{ok:false,error:r&&r.error}})()"""


async def get_token():
    tabs = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json"))
    page = next(t for t in tabs if t["type"] == "page" and "teams" in t["url"] and t["url"].startswith("https"))
    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=2**24) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": JS, "awaitPromise": True, "returnByValue": True}}))
        while (m := json.loads(await ws.recv())).get("id") != 1:
            pass
    v = m["result"]["result"].get("value") or {}
    if not v.get("ok"):
        raise SystemExit(f"token failed: {v.get('error') or m}")
    return v["token"]


def claims(tok):
    p = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


tok = asyncio.run(get_token())
c = claims(tok)
print("scopes:", c.get("scp"), "| roles:", c.get("roles"), "| expires in", int(c["exp"] - time.time()), "s")
req = urllib.request.Request("https://graph.microsoft.com/v1.0/me/chats?$top=5&$select=id,chatType,topic,lastUpdatedDateTime",
                             headers={"Authorization": "Bearer " + tok})
try:
    d = json.load(urllib.request.urlopen(req, timeout=20))
    print("HTTP 200, chats:", len(d["value"]))
    for ch in d["value"]:
        print(" ", ch["chatType"], "|", ch.get("topic") or "(no topic)", "|", ch["lastUpdatedDateTime"])
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:400])
