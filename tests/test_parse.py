import unittest

from support import fixture, tc


class TextOf(unittest.TestCase):
    def setUp(self):
        self.msgs = {m["id"]: m for m in fixture("messages.json")}
        self.rich = self.msgs["1700000002000"]

    def test_plain_text_and_entities(self):
        m = self.msgs["1700000001000"]
        self.assertEqual(tc.text_of(m["content"], m.get("properties")), "hello & bye")
        self.assertEqual(tc.msg_text(m), "hello & bye")

    def test_reply_image_emoji_files_cards_reactions(self):
        text = tc.text_of(self.rich["content"], self.rich["properties"])
        self.assertIn("> Alice: hello & bye", text)
        self.assertIn("reply [зображення] smile", text)
        self.assertIn("[файл: doc.pdf]", text)
        self.assertIn("[картка: Card]", text)
        self.assertIn("↳ like×2", text)

    def test_props_as_json_strings(self):
        props = {"files": '[{"fileName": "a.txt"}]', "cards": '[{"content": {"title": "T"}}]'}
        text = tc.text_of("x", props)
        self.assertIn("[файл: a.txt]", text)
        self.assertIn("[картка: T]", text)

    def test_bad_props_are_ignored(self):
        self.assertEqual(tc.text_of("x", {"files": "not json"}), "x")


class MsgJson(unittest.TestCase):
    def setUp(self):
        self.msgs = {m["id"]: m for m in fixture("messages.json")}
        self.rich = tc.msg_json(self.msgs["1700000002000"], "19:aaa_bbb@unq.gbl.spaces")

    def test_stable_shape(self):
        self.assertEqual(self.rich["id"], "1700000002000")
        self.assertEqual(self.rich["chat_id"], "19:aaa_bbb@unq.gbl.spaces")
        self.assertEqual(self.rich["from"], "Bob")
        self.assertEqual(self.rich["from_id"], "8:orgid:bbb")
        self.assertEqual(self.rich["type"], "RichText/Html")
        self.assertTrue(self.rich["edited"])
        self.assertFalse(self.rich["deleted"])
        self.assertEqual(self.rich["thread_id"], "1700000001000")
        for key in ("html", "attachments", "mentions", "reactions", "reply_to", "time", "text"):
            self.assertIn(key, self.rich)

    def test_attachments(self):
        kinds = [(a["kind"], a.get("name")) for a in self.rich["attachments"]]
        self.assertIn(("image", None), kinds)
        self.assertIn(("file", "doc.pdf"), kinds)
        self.assertIn(("card", None), kinds)

    def test_reactions(self):
        self.assertEqual(self.rich["reactions"], [
            {"key": "like", "count": 2, "users": ["8:orgid:aaa", "8:orgid:ccc"]}])

    def test_reply_to(self):
        self.assertEqual(self.rich["reply_to"],
                         {"id": "1700000001000", "from_id": "8:orgid:aaa", "preview": "hello & bye"})

    def test_root_has_no_thread(self):
        root = tc.msg_json(self.msgs["1700000004000"])
        self.assertIsNone(root["thread_id"])

    def test_deleted(self):
        deleted = tc.msg_json(self.msgs["1700000003000"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["text"], "")


if __name__ == "__main__":
    unittest.main()
