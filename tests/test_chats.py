import unittest

from support import NoNetwork, fixture, tc

ME = "8:orgid:me"


def convs():
    return {c["id"]: c for c in fixture("conversations.json")["conversations"]}


class ChatKind(NoNetwork):
    def test_kinds(self):
        c = convs()
        self.assertEqual(tc.chat_kind(c["48:notes"]), "self")
        self.assertEqual(tc.chat_kind(c["19:aaa_bbb@unq.gbl.spaces"]), "1:1")
        self.assertEqual(tc.chat_kind(c["19:group@thread.v2"]), "group")
        self.assertEqual(tc.chat_kind(c["19:general-a@thread.tacv2"]), "channel")
        self.assertEqual(tc.chat_kind(c["19:secret-a@thread.tacv2"]), "channel")


class Horizon(NoNetwork):
    def test_horizon(self):
        c = convs()
        self.assertEqual(tc.horizon_of(c["19:general-a@thread.tacv2"]), 1700000005003)
        self.assertEqual(tc.horizon_of(c["19:group@thread.v2"]), 0)
        self.assertEqual(tc.horizon_of({"properties": {"consumptionhorizon": "junk"}}), 0)


class ChatRow(NoNetwork):
    def test_preview_and_unread_unknown(self):
        c = convs()["19:aaa_bbb@unq.gbl.spaces"]
        row = tc.chat_row(c, {c["id"]: "Alice"}, ME)
        self.assertEqual(row["preview"], "Alice: hi there")
        self.assertIsNone(row["unread"])  # no consumptionhorizon in the list payload
        self.assertIsNone(row["read_up_to"])
        self.assertEqual(row["kind"], "1:1")
        self.assertEqual(row["title"], "[1:1] Alice")

    def test_unread_true_and_false(self):
        c = convs()["19:general-a@thread.tacv2"]
        self.assertIs(tc.chat_row(c, {}, ME)["unread"], True)
        mine = {"id": "1700000005008", "from": "https://api.spaces.skype.com/v1/users/" + ME,
                "imdisplayname": "Me", "content": "x", "messagetype": "Text"}
        c2 = {**c, "lastMessage": mine}
        self.assertIs(tc.chat_row(c2, {}, ME)["unread"], False)

    def test_non_text_preview(self):
        row = tc.chat_row(convs()["19:group@thread.v2"], {}, ME)
        self.assertEqual(row["preview"], "Bob: <Event/Call>")

    def test_number_and_cap(self):
        row = tc.chat_row(convs()["48:notes"], {}, ME, unread=5, n=3, unread_capped=True)
        self.assertEqual(row["n"], 3)
        self.assertEqual(row["unread"], 5)
        self.assertTrue(row["unread_capped"])
        self.assertEqual(row["title"], "Notes (me)")


class ChannelTree(NoNetwork):
    def test_tree(self):
        tree = tc.channel_tree(list(convs().values()))
        self.assertEqual([t["name"] for t in tree], ["Team A", "Team B"])
        team_a = tree[0]
        self.assertEqual([c["name"] for c in team_a["channels"]], ["General", "News", "Secret"])
        self.assertEqual([c["kind"] for c in team_a["channels"]], ["general", "channel", "private"])
        self.assertEqual(len(tree[1]["channels"]), 1)
        self.assertEqual(tree[1]["channels"][0]["kind"], "general")

    def test_non_channel_conversations_are_skipped(self):
        tree = tc.channel_tree(list(convs().values()))
        ids = [c["id"] for t in tree for c in t["channels"]]
        self.assertNotIn("48:notes", ids)
        self.assertNotIn("19:group@thread.v2", ids)


class ChatTitle(NoNetwork):
    def test_titles(self):
        c = convs()
        self.assertEqual(tc.chat_title(c["48:notes"], {}), "Notes (me)")
        self.assertEqual(tc.chat_title(c["19:general-a@thread.tacv2"], {}), "General")
        one = c["19:aaa_bbb@unq.gbl.spaces"]
        self.assertEqual(tc.chat_title(one, {one["id"]: "Alice"}), "[1:1] Alice")
        self.assertEqual(tc.chat_title(one, {}, fetch=False), "[chat]")
        self.assertEqual(tc.chat_title(c["19:group@thread.v2"], {}, fetch=False), "[chat]")


if __name__ == "__main__":
    unittest.main()
