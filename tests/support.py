"""Shared test setup: load the single-file CLI as a module and isolate its state/cache."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("teams_cli", os.path.join(ROOT, "teams_cli.py"))
tc = importlib.util.module_from_spec(_spec)
sys.modules["teams_cli"] = tc
_spec.loader.exec_module(tc)


def fixture(name):
    with open(os.path.join(ROOT, "tests", "fixtures", name), encoding="utf-8") as f:
        return json.load(f)


class NoNetwork(unittest.TestCase):
    """Fail loudly instead of touching the real Teams API."""

    def setUp(self):
        self._http = tc.http
        tc.http = lambda *a, **kw: (_ for _ in ()).throw(AssertionError("network call"))

    def tearDown(self):
        tc.http = self._http


class TempState(NoNetwork):
    """Redirect state and cache paths into a temporary directory; no keyring, no network."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        names = ("STATE_DIR", "STATE", "CACHE_DIR", "CONV_CACHE", "CHATINFO_CACHE",
                 "PROFILES_CACHE", "MESSAGES_DIR", "TEAMS_CACHE")
        self._old = {n: getattr(tc, n) for n in names}
        d = self.tmp.name
        tc.STATE_DIR = os.path.join(d, "config")
        tc.STATE = os.path.join(tc.STATE_DIR, "state.json")
        tc.CACHE_DIR = os.path.join(d, "cache")
        tc.CONV_CACHE = os.path.join(tc.CACHE_DIR, "conversations.json")
        tc.CHATINFO_CACHE = os.path.join(tc.CACHE_DIR, "chatinfo.json")
        tc.PROFILES_CACHE = os.path.join(tc.CACHE_DIR, "profiles.json")
        tc.MESSAGES_DIR = os.path.join(tc.CACHE_DIR, "messages")
        tc.TEAMS_CACHE = os.path.join(tc.CACHE_DIR, "teams.json")
        self._keyring = (tc.keyring_get, tc.keyring_put, tc.keyring_clear)
        tc.keyring_get = lambda name: None
        tc.keyring_put = lambda name, value: True
        tc.keyring_clear = lambda name: None
        tc._cache = None
        tc._warned = False
        for name in ("iter_conversations", "iter_msgs", "chat_get", "chat_call", "my_oid", "chatinfo",
                     "mt_call", "resolve", "resolve_write", "people", "self_name", "ams_headers",
                     "resource_token", "csa_get", "all_conversations"):
            self.addCleanup(setattr, tc, name, getattr(tc, name))

    def tearDown(self):
        tc.keyring_get, tc.keyring_put, tc.keyring_clear = self._keyring
        for name, value in self._old.items():
            setattr(tc, name, value)
        tc._cache = None
        tc._warned = False
        super().tearDown()
