"""A stand-in TestingBot API on a random local port.

The fixture is shaped after a real /v1/builds/:id response, which differs from
the OpenAPI spec: browser_version is a string (spec: integer), browser already
carries the version ("Chrome 140"), status_message is null rather than "" when
unset, and state is START until the WebDriver session ends."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

KEY, SECRET = "stub-key", "stub-secret"


def fixture():
    return [
        {"id": 101, "name": "login spec", "success": True, "unknown": False, "state": "COMPLETE", "duration": 12,
         "browser": "Chrome 140", "browser_version": "140", "os": "WIN11", "status_message": "",
         "video": "https://example.test/101.mp4"},
        {"id": 102, "name": "checkout spec", "success": False, "unknown": False, "state": "COMPLETE", "duration": 30,
         "browser": "Safari 17", "browser_version": "17", "os": "SONOMA",
         "status_message": "expected 'Welcome' but got 'Error' <&>"},
        {"id": 103, "name": "flaky spec", "success": False, "unknown": True, "state": "COMPLETE", "duration": 5,
         "browser": "Firefox 131", "browser_version": "131", "os": "WIN11", "status_message": None, "video": False},
    ]


class Stub:
    def __init__(self):
        self.builds = {"green": [fixture()[0]], "mixed": fixture(), "empty": []}
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                stub.requests.append(self.path)
                expected = "Basic " + base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    return self._send(401, {"error": "unauthorized"})
                url = urlparse(self.path)
                if not url.path.startswith("/v1/builds/"):
                    return self._send(404, {"error": "nope"})
                name = url.path[len("/v1/builds/"):]
                tests = stub.builds.get(unquote(name))
                if tests is None:
                    return self._send(404, {"error": "404 Not Found. Could not find build"})
                q = parse_qs(url.query)
                offset, count = int(q.get("offset", ["0"])[0]), int(q.get("count", ["10"])[0])
                self._send(200, {"data": tests[offset:offset + count],
                                 "meta": {"offset": offset, "count": count, "total": len(tests)}})

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
