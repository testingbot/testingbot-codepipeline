import contextlib
import io
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from testingbot_codepipeline import cli

from stub import KEY, SECRET, Stub, fixture

CODEBUILD = {"CODEBUILD_BUILD_NUMBER": "42", "CODEBUILD_BUILD_ID": "web-e2e:6f1c2b9e-0000-4000-8000-000000000000"}


class BuildIdentifierTest(unittest.TestCase):
    def test_project_and_number(self):
        self.assertEqual(cli.build_identifier(CODEBUILD), "codebuild-web-e2e-42")

    def test_explicit_override(self):
        self.assertEqual(cli.build_identifier(dict(CODEBUILD, TESTINGBOT_BUILD="mine")), "mine")

    def test_sanitises_project_name(self):
        env = dict(CODEBUILD, CODEBUILD_BUILD_ID="my project/x:uuid")
        self.assertEqual(cli.build_identifier(env), "codebuild-my-project-x-42")

    def test_outside_codebuild(self):
        with self.assertRaises(SystemExit):
            cli.build_identifier({})


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.tmp = tempfile.TemporaryDirectory()
        env = dict(CODEBUILD, TESTINGBOT_KEY=KEY, TESTINGBOT_SECRET=SECRET, TESTINGBOT_API_BASE=self.stub.url)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.stub.close()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_env_is_evalable(self):
        code, out, _ = self.run_cli("env")
        self.assertEqual(code, 0)
        self.assertEqual(out, "export TESTINGBOT_BUILD=codebuild-web-e2e-42\nexport TESTINGBOT_TUNNEL_IDENTIFIER=codebuild-web-e2e-42\n")
        _, out, _ = self.run_cli("env", "--build", "it's mine")
        self.assertIn("export TESTINGBOT_BUILD='it'\"'\"'s mine'", out)

    def test_report_writes_junit_and_lists_failures(self):
        path = os.path.join(self.tmp.name, "reports", "tb.xml")
        code, out, _ = self.run_cli("report", "--build", "mixed", "--output", path)
        self.assertEqual(code, 0)
        self.assertEqual(ET.parse(path).getroot().get("failures"), "1")
        self.assertIn("1 passed, 1 failed, 1 without status", out)
        self.assertIn("FAILED  checkout spec  [Safari 17 on SONOMA]  https://testingbot.com/members/tests/102", out)

    def test_fail_on_failed_tests(self):
        path = os.path.join(self.tmp.name, "tb.xml")
        code, _, err = self.run_cli("report", "--build", "mixed", "--output", path, "--fail-on-failed-tests")
        self.assertEqual(code, 1)
        self.assertIn("1 TestingBot test(s) failed", err)
        self.assertTrue(os.path.exists(path), "the report is written before the exit code is decided")
        self.assertEqual(self.run_cli("report", "--build", "green", "--output", path, "--fail-on-failed-tests")[0], 0)

    def test_default_build_comes_from_codebuild(self):
        self.stub.builds["codebuild-web-e2e-42"] = fixture()[:1]
        code, out, _ = self.run_cli("report", "--output", os.path.join(self.tmp.name, "x.xml"))
        self.assertEqual(code, 0)
        self.assertIn("'codebuild-web-e2e-42'", out)

    def test_empty_build(self):
        path = os.path.join(self.tmp.name, "tb.xml")
        self.assertEqual(self.run_cli("report", "--build", "empty", "--output", path)[0], 0)
        self.assertEqual(self.run_cli("report", "--build", "empty", "--output", path, "--fail-on-empty")[0], 1)

    def test_missing_build_is_an_error(self):
        code, _, err = self.run_cli("report", "--build", "nope", "--output", os.path.join(self.tmp.name, "x.xml"))
        self.assertEqual(code, 1)
        self.assertIn("No TestingBot build named 'nope'", err)

    def test_waits_for_running_tests(self):
        running = [dict(fixture()[0], state="START")]
        self.stub.builds["slow"] = running
        calls = []

        def settle(_):
            calls.append(1)
            running[0]["state"] = "COMPLETE"

        with mock.patch("time.sleep", settle):
            code, out, _ = self.run_cli("report", "--build", "slow", "--output", os.path.join(self.tmp.name, "x.xml"))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("still running on TestingBot, waiting", out)
        self.assertIn("0 still running", out)

    def test_missing_credentials(self):
        del os.environ["TESTINGBOT_SECRET"]
        with self.assertRaises(SystemExit):
            self.run_cli("report", "--build", "mixed")


if __name__ == "__main__":
    unittest.main()
