"""testingbot-codebuild: TestingBot helpers for AWS CodeBuild buildspecs.

    eval "$(testingbot-codebuild env)"   # TESTINGBOT_BUILD + TESTINGBOT_TUNNEL_IDENTIFIER
    testingbot-codebuild tunnel start    # background tunnel, blocks until ready
    testingbot-codebuild tunnel stop
    testingbot-codebuild report          # JUnit XML for a CodeBuild report group
"""

import argparse
import os
import re
import shlex
import sys
import time

from . import __version__, api, junit, tunnel


def build_identifier(environ=os.environ):
    """The TestingBot build name for this CodeBuild run.

    TESTINGBOT_BUILD wins when set. Otherwise the project name and build
    number: CODEBUILD_BUILD_NUMBER alone collides between two CodeBuild
    projects in one TestingBot account, and CODEBUILD_BUILD_ID's UUID is
    unique but unreadable in the TestingBot dashboard."""
    if environ.get("TESTINGBOT_BUILD"):
        return environ["TESTINGBOT_BUILD"]
    number = environ.get("CODEBUILD_BUILD_NUMBER")
    project = (environ.get("CODEBUILD_BUILD_ID") or "").split(":", 1)[0]
    if not number:
        raise SystemExit("error: not running in CodeBuild (CODEBUILD_BUILD_NUMBER is unset). Pass --build or set TESTINGBOT_BUILD.")
    project = re.sub(r"[^A-Za-z0-9._-]+", "-", project).strip("-")
    return f"codebuild-{project}-{number}" if project else f"codebuild-{number}"


def credentials(environ=os.environ):
    key, secret = environ.get("TESTINGBOT_KEY"), environ.get("TESTINGBOT_SECRET")
    if not key or not secret:
        raise SystemExit("error: TESTINGBOT_KEY and TESTINGBOT_SECRET must be set, for example from Secrets Manager in the buildspec env section.")
    return key, secret


def cmd_env(args):
    ident = args.build or build_identifier()
    tunnel_id = os.environ.get("TESTINGBOT_TUNNEL_IDENTIFIER") or ident
    print(f"export TESTINGBOT_BUILD={shlex.quote(ident)}")
    print(f"export TESTINGBOT_TUNNEL_IDENTIFIER={shlex.quote(tunnel_id)}")
    return 0


def cmd_tunnel_start(args):
    key, secret = credentials()
    identifier = args.identifier or os.environ.get("TESTINGBOT_TUNNEL_IDENTIFIER") or build_identifier()
    extra = args.tunnel_args[1:] if args.tunnel_args[:1] == ["--"] else args.tunnel_args
    try:
        binary = args.binary or tunnel.install(version=args.tunnel_version, install_dir=args.install_dir)
        tunnel.start(key, secret, identifier, binary, metrics_port=args.metrics_port, logfile=args.logfile,
                     state_file=args.state_file, timeout=args.timeout, extra_args=extra)
    except tunnel.TunnelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_tunnel_stop(args):
    tunnel.stop(state_file=args.state_file, grace=args.grace)
    return 0


def cmd_report(args):
    key, secret = credentials()
    identifier = args.build or build_identifier()
    client = api.Client(key, secret)
    deadline = time.monotonic() + args.wait
    try:
        while True:
            tests = client.build_tests(identifier)
            counts = api.summarize(tests)
            # Tests update their status after the WebDriver session ends, so a
            # report written straight after the test runner can catch a few
            # still in START. --wait gives them a moment to settle.
            if counts["running"] == 0 or time.monotonic() >= deadline:
                break
            print(f"{counts['running']} test(s) still running on TestingBot, waiting...")
            time.sleep(args.poll_interval)
    except api.ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if not tests:
        print(f"warning: TestingBot build '{identifier}' contains no tests. Nothing to report.", file=sys.stderr)
        return 1 if args.fail_on_empty else 0

    junit.write(args.output, identifier, tests)
    print(f"TestingBot build '{identifier}': {counts['total']} test(s), {counts['passed']} passed, "
          f"{counts['failed']} failed, {counts['unknown']} without status, {counts['running']} still running.")
    print(f"Wrote {args.output}")
    for t in tests:
        if api.classify(t) == "failed":
            print(f"  FAILED  {t.get('name') or t.get('id')}  [{api.browser_label(t)}]  {api.test_url(t)}")

    if args.fail_on_failed_tests and counts["failed"]:
        print(f"error: {counts['failed']} TestingBot test(s) failed.", file=sys.stderr)
        return 1
    return 0


def parser():
    p = argparse.ArgumentParser(prog="testingbot-codebuild", description="TestingBot helpers for AWS CodeBuild.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("env", help="print TESTINGBOT_BUILD and TESTINGBOT_TUNNEL_IDENTIFIER exports for eval")
    e.add_argument("--build", help="build identifier (default: codebuild-<project>-<build number>)")
    e.set_defaults(func=cmd_env)

    t = sub.add_parser("tunnel", help="start or stop TestingBot Tunnel").add_subparsers(dest="action", required=True)
    ts = t.add_parser("start", help="start the tunnel in the background and wait until it is ready")
    ts.add_argument("--identifier", help="tunnel identifier (default: $TESTINGBOT_TUNNEL_IDENTIFIER, else the build identifier)")
    ts.add_argument("--tunnel-version", default=tunnel.DEFAULT_VERSION, help="tunnel release (default: %(default)s)")
    ts.add_argument("--metrics-port", type=int, default=tunnel.DEFAULT_METRICS_PORT, help="port of /readyz (default: %(default)s)")
    ts.add_argument("--timeout", type=int, default=180, help="seconds to wait for readiness (default: %(default)s)")
    ts.add_argument("--logfile", help="tunnel log (default: <tmp>/testingbot-tunnel.log)")
    ts.add_argument("--install-dir", help="download cache (default: ~/.testingbot-tunnel)")
    ts.add_argument("--binary", help=argparse.SUPPRESS)
    ts.add_argument("--state-file", help=argparse.SUPPRESS)
    ts.add_argument("tunnel_args", nargs=argparse.REMAINDER, help="extra tunnel flags after --, e.g. -- --nobump")
    ts.set_defaults(func=cmd_tunnel_start)
    tp = t.add_parser("stop", help="stop the tunnel started by `tunnel start`")
    tp.add_argument("--grace", type=int, default=30, help="seconds to wait after SIGTERM before SIGKILL")
    tp.add_argument("--state-file", help=argparse.SUPPRESS)
    tp.set_defaults(func=cmd_tunnel_stop)

    r = sub.add_parser("report", help="write the TestingBot build as JUnit XML for a CodeBuild report group")
    r.add_argument("--build", help="build identifier (default: $TESTINGBOT_BUILD, else codebuild-<project>-<build number>)")
    r.add_argument("--output", default="testingbot-reports/testingbot.xml", help="JUnit file (default: %(default)s)")
    r.add_argument("--fail-on-failed-tests", action="store_true", help="exit 1 when the build contains failed tests")
    r.add_argument("--fail-on-empty", action="store_true", help="exit 1 when the build contains no tests")
    r.add_argument("--wait", type=int, default=60, help="seconds to wait for running tests to finish (default: %(default)s)")
    r.add_argument("--poll-interval", type=float, default=5, help=argparse.SUPPRESS)
    r.set_defaults(func=cmd_report)
    return p


def main(argv=None):
    # CodeBuild pipes stdout, which would otherwise be block-buffered and land
    # in the log after the stderr it preceded.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    args = parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
