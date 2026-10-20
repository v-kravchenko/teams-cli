import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from support import TempState, tc


class Cli(TempState):
    def run_main(self, argv):
        old = sys.argv
        sys.argv = ["teams_cli.py"] + argv
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                tc.main()
        finally:
            sys.argv = old
        return out.getvalue(), err.getvalue()

    def test_error_is_json_with_exit_1(self):
        self._chatinfo = tc.chatinfo
        tc.chatinfo = lambda: {"chats": [], "names": {}, "titles": {}}
        try:
            with self.assertRaises(SystemExit) as cm:
                self.run_main(["read", "99", "--json"])
            self.assertEqual(cm.exception.code, 1)
        finally:
            tc.chatinfo = self._chatinfo

    def test_error_output_is_sanitized(self):
        self._read, self._resolve = tc.read_messages, tc.resolve
        tc.resolve = lambda sel: sel
        tc.read_messages = lambda *a: (_ for _ in ()).throw(tc.TeamsError("bad \x1b]0;x\x07"))
        try:
            with self.assertRaises(SystemExit) as cm:
                self.run_main(["read", "19:x"])
        finally:
            tc.read_messages, tc.resolve = self._read, self._resolve
        self.assertNotIn("\x1b", str(cm.exception.code))

    def test_login_prints_url_without_prompting(self):
        import builtins
        old_input = builtins.input
        builtins.input = lambda *a: (_ for _ in ()).throw(AssertionError("interactive input"))
        try:
            out, err = self.run_main(["login"])
        finally:
            builtins.input = old_input
        self.assertIn("https://login.microsoftonline.com/organizations/oauth2/authorize", out)
        self.assertIn("login --url", err)
        self.assertEqual(out.count("\n"), 1)

    def test_login_from_stdin(self):
        saved = []
        self._token_request, self._session, self._save = tc.token_request, tc.session, tc.save
        tc.token_request = lambda form: {"refresh_token": "r", "access_token": "a", "expires_in": 3600}
        tc.session = lambda: ("sk", "host")
        tc.save = lambda st: saved.append(dict(st))
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("https://x/?code=abc%2Fdef\n")
        try:
            self.run_main(["login", "--url", "-"])
        finally:
            sys.stdin = old_stdin
            tc.token_request, tc.session, tc.save = self._token_request, self._session, self._save
        self.assertEqual(saved[0]["refresh_token"], "r")
        self.assertEqual(saved[0]["access_token"], "a")

    def test_login_bad_url(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main(["login", "--url", "nope"])
        self.assertIn("no code=", str(cm.exception.code))

    def test_send_dry_run(self):
        self._resolve, self._chatinfo = tc.resolve_write, tc.chatinfo
        tc.resolve_write = lambda sel: sel
        tc.chatinfo = lambda: {"chats": [], "names": {}, "titles": {"19:x": "X"}}
        try:
            out, _ = self.run_main(["send", "19:x", "hi", "--json"])
        finally:
            tc.resolve_write, tc.chatinfo = self._resolve, self._chatinfo
        self.assertTrue(json.loads(out)["dry_run"])

    def test_logout_clears_tokens_and_cache(self):
        cleared = []
        self._keyring_clear = tc.keyring_clear
        tc.keyring_clear = cleared.append
        os.makedirs(tc.STATE_DIR, mode=0o700, exist_ok=True)
        with open(tc.STATE, "w") as f:
            json.dump({"refresh_token": "secret", "endpoint": "e"}, f)
        tc._write_cache(tc.CHATINFO_CACHE, {"chats": []})
        tc._cache = None
        try:
            self.run_main(["logout"])
        finally:
            tc.keyring_clear = self._keyring_clear
        self.assertEqual(cleared, list(tc.SECRET_KEYS))
        with open(tc.STATE) as f:
            self.assertEqual(json.load(f), {"endpoint": "e"})
        self.assertFalse(os.path.exists(tc.CACHE_DIR))

    def test_cache_clear(self):
        tc._write_cache(tc.CHATINFO_CACHE, {"chats": []})
        self.run_main(["cache", "clear"])
        self.assertFalse(os.path.exists(tc.CACHE_DIR))


if __name__ == "__main__":
    unittest.main()
