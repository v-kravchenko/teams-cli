import io
import json
import os
import stat
import time
import unittest
from contextlib import redirect_stderr

from support import TempState, fixture, tc


class WriteCache(TempState):
    def test_perms_and_version(self):
        tc._write_cache(tc.CHATINFO_CACHE, {"chats": ["19:x"]})
        self.assertEqual(stat.S_IMODE(os.stat(tc.CACHE_DIR).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(tc.CHATINFO_CACHE).st_mode), 0o600)
        d = tc._read_cache(tc.CHATINFO_CACHE)
        self.assertEqual(d["version"], tc.CACHE_VERSION)
        self.assertEqual(d["chats"], ["19:x"])

    def test_corrupt_and_newer_are_ignored(self):
        os.makedirs(tc.CACHE_DIR)
        with open(tc.CHATINFO_CACHE, "w") as f:
            f.write("{not json")
        self.assertIsNone(tc._read_cache(tc.CHATINFO_CACHE))
        with open(tc.CHATINFO_CACHE, "w") as f:
            json.dump({"version": tc.CACHE_VERSION + 1, "chats": []}, f)
        self.assertIsNone(tc._read_cache(tc.CHATINFO_CACHE))

    def test_no_tmp_files_left(self):
        tc._write_cache(tc.CHATINFO_CACHE, {"chats": []})
        self.assertEqual([f for f in os.listdir(tc.CACHE_DIR) if f.startswith(".tmp-")], [])


class Conversations(TempState):
    def pages(self):
        return [[{"id": "19:a"}]]

    def test_fresh_cache_skips_network(self):
        tc._write_cache(tc.CONV_CACHE, {"fetched_at": time.time(), "conversations": [{"id": "cached"}]})
        tc.iter_conversations = lambda page_size=200: (_ for _ in ()).throw(AssertionError("network"))
        self.assertEqual(tc.all_conversations(900), [{"id": "cached"}])

    def test_stale_cache_refetches_and_writes(self):
        tc._write_cache(tc.CONV_CACHE, {"fetched_at": 0, "conversations": [{"id": "old"}]})
        tc.iter_conversations = lambda page_size=200: iter(self.pages())
        self.assertEqual(tc.all_conversations(900), [{"id": "19:a"}])
        self.assertEqual(tc._read_cache(tc.CONV_CACHE)["conversations"], [{"id": "19:a"}])

    def test_refresh_ignores_fresh_cache(self):
        tc._write_cache(tc.CONV_CACHE, {"fetched_at": time.time(), "conversations": [{"id": "old"}]})
        tc.iter_conversations = lambda page_size=200: iter(self.pages())
        self.assertEqual(tc.all_conversations(900, refresh=True), [{"id": "19:a"}])

    def test_corrupt_cache_refetches(self):
        os.makedirs(tc.CACHE_DIR)
        with open(tc.CONV_CACHE, "w") as f:
            f.write("junk")
        tc.iter_conversations = lambda page_size=200: iter(self.pages())
        self.assertEqual(tc.all_conversations(900), [{"id": "19:a"}])

    def test_legacy_file_without_version_is_used(self):
        os.makedirs(tc.CACHE_DIR)
        with open(tc.CONV_CACHE, "w") as f:
            json.dump({"fetched_at": time.time(), "conversations": [{"id": "legacy"}]}, f)
        self.assertEqual(tc.all_conversations(900), [{"id": "legacy"}])


class ChatInfo(TempState):
    def write_state(self, data):
        os.makedirs(tc.STATE_DIR, mode=0o700, exist_ok=True)
        with open(tc.STATE, "w") as f:
            json.dump(data, f)

    def test_seeds_from_legacy_state_and_strips_it(self):
        self.write_state({"chats": ["19:x"], "names": {"19:x": "Alice"},
                          "titles": {"19:x": "Alice chat"}, "endpoint": "e"})
        tc._cache = None
        info = tc.chatinfo()
        self.assertEqual(info["chats"], ["19:x"])
        self.assertEqual(info["names"], {"19:x": "Alice"})
        self.assertTrue(os.path.exists(tc.CHATINFO_CACHE))
        with open(tc.STATE) as f:
            state = json.load(f)
        self.assertEqual(state, {"endpoint": "e"})

    def test_save_moves_legacy_keys(self):
        self.write_state({"chats": ["19:x"], "names": {}, "titles": {"19:x": "X"}})
        tc._cache = None
        tc.save(tc.load())
        with open(tc.STATE) as f:
            self.assertEqual(json.load(f), {})
        self.assertEqual(tc.chatinfo()["titles"], {"19:x": "X"})

    def test_update_writes(self):
        tc.update_chatinfo(chats=["19:y"], titles={"19:y": "Y"})
        self.assertEqual(tc.chatinfo()["chats"], ["19:y"])


