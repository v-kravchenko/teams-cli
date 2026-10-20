"""Read-only checks against the real account; skipped unless TEAMS_LIVE=1.

    TEAMS_LIVE=1 python3 -m unittest discover -s tests -p 'test_live.py' -v
"""
import os
import unittest

from support import tc


@unittest.skipUnless(os.environ.get("TEAMS_LIVE") == "1", "set TEAMS_LIVE=1 to run read-only live checks")
class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chats = tc.list_chats()

    def test_chats(self):
        self.assertTrue(self.chats)
        self.assertIn("id", self.chats[0])
        self.assertIn("title", self.chats[0])

    def test_teams_tree(self):
        tree = tc.channel_tree(tc.all_conversations(86400))
        self.assertTrue(tree)
        self.assertTrue(all(t["channels"] for t in tree))

    def test_read_recent(self):
        rows = tc.read_messages(self.chats[0]["id"], 1)
        self.assertIsInstance(rows, list)


if __name__ == "__main__":
    unittest.main()
