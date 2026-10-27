import json
import os
import unittest
from unittest import mock

from testingbot_codepipeline import gate

from stub import KEY, SECRET, Stub, fixture


class FakeCodePipeline:
    def __init__(self):
        self.calls = []

    def put_job_success_result(self, **kw):
        self.calls.append(("success", kw))

    def put_job_failure_result(self, **kw):
        self.calls.append(("failure", kw))


class FakeSecrets:
    def __init__(self, value):
        self.value = value
        self.asked = []

    def get_secret_value(self, SecretId):
        self.asked.append(SecretId)
        return {"SecretString": json.dumps(self.value)}


def event(user_parameters, continuation=None):
    data = {"actionConfiguration": {"configuration": {"FunctionName": "tb-gate", "UserParameters": user_parameters}}}
    if continuation:
        data["continuationToken"] = continuation
    return {"CodePipeline.job": {"id": "job-1", "accountId": "123", "data": data}}


class ParseParametersTest(unittest.TestCase):
    def test_bare_string_and_json(self):
        self.assertEqual(gate.parse_parameters("b-1")["build"], "b-1")
        p = gate.parse_parameters('{"build": "b-2", "max_failed": 2, "allow_unknown": false}')
        self.assertEqual((p["build"], p["max_failed"], p["allow_unknown"], p["max_wait"]), ("b-2", 2, False, 300))

    def test_rejects_unresolved_variable(self):
        with self.assertRaisesRegex(ValueError, "was not resolved"):
            gate.parse_parameters('{"build": "#{TestVariables.TESTINGBOT_BUILD}"}')

    def test_rejects_empty_and_bad_json(self):
        for raw in ("", "  ", "{}", "{nope"):
            with self.assertRaises(ValueError):
                gate.parse_parameters(raw)


class EvaluateTest(unittest.TestCase):
    def params(self, **kw):
        return dict(gate.parse_parameters("b"), **kw)

    def test_verdicts(self):
        self.assertEqual(gate.evaluate(self.params(), [fixture()[0]])[0], "pass")
        verdict, message = gate.evaluate(self.params(), fixture())
        self.assertEqual(verdict, "fail")
        self.assertIn("checkout spec [Safari 17 on SONOMA] https://testingbot.com/members/tests/102", message)
        self.assertEqual(gate.evaluate(self.params(max_failed=1), fixture())[0], "pass")
        self.assertEqual(gate.evaluate(self.params(max_failed=1, allow_unknown=False), fixture())[0], "fail")
        self.assertEqual(gate.evaluate(self.params(), [dict(fixture()[0], state="START")])[0], "wait")

    def test_empty_build_fails_unless_allowed(self):
        self.assertEqual(gate.evaluate(self.params(), [])[0], "fail")
        self.assertEqual(gate.evaluate(self.params(allow_empty=True), [])[0], "pass")


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.cp = FakeCodePipeline()
        self.secrets = FakeSecrets({"key": KEY, "secret": SECRET})
        clients = mock.patch.dict(gate._clients, {"codepipeline": self.cp, "secretsmanager": self.secrets}, clear=True)
        env = mock.patch.dict(os.environ, {"TESTINGBOT_SECRET_ARN": "arn:secret", "TESTINGBOT_API_BASE": self.stub.url}, clear=True)
        sleep = mock.patch("time.sleep")
        for p in (clients, env, sleep):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.stub.close()

    def test_success(self):
        self.assertEqual(gate.handler(event('{"build": "green"}'), None), {"verdict": "pass"})
        kind, kw = self.cp.calls[0]
        self.assertEqual(kind, "success")
        self.assertEqual(kw["jobId"], "job-1")
        self.assertEqual(kw["executionDetails"]["externalExecutionId"], "green")
        self.assertNotIn("continuationToken", kw)
        self.assertEqual(self.secrets.asked, ["arn:secret"])

    def test_failure(self):
        gate.handler(event("mixed"), None)
        kind, kw = self.cp.calls[0]
        self.assertEqual(kind, "failure")
        self.assertEqual(kw["failureDetails"]["type"], "JobFailed")
        self.assertIn("1 failed", kw["failureDetails"]["message"])

    def test_running_tests_continue_then_give_up(self):
        self.stub.builds["slow"] = [dict(fixture()[0], state="START")]
        self.assertEqual(gate.handler(event('{"build": "slow", "max_wait": 120}'), None), {"verdict": "wait"})
        kind, kw = self.cp.calls[-1]
        self.assertEqual(kind, "success")
        token = json.loads(kw["continuationToken"])

        expired = json.dumps({"started": token["started"] - 1000})
        self.assertEqual(gate.handler(event('{"build": "slow", "max_wait": 120}', expired), None), {"verdict": "fail"})
        kind, kw = self.cp.calls[-1]
        self.assertEqual(kind, "failure")
        self.assertIn("Gave up after 120s", kw["failureDetails"]["message"])

    def test_continuation_resolves_when_tests_finish(self):
        token = json.dumps({"started": 0})
        with mock.patch("time.time", return_value=10):
            self.assertEqual(gate.handler(event("green", token), None), {"verdict": "pass"})

    def test_every_error_is_reported_to_codepipeline(self):
        for params, needle in (('{"build": "#{X.TESTINGBOT_BUILD}"}', "was not resolved"),
                               ("nope", "No TestingBot build named 'nope'")):
            gate.handler(event(params), None)
            kind, kw = self.cp.calls[-1]
            self.assertEqual(kind, "failure")
            self.assertIn(needle, kw["failureDetails"]["message"])

        self.secrets.value = {"key": "wrong", "secret": "creds"}
        gate.handler(event("green"), None)
        self.assertIn("rejected the credentials", self.cp.calls[-1][1]["failureDetails"]["message"])

        self.secrets.value = {"username": "x"}
        gate.handler(event("green"), None)
        message = self.cp.calls[-1][1]["failureDetails"]["message"]
        self.assertIn('must be a JSON object with "key" and "secret"', message)

    def test_secret_never_reaches_codepipeline(self):
        gate.handler(event("mixed"), None)
        gate.handler(event("nope"), None)
        self.assertNotIn(SECRET, json.dumps(self.cp.calls))


if __name__ == "__main__":
    unittest.main()
