"""Exercises install/start/stop against a fake tunnel binary: a Python script
that serves /readyz on --metrics-port after a short delay and records how it
was started, so the real tunnel never has to be downloaded."""

import hashlib
import io
import json
import os
import socket
import stat
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from testingbot_codepipeline import tunnel

FAKE_TUNNEL = r'''#!{python}
import json, os, signal, socketserver, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
args = sys.argv[1:]
port = int(args[args.index("--metrics-port") + 1])
record = os.environ["FAKE_TUNNEL_RECORD"]
mode = os.environ.get("FAKE_TUNNEL_MODE", "ok")
with open(record, "w") as f:
    json.dump({{"args": args, "key": os.environ.get("TESTINGBOT_KEY"), "secret": os.environ.get("TESTINGBOT_SECRET")}}, f)
if mode == "crash":
    print("authentication failed", flush=True)
    sys.exit(3)
def bye(*_):
    open(record + ".stopped", "w").close()
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
if mode == "never":
    while True: time.sleep(1)
time.sleep(0.3)
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/readyz" else 404); self.end_headers()
    def log_message(self, *a): pass
class S(HTTPServer):
    # HTTPServer.server_bind resolves socket.getfqdn(), which takes ~30s on
    # the macOS CI runners while the port already accepts connections.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]
S(("127.0.0.1", port), H).serve_forever()
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TunnelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.binary = os.path.join(self.tmp, "testingbot-tunnel")
        with open(self.binary, "w") as f:
            f.write(FAKE_TUNNEL.format(python=sys.executable))
        os.chmod(self.binary, 0o755)
        self.record = os.path.join(self.tmp, "record.json")
        self.state = os.path.join(self.tmp, "state.json")
        self.port = free_port()
        env = mock.patch.dict(os.environ, {"FAKE_TUNNEL_RECORD": self.record})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: tunnel.stop(state_file=self.state, grace=2, log=lambda *_: None))

    def start(self, **kw):
        opts = dict(metrics_port=self.port, logfile=os.path.join(self.tmp, "tunnel.log"), state_file=self.state,
                    timeout=30, log=lambda *_: None, poll_interval=0.1)
        opts.update(kw)
        return tunnel.start("k", "s3cret", "codebuild-app-7", self.binary, **opts)

    def test_start_waits_for_readyz_then_stop_sends_sigterm(self):
        pid = self.start(extra_args=["--nobump"])
        with open(self.record) as f:
            rec = json.load(f)
        self.assertEqual((rec["key"], rec["secret"]), ("k", "s3cret"))
        self.assertNotIn("s3cret", rec["args"], "credentials go through the environment, not argv")
        self.assertIn("--tunnel-identifier", rec["args"])
        self.assertEqual(rec["args"][rec["args"].index("--tunnel-identifier") + 1], "codebuild-app-7")
        self.assertEqual(rec["args"][-1], "--nobump")
        with open(self.state) as f:
            self.assertEqual(json.load(f)["pid"], pid)

        self.assertTrue(tunnel.stop(state_file=self.state, grace=5, log=lambda *_: None))
        self.assertTrue(os.path.exists(self.record + ".stopped"), "stop must use SIGTERM so the tunnel shuts down gracefully")
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(tunnel.stop(state_file=self.state, log=lambda *_: None))

    def test_readiness_probe_ignores_http_proxy(self):
        dead = f"http://127.0.0.1:{free_port()}"
        with mock.patch.dict(os.environ, {"HTTP_PROXY": dead, "http_proxy": dead, "NO_PROXY": "", "no_proxy": ""}):
            self.start()

    def test_refuses_a_second_tunnel(self):
        self.start()
        with self.assertRaisesRegex(tunnel.TunnelError, "already running"):
            self.start()

    def test_early_exit_reports_the_log(self):
        with mock.patch.dict(os.environ, {"FAKE_TUNNEL_MODE": "crash"}):
            with self.assertRaisesRegex(tunnel.TunnelError, r"(?s)exited with code 3.*authentication failed"):
                self.start()
        self.assertFalse(os.path.exists(self.state))

    def test_timeout_kills_the_tunnel(self):
        with mock.patch.dict(os.environ, {"FAKE_TUNNEL_MODE": "never"}):
            with self.assertRaisesRegex(tunnel.TunnelError, "not ready after 1s"):
                self.start(timeout=1)
        self.assertTrue(os.path.exists(self.record + ".stopped"))
        self.assertFalse(os.path.exists(self.state))


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.releases = os.path.join(self.tmp, "releases")
        os.makedirs(self.releases)
        self.asset = tunnel.platform_asset()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"#!/bin/sh\necho tunnel\n"
            info = tarfile.TarInfo(f"{self.asset}/bin/testingbot-tunnel")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        self.archive = buf.getvalue()
        self.write_release(hashlib.sha256(self.archive).hexdigest())

    def write_release(self, checksum):
        with open(os.path.join(self.releases, f"{self.asset}.tar.gz"), "wb") as f:
            f.write(self.archive)
        with open(os.path.join(self.releases, f"{self.asset}.tar.gz.sha256"), "w") as f:
            f.write(f"{checksum}  {self.asset}.tar.gz\n")

    def install(self):
        url = "file://" + self.releases + "/{asset}"
        return tunnel.install(version="9.9", install_dir=os.path.join(self.tmp, "cache"), release_url=url, log=lambda *_: None)

    def test_downloads_verifies_and_caches(self):
        binary = self.install()
        self.assertTrue(binary.endswith(f"9.9/{self.asset}/bin/testingbot-tunnel"))
        self.assertTrue(os.stat(binary).st_mode & stat.S_IXUSR)
        os.remove(os.path.join(self.releases, f"{self.asset}.tar.gz"))
        self.assertEqual(self.install(), binary, "a cached install must not download again")

    def test_checksum_mismatch(self):
        self.write_release("0" * 64)
        with self.assertRaisesRegex(tunnel.TunnelError, "Checksum mismatch"):
            self.install()

    def test_platform_names(self):
        for system, machine, expected in (("Linux", "x86_64", "linux-x64"), ("Linux", "aarch64", "linux-arm64"),
                                          ("Darwin", "arm64", "macos-arm64")):
            with mock.patch("platform.system", return_value=system), mock.patch("platform.machine", return_value=machine):
                self.assertEqual(tunnel.platform_asset(), f"testingbot-tunnel-{expected}")
        with mock.patch("platform.system", return_value="Windows"), mock.patch("platform.machine", return_value="AMD64"):
            with self.assertRaises(tunnel.TunnelError):
                tunnel.platform_asset()


if __name__ == "__main__":
    unittest.main()
