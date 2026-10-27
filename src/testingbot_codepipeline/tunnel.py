"""Download, start, wait for and stop TestingBot Tunnel inside a CodeBuild build.

Uses the self-contained Linux/macOS builds (bundled Java runtime), so the build
image needs no `java:` runtime-version. Readiness is taken from the tunnel's
own /readyz endpoint on the metrics port rather than a ready file, because
/readyz only turns 200 after the tunnel's startup self-test passes."""

import hashlib
import json
import os
import platform
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

DEFAULT_VERSION = "5.0"
DEFAULT_METRICS_PORT = 8003
RELEASE_URL = "https://github.com/testingbot/Testingbot-Tunnel/releases/download/v{version}/{asset}"


class TunnelError(Exception):
    pass


def default_state_file():
    return os.path.join(tempfile.gettempdir(), "testingbot-tunnel.json")


def default_install_dir():
    # Under $HOME so it can be listed in the buildspec `cache: paths:` and
    # survive between builds.
    return os.path.join(os.path.expanduser("~"), ".testingbot-tunnel")


def platform_asset():
    system = platform.system()
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    os_name = {"Linux": "linux", "Darwin": "macos"}.get(system)
    if not arch or not os_name:
        raise TunnelError(f"No self-contained tunnel build for {system}/{machine}. Linux and macOS on x64 or arm64 are supported.")
    return f"testingbot-tunnel-{os_name}-{arch}"


def _download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "testingbot-codepipeline"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
    except urllib.error.URLError as e:
        raise TunnelError(f"Could not download {url}: {getattr(e, 'reason', e)}") from None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(version=DEFAULT_VERSION, install_dir=None, release_url=RELEASE_URL, log=print):
    """Return the path of the tunnel binary, downloading it when not cached."""
    name = platform_asset()
    root = os.path.join(install_dir or default_install_dir(), version)
    binary = os.path.join(root, name, "bin", "testingbot-tunnel")
    if os.access(binary, os.X_OK):
        log(f"Using cached TestingBot Tunnel {version} from {root}")
        return binary

    asset = f"{name}.tar.gz"
    os.makedirs(root, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, asset)
        checksum = archive + ".sha256"
        log(f"Downloading TestingBot Tunnel {version} ({asset})")
        _download(release_url.format(version=version, asset=asset), archive)
        _download(release_url.format(version=version, asset=asset + ".sha256"), checksum)
        with open(checksum) as f:
            expected = f.read().split()[0].strip().lower()
        actual = _sha256(archive)
        if actual != expected:
            raise TunnelError(f"Checksum mismatch for {asset}: expected {expected}, got {actual}")
        with tarfile.open(archive) as tar:
            # The "data" filter (3.12+, backported to 3.8.17+) refuses absolute
            # paths, ../ escapes and links leaving the archive. It keeps the
            # owner's exec bits, which the bundled runtime needs.
            if hasattr(tarfile, "data_filter"):
                tar.extractall(root, filter="data")
            else:
                tar.extractall(root)

    if not os.access(binary, os.X_OK):
        raise TunnelError(f"The archive did not contain {binary}")
    return binary


# The probe is always local. Without an explicit empty ProxyHandler, urllib
# sends it to HTTP_PROXY (set on many VPC CodeBuild projects) or to the macOS
# system proxy, and the tunnel never looks ready.
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ready(port):
    try:
        with _local.open(f"http://127.0.0.1:{port}/readyz", timeout=2) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _tail(path, lines=20):
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-lines:])
    except OSError:
        return ""


def start(key, secret, identifier, binary, metrics_port=DEFAULT_METRICS_PORT, logfile=None,
          state_file=None, timeout=180, extra_args=(), log=print, poll_interval=1.0):
    state_file = state_file or default_state_file()
    logfile = logfile or os.path.join(tempfile.gettempdir(), "testingbot-tunnel.log")

    if os.path.exists(state_file):
        raise TunnelError(f"A tunnel is already running according to {state_file}. Run `testingbot-codebuild tunnel stop` first.")

    args = [binary, "--metrics-port", str(metrics_port), "--logfile", logfile]
    if identifier:
        args += ["--tunnel-identifier", identifier]
    args += list(extra_args)

    # The tunnel reads TESTINGBOT_KEY/TESTINGBOT_SECRET itself, which keeps the
    # credentials out of the process list and out of the CodeBuild log.
    env = dict(os.environ, TESTINGBOT_KEY=key, TESTINGBOT_SECRET=secret)
    log("Starting TestingBot Tunnel" + (f" with identifier '{identifier}'" if identifier else ""))
    with open(logfile, "ab") as out:
        # A new session detaches the tunnel from this command's shell, so it
        # keeps running into the next buildspec command and phase.
        proc = subprocess.Popen(args, env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)

    with open(state_file, "w") as f:
        json.dump({"pid": proc.pid, "metrics_port": metrics_port, "identifier": identifier, "logfile": logfile}, f)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _forget(state_file)
            raise TunnelError(f"The tunnel exited with code {proc.returncode} before it was ready.\n{_tail(logfile)}")
        if _ready(metrics_port):
            log(f"TestingBot Tunnel is ready (pid {proc.pid}, log {logfile})")
            return proc.pid
        time.sleep(poll_interval)

    _terminate(proc.pid, grace=10)
    _forget(state_file)
    raise TunnelError(f"The tunnel was not ready after {timeout}s.\n{_tail(logfile)}")


def _forget(state_file):
    try:
        os.remove(state_file)
    except FileNotFoundError:
        pass


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0; reap it when it is our own child.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _terminate(pid, grace=30):
    # SIGTERM triggers the tunnel's graceful shutdown, which tells TestingBot
    # the tunnel is going away instead of leaving it to time out.
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _alive(pid):
            return
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop(state_file=None, grace=30, log=print):
    state_file = state_file or default_state_file()
    try:
        with open(state_file) as f:
            state = json.load(f)
    except FileNotFoundError:
        log("No running TestingBot Tunnel found.")
        return False
    _terminate(int(state["pid"]), grace=grace)
    _forget(state_file)
    log(f"Stopped TestingBot Tunnel (pid {state['pid']})")
    return True