class Messages(TempState):
    def rows(self, *ids):
        return [{"id": str(i), "text": f"m{i}"} for i in ids]

    def test_roundtrip_and_perms(self):
        tc.cache_messages("19:x", self.rows(1, 2))
        d = tc.cached_messages("19:x")
        self.assertEqual([m["id"] for m in d["messages"]], ["1", "2"])
        self.assertEqual(stat.S_IMODE(os.stat(tc._msg_cache_path("19:x")).st_mode), 0o600)
        self.assertIsNone(tc.cached_messages("19:other"))

    def test_cache_keeps_newest_50(self):
        tc.cache_messages("19:x", self.rows(*range(60)))
        self.assertEqual(len(tc.cached_messages("19:x")["messages"]), 50)
        self.assertEqual(tc.cached_messages("19:x")["messages"][-1]["id"], "59")

    def test_update_dedups_and_sorts(self):
        tc.cache_messages("19:x", self.rows(1, 3))
        tc.update_cached_message("19:x", {"id": "2", "text": "new"})
        tc.update_cached_message("19:x", {"id": "3", "text": "edited"})
        d = tc.cached_messages("19:x")
        self.assertEqual([m["id"] for m in d["messages"]], ["1", "2", "3"])
        self.assertEqual(d["messages"][-1]["text"], "edited")


class ReadFallback(TempState):
    def test_falls_back_to_cache(self):
        tc.cache_messages("19:x", [{"id": "1", "text": "cached"}])
        tc.iter_msgs = lambda cid, **kw: (_ for _ in ()).throw(tc.TeamsError("offline"))
        err = io.StringIO()
        with redirect_stderr(err):
            rows = tc.read_messages("19:x", n=5)
        self.assertEqual(rows, [{"id": "1", "text": "cached"}])
        self.assertIn("showing cache", err.getvalue())

    def test_refresh_and_before_do_not_fall_back(self):
        tc.cache_messages("19:x", [{"id": "1", "text": "cached"}])
        tc.iter_msgs = lambda cid, **kw: (_ for _ in ()).throw(tc.TeamsError("offline"))
        with self.assertRaises(tc.TeamsError):
            tc.read_messages("19:x", refresh=True)
        with self.assertRaises(tc.TeamsError):
            tc.read_messages("19:x", before=10)
        with self.assertRaises(tc.TeamsError):
            tc.read_messages("19:no-cache")

    def test_success_writes_cache(self):
        msg = {"id": "1700000001000", "messagetype": "Text", "content": "hi",
               "composetime": "2024-01-01T10:00:00.000Z"}
        tc.iter_msgs = lambda cid, **kw: iter([msg])
        rows = tc.read_messages("19:x")
        self.assertEqual(len(rows), 1)
        self.assertEqual(tc.cached_messages("19:x")["messages"][0]["id"], "1700000001000")


class Profiles(TempState):
    def test_cached_after_first_call(self):
        calls = []

        def mt(path, body):
            calls.append(body)
            return {"value": [{"mri": "8:orgid:a", "displayName": "Alice"}]}
        tc.mt_call = mt
        self.assertEqual(tc.profiles(["8:orgid:a"]), {"8:orgid:a": "Alice"})
        tc.mt_call = lambda path, body: (_ for _ in ()).throw(AssertionError("network"))
        self.assertEqual(tc.profiles(["8:orgid:a"]), {"8:orgid:a": "Alice"})
        self.assertEqual(len(calls), 1)

    def test_stale_entry_refetched(self):
        tc._write_cache(tc.PROFILES_CACHE, {"names": {"8:orgid:a": {"name": "Old", "at": 0}}})
        tc.mt_call = lambda path, body: {"value": [{"mri": "8:orgid:a", "displayName": "New"}]}
        self.assertEqual(tc.profiles(["8:orgid:a"]), {"8:orgid:a": "New"})

    def test_ignores_non_orgid(self):
        tc.mt_call = lambda path, body: (_ for _ in ()).throw(AssertionError("network"))
        self.assertEqual(tc.profiles(["28:x", ""]), {})


class Clear(TempState):
    def test_clear_cache(self):
        tc.cache_messages("19:x", [{"id": "1"}])
        tc.clear_cache()
        self.assertFalse(os.path.exists(tc.CACHE_DIR))


class ListChats(TempState):
    def test_caches_ids_and_titles(self):
        convs = [c for c in fixture("conversations.json")["conversations"]
                 if c["id"] != "48:notifications"]
        one = next(c for c in convs if c["id"].endswith("@unq.gbl.spaces"))
        tc.update_chatinfo(names={one["id"]: "Alice"})
        tc.chat_get = lambda path: {"conversations": convs}
        tc.my_oid = lambda: "8:orgid:me"
        tc.chat_state = lambda c, me: (c.get("lastMessage") or {}, 0, False)
        rows = tc.list_chats()
        self.assertEqual(len(rows), len(convs))
        self.assertEqual(tc.chatinfo()["chats"], [c["id"] for c in convs])
        self.assertEqual(tc.chatinfo()["titles"][one["id"]], "[1:1] Alice")


if __name__ == "__main__":
    unittest.main()
