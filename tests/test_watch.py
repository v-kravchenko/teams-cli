import base64
import gzip
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from support import TempState, tc


def message(mid="1700000001000", cid="19:x", **kw):
    res = {"id": mid, "version": "1", "conversationid": cid, "messagetype": "Text",
           "content": "hello", "from": "https://api.spaces.skype.com/v1/users/8:orgid:a",
           "imdisplayname": "Alice", "composetime": "2024-01-01T10:00:00.000Z"}
    res.update(kw)
    return res


class Decode(unittest.TestCase):
    def test_plain(self):
        req = {"body": json.dumps({"type": "EventMessage"}), "headers": {}}
        self.assertEqual(tc.decode_event(req), {"type": "EventMessage"})

    def test_gzip_header(self):
        body = base64.b64encode(gzip.compress(b'{"a": 1}')).decode()
        req = {"body": body, "headers": {"X-Microsoft-Skype-Content-Encoding": "gzip"}}
        self.assertEqual(tc.decode_event(req), {"a": 1})

    def test_cp_wrapper(self):
        inner = base64.b64encode(gzip.compress(b'{"a": 2}')).decode()
        self.assertEqual(tc.decode_event({"body": json.dumps({"cp": inner}), "headers": {}}), {"a": 2})

    def test_gp_wrapper(self):
        inner = base64.b64encode(b'{"a": 3}').decode()
        self.assertEqual(tc.decode_event({"body": json.dumps({"gp": inner}), "headers": {}}), {"a": 3})

    def test_empty(self):
        self.assertEqual(tc.decode_event({"body": "", "headers": {}}), {})


class Emit(TempState):
    def setUp(self):
        super().setUp()
        self._chatinfo = tc.chatinfo
        tc.chatinfo = lambda: {"chats": [], "names": {}, "titles": {"19:x": "Chat X"}}

    def tearDown(self):
        tc.chatinfo = self._chatinfo
        super().tearDown()

    def test_message(self):
        ev = tc.emit(message(), "NewMessage", set(), set())
        self.assertEqual(ev["event"], "message")
        self.assertEqual(ev["chat"], "Chat X")
        self.assertEqual(ev["text"], "hello")
        self.assertEqual(ev["from_id"], "8:orgid:a")

    def test_filter_and_dedup(self):
        ev = message()
        self.assertIsNone(tc.emit(ev, "NewMessage", {"19:other"}, set()))
        seen = set()
        self.assertIsNotNone(tc.emit(ev, "NewMessage", set(), seen))
        self.assertIsNone(tc.emit(ev, "NewMessage", set(), seen))
        self.assertIsNotNone(tc.emit({**ev, "version": "2"}, "MessageUpdate", set(), seen))

    def test_typing(self):
        typing = tc.emit(message(messagetype="Control/Typing"), "NewMessage", set(), set())
        self.assertEqual(typing["event"], "typing")
        stop = tc.emit(message(messagetype="Control/ClearTyping"), "NewMessage", set(), set())
        self.assertEqual(stop["event"], "typing_stop")

    def test_system_and_deleted(self):
        system = tc.emit(message(messagetype="Event/Call"), "NewMessage", set(), set())
        self.assertEqual(system["event"], "system")
        deleted = tc.emit(message(properties={"deletetime": "2024-01-01T10:06:00.000Z"}),
                          "MessageUpdate", set(), set())
        self.assertEqual(deleted["event"], "message_deleted")
        edited = tc.emit(message(), "MessageUpdate", set(), set())
        self.assertEqual(edited["event"], "message_updated")

    def test_updates_existing_cache(self):
        tc.cache_messages("19:x", [tc.msg_json(message("1700000000001"))])
        tc.emit(message("1700000000003"), "NewMessage", set(), set())
        cached = tc.cached_messages("19:x")
        self.assertEqual([m["id"] for m in cached["messages"]], ["1700000000001", "1700000000003"])

    def test_does_not_create_cache(self):
        tc.emit(message("1700000000003"), "NewMessage", set(), set())
        self.assertIsNone(tc.cached_messages("19:x"))


class EmitRead(unittest.TestCase):
    def test_read_event(self):
        res = {"id": "19:x", "properties": {"consumptionhorizon": "1700000001000;1;1"}}
        horizons = {}
        self.assertEqual(tc.emit_read(res, set(), horizons),
                         {"event": "read", "chat_id": "19:x", "read_up_to": "1700000001000"})
        self.assertIsNone(tc.emit_read(res, set(), horizons))
        self.assertIsNone(tc.emit_read(res, {"19:other"}, {}))

    def test_no_horizon(self):
        self.assertIsNone(tc.emit_read({"id": "19:x", "properties": {}}, set(), {}))


class CliEvent(unittest.TestCase):
    def test_json_line(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tc.cli_event(True)({"event": "message", "chat": "X", "from": "A", "text": "hi"})
        self.assertEqual(json.loads(out.getvalue())["event"], "message")

    def test_status_goes_to_stderr(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            tc.cli_event(False)(tc.status("connected"))
        self.assertEqual(out.getvalue(), "")
        self.assertIn("[connected]", err.getvalue())

    def test_text_is_sanitized(self):
        out = io.StringIO()
        ev = {"event": "message", "chat": "X\x1b]0;pwn\x07", "from": "A",
              "text": "hi\x1b[2J", "type": "Text", "time": "2024-01-01T10:00:00Z"}
        with redirect_stdout(out):
            tc.cli_event(False)(ev)
        self.assertNotIn("\x1b", out.getvalue())
        self.assertIn("hi", out.getvalue())


if __name__ == "__main__":
    unittest.main()
