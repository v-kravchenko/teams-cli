#!/usr/bin/env python3
"""Read-only render benchmark for teams-for-linux (Electron) over CDP.

Safety: only scrolls the open chat with mouse-wheel events and reads metrics.
No clicks, no key events, no typing, no navigation. Enforced by a CDP method
allowlist (see ALLOWED) plus a wheel-only check on Input.dispatchMouseEvent.

Start Teams with:  teams-for-linux --remote-debugging-port=9222
Open ONE long chat/channel and leave it open, then:
  python3 -I teams_perf.py run --label gpu-on-load
  python3 -I teams_perf.py compare results/*.json
"""
import argparse, asyncio, glob, json, os, statistics, sys, time, urllib.request

import websockets

ALLOWED = {
    "Runtime.enable", "Runtime.evaluate",
    "Performance.enable", "Performance.getMetrics",
    "Input.dispatchMouseEvent",  # restricted to type=mouseWheel below
}

HOOK_JS = r"""
(() => {
  if (window.__perf) return 'already';
  const p = window.__perf = {frames: [], long: [], collecting: false, last: 0};
  const tick = t => {
    if (p.collecting && p.last) p.frames.push(t - p.last);
    p.last = t;
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  new PerformanceObserver(l => {
    if (!p.collecting) return;
    for (const e of l.getEntries()) p.long.push(e.duration);
  }).observe({type: 'longtask', buffered: false});
  return 'installed';
})()
"""

FIND_SCROLLER_JS = r"""
(() => {
  let best = null, area = 0;
  for (const el of document.querySelectorAll('*')) {
    if (el.scrollHeight <= el.clientHeight + 200) continue;
    const oy = getComputedStyle(el).overflowY;
    if (oy !== 'auto' && oy !== 'scroll') continue;
    const r = el.getBoundingClientRect();
    if (r.width < 400 || r.height < 200) continue;
    if (r.width * r.height > area) { area = r.width * r.height; best = r; }
  }
  return best ? {x: best.x + best.width / 2, y: best.y + best.height / 2,
                 w: best.width, h: best.height} : null;
})()
"""


def pct(xs, q):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))]


class Cdp:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    async def send(self, method, params=None):
        params = params or {}
        if method not in ALLOWED:
            raise RuntimeError(f"blocked CDP method: {method}")
        if method == "Input.dispatchMouseEvent" and params.get("type") != "mouseWheel":
            raise RuntimeError("blocked: only mouseWheel input is allowed")
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def eval(self, expr):
        r = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")


def teams_cpu():
    """{pid: (kind, cpu_ticks)} for every teams-for-linux process."""
    out = {}
    for d in glob.glob("/proc/[0-9]*"):
        try:
            raw = open(d + "/cmdline", "rb").read()
            if not raw.startswith(b"/opt/teams-for-linux/"):
                continue
            # Chromium rewrites argv into one space-separated blob, so search the raw bytes
            i = raw.find(b"--type=")
            kind = raw[i + 7:].split(b" ", 1)[0].split(b"\0", 1)[0].decode() if i >= 0 else "main"
            f = open(d + "/stat").read().rsplit(")", 1)[1].split()
            out[int(d[6:])] = (kind, int(f[11]) + int(f[12]))
        except (OSError, IndexError):
            pass
    return out


def sys_cpu():
    f = [int(x) for x in open("/proc/stat").readline().split()[1:]]
    return sum(f), f[3] + f[4]  # total, idle+iowait


def psi_cpu():
    try:
        return float(open("/proc/pressure/cpu").readline().split("avg10=")[1].split()[0])
    except (OSError, IndexError):
        return None


def cpu_report(a, b, secs):
    hz = os.sysconf("SC_CLK_TCK")
    per = {}
    for pid, (kind, t1) in b.items():
        if pid in a:
            per[kind] = per.get(kind, 0) + (t1 - a[pid][1]) / hz / secs * 100
    return {k: round(v, 1) for k, v in sorted(per.items(), key=lambda kv: -kv[1])}


def frame_stats(frames):
    # gaps over 1s mean rAF was paused (window hidden/occluded), not slow frames
    paused = [f for f in frames if f > 1000]
    frames = [f for f in frames if f <= 1000]
    if not frames:
        return {"paused_gaps": len(paused)}
    return {
        "paused_gaps": len(paused),
        "paused_total_s": round(sum(paused) / 1000, 1),
        "frames": len(frames),
        "fps_avg": round(1000 / statistics.mean(frames), 1),
        "fps_median": round(1000 / pct(frames, .5), 1),
        "dt_p50_ms": round(pct(frames, .5), 1),
        "dt_p95_ms": round(pct(frames, .95), 1),
        "dt_p99_ms": round(pct(frames, .99), 1),
        "dt_max_ms": round(max(frames), 1),
        "janky_gt33ms": sum(f > 33 for f in frames),
        "janky_gt100ms": sum(f > 100 for f in frames),
    }


