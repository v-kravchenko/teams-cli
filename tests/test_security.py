import json
import os
import stat
import unittest

from support import TempState, tc


class Clean(unittest.TestCase):
    def test_strips_control_characters(self):
        raw = "a\x1b]0;title\x07b\x00c\x9bd\ne\tf"
        self.assertEqual(tc.clean(raw), "a]0;titlebcd\ne\tf")

    def test_none_and_numbers(self):
        self.assertEqual(tc.clean(None), "")
        self.assertEqual(tc.clean(42), "42")

    def test_ts_is_clean(self):
        self.assertEqual(tc.ts("2024-01-01T10:00:00Z\x1b"), "2024-01-01 10:00:00")


class AmsUrl(unittest.TestCase):
    def test_good(self):
        self.assertTrue(tc.ams_url_ok("https://eu-api.asm.skype.com/v1/objects/x/views/imgo"))
        self.assertTrue(tc.ams_url_ok("https://asm.skype.com/x"))

    def test_bad(self):
        for url in ("http://eu-api.asm.skype.com/x", "https://evil.com/?asm.skype.com",
                    "https://asm.skype.com.evil.com/x", "https://evilasm.skype.com/x", "",
                    "not a url"):
            self.assertFalse(tc.ams_url_ok(url), url)


class Download(TempState):
    def test_non_ams_image_does_not_get_the_token(self):
        msg = {"id": "1", "messagetype": "RichText/Html",
               "content": '<img src="https://evil.example/?asm.skype.com" '
                          'itemtype="http://schema.skype.com/AMSImage">'}
        tc.iter_msgs = lambda cid, **kw: iter([msg])
        tc.ams_headers = lambda extra=None: (_ for _ in ()).throw(AssertionError("token sent"))
        saved = tc.download_attachments("19:x", "1", self.tmp.name)
        self.assertEqual(saved[0]["kind"], "image")
        self.assertIn("not an AMS image", saved[0]["note"])

    def test_downloaded_image_is_0600(self):
        msg = {"id": "1", "messagetype": "RichText/Html",
               "content": '<img src="https://eu-api.asm.skype.com/x" '
                          'itemtype="http://schema.skype.com/AMSImage">'}

        class Response:
            headers = {"Content-Type": "image/png"}

            def read(self):
                return b"png"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        tc.iter_msgs = lambda cid, **kw: iter([msg])
        tc.ams_headers = lambda extra=None: {"Authorization": "skype_token x"}
        old = tc.urllib.request.urlopen
        tc.urllib.request.urlopen = lambda req, timeout=60: Response()
        try:
            saved = tc.download_attachments("19:x", "1", self.tmp.name)
        finally:
            tc.urllib.request.urlopen = old
        path = saved[0]["path"]
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_mid_cannot_escape_the_output_directory(self):
        tc.iter_msgs = lambda cid, **kw: iter([])
        with self.assertRaises(tc.TeamsError):
            tc.download_attachments("19:x", "../../etc/passwd", self.tmp.name)


class Dump(TempState):
    def test_dump_is_0600(self):
        path = os.path.join(self.tmp.name, "dump.jsonl")
        old = os.environ.get("TEAMS_DUMP")
        os.environ["TEAMS_DUMP"] = path
        try:
            tc.dump_event({"type": "EventMessage", "text": "secret"})
        finally:
            if old is None:
                os.environ.pop("TEAMS_DUMP", None)
            else:
                os.environ["TEAMS_DUMP"] = old
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(json.loads(f.read())["text"], "secret")


class NoBrowser(unittest.TestCase):
    def test_webbrowser_is_not_imported(self):
        self.assertFalse(hasattr(tc, "webbrowser"))


if __name__ == "__main__":
    unittest.main()
