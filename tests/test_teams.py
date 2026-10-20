import io
import json
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace

from support import NoNetwork, TempState, fixture, tc

PINNED = ["19:zz@thread.tacv2"]


class CsaTree(NoNetwork):
    def setUp(self):
        super().setUp()
        self.tree = tc.csa_tree(fixture("csa_teams.json"), PINNED)

    def test_teams_sorted_archived_last_deleted_dropped(self):
        self.assertEqual([t["name"] for t in self.tree], ["Alpha", "beta team", "Zeta (old)"])
        self.assertEqual([t["id"] for t in self.tree], ["g-a", "g-b", "g-z"])
        self.assertTrue(self.tree[-1]["archived"])
        self.assertEqual(self.tree[0]["members"], 95)
        self.assertTrue(self.tree[0]["favorite"])

    def test_channel_order_general_pinned_name(self):
        names = [c["name"] for c in self.tree[0]["channels"]]
        self.assertEqual(names, ["General", "Zebra", "news", "Secret"])  # joined, not deleted

    def test_channel_flags(self):
        ch = {c["name"]: c for c in self.tree[0]["channels"]}
        self.assertEqual(ch["General"]["kind"], "general")
        self.assertEqual(ch["Secret"]["kind"], "private")
        self.assertEqual(ch["news"]["kind"], "channel")
        self.assertTrue(ch["news"]["unread"] and ch["news"]["muted"])
        self.assertTrue(ch["Zebra"]["pinned"])
        self.assertFalse(ch["General"]["unread"])

    def test_missing_read_flag_is_not_unread(self):
        self.assertFalse(self.tree[-1]["channels"][0]["unread"])


class TeamsTree(TempState):
    def setUp(self):
        super().setUp()
        self.calls = []

        def csa_get(path):
            self.calls.append(path)
            return {"pinChannelOrder": PINNED} if path == "/pinnedChannels" else fixture("csa_teams.json")
        tc.csa_get = csa_get

    def test_fetch_then_cache(self):
        tree = tc.teams_tree()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(tc.teams_tree(), tree)
        self.assertEqual(len(self.calls), 2)
        tc.teams_tree(refresh=True)
        self.assertEqual(len(self.calls), 4)

    def test_stale_cache_refetches(self):
        tc._write_cache(tc.TEAMS_CACHE, {"fetched_at": time.time() - 901, "teams": [{"id": "old"}]})
        self.assertEqual(tc.teams_tree()[0]["id"], "g-a")

    def test_pinned_failure_is_not_fatal(self):
        def csa_get(path):
            if path == "/pinnedChannels":
                raise tc.TeamsError("HTTP 404")
            return fixture("csa_teams.json")
        tc.csa_get = csa_get
        names = [c["name"] for c in tc.teams_tree()[0]["channels"]]
        self.assertEqual(names, ["General", "news", "Secret", "Zebra"])

    def test_falls_back_to_conversation_list(self):
        def csa_get(path):
            raise tc.TeamsError("HTTP 401")
        tc.csa_get = csa_get
        tc.all_conversations = lambda ttl, refresh=False, progress=None: fixture("conversations.json")["conversations"]
        err = io.StringIO()
        with redirect_stderr(err):
            tree = tc.teams_tree()
        self.assertEqual([t["name"] for t in tree], ["Team A", "Team B"])
        self.assertIn("aggregator failed", err.getvalue())
        self.assertIsNone(tc._read_cache(tc.TEAMS_CACHE))  # the fallback is not cached as the CSA tree

    def test_text_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tc.cmd_teams(SimpleNamespace(refresh=False, json=False))
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "Alpha  (95 members)")
        self.assertIn("  • news  (muted)", lines)
        self.assertIn("    Zebra  (pinned)", lines)
        self.assertIn("    Secret  (private)", lines)
        self.assertIn("Zeta (old)  (archived)", lines)

    def test_json_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tc.cmd_teams(SimpleNamespace(refresh=False, json=True))
        self.assertEqual(json.loads(out.getvalue())[0]["channels"][0]["kind"], "general")


class CsaToken(TempState):
    def test_csa_token_is_a_secret(self):
        self.assertIn("csa_token", tc.SECRET_KEYS)


if __name__ == "__main__":
    unittest.main()
