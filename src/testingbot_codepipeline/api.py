"""Minimal TestingBot REST API client. Standard library only, so it runs on a
stock CodeBuild image and in the Lambda Python runtime without a layer."""

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_API_BASE = "https://api.testingbot.com/v1"
PAGE_SIZE = 500  # the API's maximum ?count=
USER_AGENT = "testingbot-codepipeline"


class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


class BuildNotFound(ApiError):
    pass


class Client:
    def __init__(self, key, secret, api_base=None, timeout=30):
        if not key or not secret:
            raise AuthError("TestingBot key and secret are required.")
        token = base64.b64encode(f"{key}:{secret}".encode()).decode()
        self._auth = f"Basic {token}"
        # API_BASE exists so the tests can point at a stub; it is not a
        # documented user-facing setting.
        self.api_base = (api_base or os.environ.get("TESTINGBOT_API_BASE") or DEFAULT_API_BASE).rstrip("/")
        self.timeout = timeout

    def __repr__(self):
        # Never let the credentials end up in a traceback or a debug print.
        return f"<Client {self.api_base}>"

    def _get(self, path, query):
        url = f"{self.api_base}{path}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(url, headers={
            "Authorization": self._auth,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            if e.code == 401:
                raise AuthError("TestingBot rejected the credentials. Check TESTINGBOT_KEY and TESTINGBOT_SECRET.") from None
            if e.code == 404:
                raise BuildNotFound(body) from None
            raise ApiError(f"TestingBot API returned HTTP {e.code}: {body}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"Could not reach the TestingBot API at {self.api_base}: {e.reason}") from None

    def build_tests(self, identifier):
        """Every test in the build, following pagination until meta.total."""
        path = "/builds/" + urllib.parse.quote(identifier, safe="")
        tests, offset = [], 0
        while True:
            try:
                page = self._get(path, {"offset": offset, "count": PAGE_SIZE, "skip_fields": "logs,thumbs"})
            except BuildNotFound:
                raise BuildNotFound(
                    f"No TestingBot build named '{identifier}'. "
                    "Pass the same value as the 'build' capability in your tests."
                ) from None
            data = page.get("data") or []
            tests.extend(data)
            total = (page.get("meta") or {}).get("total") or 0
            offset += len(data)
            if not data or offset >= total:
                return tests


def test_url(test):
    return f"https://testingbot.com/members/tests/{test['id']}"


def browser_label(test):
    # browser usually carries the version already ("Chrome 150", "IE9"), so
    # browser_version is only appended when it is not in there. Both are
    # stringified: the API spec declares browser_version a number, the API
    # returns a string.
    browser = str(test.get("browser") or "?")
    version = "" if test.get("browser_version") is None else str(test.get("browser_version"))
    if version and version not in browser:
        browser = f"{browser} {version}"
    return f"{browser} on {test.get('os') or '?'}"


def classify(test):
    """passed | failed | unknown | running.

    A test whose status was never reported is `unknown` (success == 2), not
    failed: many suites never call the status API, and treating that as a
    failure would turn every such build red."""
    if test.get("state") and test.get("state") != "COMPLETE":
        return "running"
    if test.get("unknown"):
        return "unknown"
    return "passed" if test.get("success") else "failed"


def summarize(tests):
    counts = {"passed": 0, "failed": 0, "unknown": 0, "running": 0}
    for t in tests:
        counts[classify(t)] += 1
    counts["total"] = len(tests)
    counts["duration"] = sum(int(t.get("duration") or 0) for t in tests)
    return counts
