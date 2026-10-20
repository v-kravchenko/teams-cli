#!/usr/bin/env python3
"""Sanity check of the teams -> channels tree against the cached conversation list (no network).

    python3 tools/check_tree.py

Fails if a team lost its General channel, a channel shows up twice, or a standard channel belongs to
another team's General.
"""
import importlib.util, json, os, sys

spec = importlib.util.spec_from_file_location("teams_cli", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "teams_cli.py"))
tc = importlib.util.module_from_spec(spec)
sys.modules["teams_cli"] = tc
spec.loader.exec_module(tc)

convs = json.load(open(tc.CONV_CACHE, encoding="utf-8"))["conversations"]
tree = tc.channel_tree(convs)
channels = [ch for t in tree for ch in t["channels"]]
ids = [ch["id"] for ch in channels]
assert ids == list(dict.fromkeys(ids)), "a channel appears in more than one team"

general_of = {t["id"]: ch["id"] for t in tree for ch in t["channels"] if ch["kind"] == "general"}
assert len(general_of) == len(tree), f"{len(tree)} teams but {len(general_of)} General channels"

tp = {c["id"]: (c.get("threadProperties") or {}) for c in convs}
for t in tree:
    for ch in t["channels"]:
        assert ch["name"], f"channel {ch['id']} has no name"
        if ch["kind"] == "channel":
            assert tp[ch["id"]].get("spaceId") == general_of[t["id"]], f"{ch['name']} points at another team's General"
        elif ch["kind"] == "private":
            assert not tp[ch["id"]].get("spaceId"), f"{ch['name']} looks standard but is marked private"

kinds = {k: sum(1 for ch in channels if ch["kind"] == k) for k in ("general", "channel", "private")}
print(f"ok: {len(tree)} teams, {len(channels)} channels {kinds}")