async def phase(c, name, secs, scroller=None):
    """Run one phase; returns metrics. scroller=None means idle."""
    await c.eval("(()=>{const p=window.__perf;p.frames=[];p.long=[];p.last=0;p.collecting=true})()")
    m0 = {m["name"]: m["value"] for m in (await c.send("Performance.getMetrics"))["metrics"]}
    cpu0, sys0, t0 = teams_cpu(), sys_cpu(), time.monotonic()
    if scroller is None:
        await asyncio.sleep(secs)
    else:
        direction, steps = -1, 0
        while time.monotonic() - t0 < secs:
            await c.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": scroller["x"], "y": scroller["y"],
                "deltaX": 0, "deltaY": 120 * direction})
            steps += 1
            if steps % 60 == 0:
                direction = -direction
            await asyncio.sleep(0.016)
    dt = time.monotonic() - t0
    cpu1, sys1 = teams_cpu(), sys_cpu()
    m1 = {m["name"]: m["value"] for m in (await c.send("Performance.getMetrics"))["metrics"]}
    data = await c.eval("(()=>{const p=window.__perf;p.collecting=false;return {f:p.frames,l:p.long}})()")
    long = data["l"]
    busy = 100 * (1 - (sys1[1] - sys0[1]) / max(1, sys1[0] - sys0[0]))
    delta = lambda k: round((m1.get(k, 0) - m0.get(k, 0)) / dt * 100, 1)
    return {
        "seconds": round(dt, 1),
        "frame": frame_stats(data["f"]),
        "longtasks": {"count": len(long), "total_ms": round(sum(long)), "max_ms": round(max(long, default=0)),
                      "p95_ms": round(pct(long, .95))},
        "page_busy_pct": {k: delta(k) for k in ("TaskDuration", "ScriptDuration", "LayoutDuration", "RecalcStyleDuration")},
        "teams_cpu_pct": cpu_report(cpu0, cpu1, dt),
        "system_cpu_busy_pct": round(busy, 1),
    }


async def run(a):
    tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{a.port}/json"))
    pages = [t for t in tabs if t["type"] == "page"
             and any(h in t["url"] for h in ("teams.microsoft.com", "teams.cloud.microsoft", "office.com"))]
    if not pages:
        sys.exit("no Teams page found; is teams-for-linux started with --remote-debugging-port?")
    async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=2**26) as ws:
        c = Cdp(ws)
        await c.send("Runtime.enable")
        await c.send("Performance.enable")
        print("hook:", await c.eval(HOOK_JS), "| visibility:", await c.eval("document.visibilityState"),
              "| focus:", await c.eval("document.hasFocus()"))
        scroller = await c.eval(FIND_SCROLLER_JS)
        if not scroller:
            sys.exit("no long scrollable pane found; open a long chat first")
        print("scroll pane:", {k: round(v) for k, v in scroller.items()})
        res = {"label": a.label, "time": time.strftime("%F %T"), "loadavg": open("/proc/loadavg").read().split()[:3],
               "psi_cpu_some_avg10": psi_cpu(), "phases": {}}
        res["phases"]["idle"] = await phase(c, "idle", a.idle)
        res["phases"]["scroll"] = await phase(c, "scroll", a.scroll, scroller)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, f"{time.strftime('%H%M%S')}-{a.label}.json")
    json.dump(res, open(path, "w"), indent=1)
    show(res)
    print("saved:", path)


def show(r):
    print(f"\n== {r['label']}  load={r['loadavg']} psi={r['psi_cpu_some_avg10']}")
    for name, p in r["phases"].items():
        f, l = p["frame"], p["longtasks"]
        print(f" [{name}] paused={f.get('paused_gaps')} fps={f.get('fps_avg')} dt p95={f.get('dt_p95_ms')}ms p99={f.get('dt_p99_ms')}ms "
              f"janky>33={f.get('janky_gt33ms')} >100={f.get('janky_gt100ms')} | "
              f"longtasks n={l['count']} total={l['total_ms']}ms max={l['max_ms']}ms | "
              f"renderer-busy={p['page_busy_pct']['TaskDuration']}% script={p['page_busy_pct']['ScriptDuration']}% | "
              f"sys-cpu={p['system_cpu_busy_pct']}% teams={p['teams_cpu_pct']}")


def compare(a):
    for path in a.files:
        show(json.load(open(path)))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--label", required=True)
    r.add_argument("--port", type=int, default=9222)
    r.add_argument("--idle", type=int, default=10)
    r.add_argument("--scroll", type=int, default=30)
    r.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"))
    c = sub.add_parser("compare")
    c.add_argument("files", nargs="+")
    a = ap.parse_args()
    if a.cmd == "run":
        asyncio.run(run(a))
    else:
        compare(a)


if __name__ == "__main__":
    main()
