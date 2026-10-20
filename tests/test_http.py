import unittest
import urllib.error

from support import tc


class Http(unittest.TestCase):
    def setUp(self):
        self._urlopen = tc.urllib.request.urlopen

    def tearDown(self):
        tc.urllib.request.urlopen = self._urlopen

    def test_network_error_becomes_teams_error(self):
        tc.urllib.request.urlopen = lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("down"))
        with self.assertRaises(tc.TeamsError) as cm:
            tc.http("GET", "https://example.com/x", retries=1)
        self.assertIn("URLError", str(cm.exception))

    def test_writes_are_not_retried(self):
        calls = []

        def fail(*a, **kw):
            calls.append(1)
            raise urllib.error.URLError("down")
        tc.urllib.request.urlopen = fail
        with self.assertRaises(tc.TeamsError):
            tc.http("POST", "https://example.com/x", retries=3)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
