import unittest

from support import tc


class Resolve(unittest.TestCase):
    def setUp(self):
        self._chatinfo = tc.chatinfo
        tc.chatinfo = lambda: {"chats": ["19:a", "19:b", "19:abc"], "names": {}, "titles": {}}

    def tearDown(self):
        tc.chatinfo = self._chatinfo

    def test_number(self):
        self.assertEqual(tc.resolve("0"), "19:a")
        self.assertEqual(tc.resolve("2"), "19:abc")

    def test_full_id(self):
        self.assertEqual(tc.resolve("19:other"), "19:other")

    def test_unique_fragment(self):
        self.assertEqual(tc.resolve("bc"), "19:abc")

    def test_ambiguous(self):
        with self.assertRaises(tc.TeamsError):
            tc.resolve("b")  # 19:b and 19:abc both contain "b"

    def test_unknown_and_out_of_range(self):
        for sel in ("nope", "9"):
            with self.assertRaises(tc.TeamsError):
                tc.resolve(sel)


class ResolveWrite(unittest.TestCase):
    def test_write_needs_full_id(self):
        for sel in ("0", "abc", "19"):
            with self.assertRaises(tc.TeamsError):
                tc.resolve_write(sel)
        self.assertEqual(tc.resolve_write("19:abc"), "19:abc")


class Ids(unittest.TestCase):
    def setUp(self):
        self._my_oid = tc.my_oid
        tc.my_oid = lambda: "b"

    def tearDown(self):
        tc.my_oid = self._my_oid

    def test_mri_of(self):
        self.assertEqual(tc.mri_of("me"), "8:orgid:b")
        self.assertEqual(tc.mri_of("oid"), "8:orgid:oid")
        self.assertEqual(tc.mri_of("8:orgid:x"), "8:orgid:x")
        self.assertEqual(tc.mri_of("28:x"), "28:x")

    def test_one_to_one_id_is_sorted(self):
        self.assertEqual(tc.one_to_one_id("8:orgid:a"), "19:a_b@unq.gbl.spaces")


if __name__ == "__main__":
    unittest.main()
