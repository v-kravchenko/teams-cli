import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from support import tc


class PrepareSend(unittest.TestCase):
    def setUp(self):
        self._people = tc.people
        tc.people = lambda cid: {"8:orgid:a": "Alice", "8:orgid:b": "Alina", "8:orgid:c": "Bob"}
        self._iter = tc.iter_msgs

    def tearDown(self):
        tc.people = self._people
        tc.iter_msgs = self._iter

    def test_nothing_to_send(self):
        with self.assertRaises(tc.TeamsError):
            tc.prepare_send("19:x")

    def test_bad_image(self):
        with self.assertRaises(tc.TeamsError):
            tc.prepare_send("19:x", image="a.txt")
        with self.assertRaises(tc.TeamsError):
            tc.prepare_send("19:x", image="/no/such/file.png")

    def test_mention(self):
        content, mentions = tc.build_mentions("19:x", "hi @Alice", ["Alice"])
        self.assertIn('itemid="0"', content)
        self.assertIn(">Alice</span>", content)
        self.assertEqual(mentions[0]["mri"], "8:orgid:a")
        self.assertEqual(mentions[0]["displayName"], "Alice")

    def test_mention_errors(self):
        with self.assertRaises(tc.TeamsError):
            tc.build_mentions("19:x", "hi @Ali", ["Ali"])  # ambiguous
        with self.assertRaises(tc.TeamsError):
            tc.build_mentions("19:x", "hi @Nobody", ["Nobody"])  # not seen
        with self.assertRaises(tc.TeamsError):
            tc.build_mentions("19:x", "hi there", ["Alice"])  # @Alice missing

    def test_no_mention_queries_do_not_fetch(self):
        tc.people = lambda cid: (_ for _ in ()).throw(AssertionError("network call"))
        content, mentions = tc.build_mentions("19:x", "<hi>\n", [])
        self.assertEqual(content, "&lt;hi&gt;<br>")
        self.assertEqual(mentions, [])

    def test_reply_quote(self):
        tc.iter_msgs = lambda cid, **kw: iter([{
            "id": "123", "content": "hi <b>x</b>", "imdisplayname": "Alice",
            "from": "https://api.spaces.skype.com/v1/users/8:orgid:a"}])
        p = tc.prepare_send("19:x", "yo", reply_to="123")
        self.assertIn('itemid="123"', p["content"])
        self.assertIn('itemprop="preview">hi x</p>', p["content"])
        self.assertIn("<p>yo</p>", p["content"])

    def test_reply_not_found(self):
        tc.iter_msgs = lambda cid, **kw: iter([])
        with self.assertRaises(tc.TeamsError):
            tc.prepare_send("19:x", "yo", reply_to="123")


class SendPrepared(unittest.TestCase):
    def setUp(self):
        self._chat_call = tc.chat_call
        self._self_name = tc.self_name
        self.calls = []
        tc.self_name = lambda cid: "Me"

    def tearDown(self):
        tc.chat_call = self._chat_call
        tc.self_name = self._self_name

    def test_body(self):
        def fake(method, path, body=None):
            self.calls.append((method, path, body))
            return {"OriginalArrivalTime": 1700000009000}
        tc.chat_call = fake
        p = {"content": "<p>hi</p>", "mentions": [{"displayName": "Alice", "mri": "8:orgid:a"}],
             "reply_to": None, "image": None}
        res = tc.send_prepared("19:x", p, "hi")
        self.assertEqual(res["chat_id"], "19:x")
        self.assertEqual(res["id"], "1700000009000")
        method, path, body = self.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(path.endswith("/v1/users/ME/conversations/19%3Ax/messages"))
        self.assertEqual(body["content"], "<p>hi</p>")
        self.assertEqual(body["messagetype"], "RichText/Html")
        self.assertEqual(json.loads(body["properties"]["mentions"])[0]["mri"], "8:orgid:a")


class DryRun(unittest.TestCase):
    def setUp(self):
        self._resolve = tc.resolve_write
        self._chatinfo = tc.chatinfo
        tc.resolve_write = lambda sel: sel
        tc.chatinfo = lambda: {"chats": [], "names": {}, "titles": {"19:x": "Chat X"}}

    def tearDown(self):
        tc.resolve_write = self._resolve
        tc.chatinfo = self._chatinfo

    def cmd(self, **kw):
        args = {"chat": "19:x", "text": "hi", "reply_to": None, "mention": None,
                "image": None, "yes": False, "json": True}
        args.update(kw)
        out = io.StringIO()
        with redirect_stdout(out):
            tc.cmd_send(SimpleNamespace(**args))
        return out.getvalue()

    def test_send_dry_run_json(self):
        out = json.loads(self.cmd())
        self.assertFalse(out["sent"])
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["chat_id"], "19:x")

    def test_send_dry_run_text(self):
        self.assertIn("DRY RUN", self.cmd(json=False))

    def test_edit_dry_run(self):
        out = io.StringIO()
        a = SimpleNamespace(chat="19:x", id="123", text="new", yes=False, json=False)
        with redirect_stdout(out):
            tc.cmd_edit(a)
        self.assertIn("Would edit message 123", out.getvalue())
        self.assertIn("Chat X", out.getvalue())


if __name__ == "__main__":
    unittest.main()
