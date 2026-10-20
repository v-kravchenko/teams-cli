import unittest

from support import TempState, tc


class Search(TempState):
    def test_parsing(self):
        payload = {"EntitySets": [{"ResultSets": [{
            "Total": 1,
            "Results": [{
                "Source": {
                    "TeamName": "Team",
                    "ChannelName": "Channel",
                    "ClientConversationId": "19:x@thread.tacv2;messageid=123",
                    "DateTimeSent": "2024-01-01T00:00:00Z",
                    "From": {"EmailAddress": {"Name": "Alice"}},
                    "Preview": "hello",
                }
            }],
        }]}]}
        tc.http = lambda *a, **kw: (200, payload)
        tc.resource_token = lambda resource, key: "token"
        res = tc.search_messages("hello", n=5)
        self.assertEqual(res["total"], 1)
        row = res["results"][0]
        self.assertEqual(row["chat_id"], "19:x@thread.tacv2")
        self.assertEqual(row["thread_id"], "123")
        self.assertEqual(row["chat"], "Team / Channel")
        self.assertEqual(row["from"], "Alice")
        self.assertEqual(row["text"], "hello")
        self.assertEqual(row["id"], "1704067200000")

    def test_no_thread_for_chats(self):
        payload = {"EntitySets": [{"ResultSets": [{"Total": 1, "Results": [{
            "Source": {"Subject": "Chat", "ClientConversationId": "19:chat@thread.v2",
                       "DateTimeSent": "2024-01-01T00:00:00Z"}}]}]}]}
        tc.http = lambda *a, **kw: (200, payload)
        tc.resource_token = lambda resource, key: "token"
        self.assertIsNone(tc.search_messages("x")["results"][0]["thread_id"])


if __name__ == "__main__":
    unittest.main()
