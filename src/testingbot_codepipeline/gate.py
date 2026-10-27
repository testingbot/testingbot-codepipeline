"""CodePipeline "Invoke → AWS Lambda" action that gates a stage on a TestingBot build.

Place it after the build/test stage and before deploy. It reads the build
identifier from the action's UserParameters, usually a CodeBuild exported
variable:

    {"build": "#{TestVariables.TESTINGBOT_BUILD}"}

and succeeds or fails the CodePipeline job on the TestingBot result. Tests
still running are waited for through CodePipeline continuation tokens, so a
slow status update never holds a Lambda open.

Every exit path reports a job result: an unreported job leaves the pipeline
waiting until the action times out, which reads as a hang rather than an error.
"""

import json
import os
import time

from . import api

DEFAULT_MAX_WAIT = 300
# Pause before handing back a continuation token, so CodePipeline does not
# re-invoke in a tight loop while tests finish.
CONTINUATION_PAUSE = 15
SUMMARY_LIMIT = 2048   # executionDetails.summary
MESSAGE_LIMIT = 5000   # failureDetails.message

_clients = {}


def _client(name):
    # Created lazily so the module imports without boto3 (the CLI and the
    # tests); the Lambda runtime ships it.
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name)
    return _clients[name]


def parse_parameters(raw):
    """UserParameters is either a JSON object or a bare build identifier."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError('UserParameters is empty. Set it to {"build": "#{<namespace>.TESTINGBOT_BUILD}"}.')
    if raw.startswith("{"):
        try:
            params = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"UserParameters is not valid JSON: {e}") from None
    else:
        params = {"build": raw}
    build = str(params.get("build") or "").strip()
    if not build:
        raise ValueError('UserParameters has no "build".')
    if build.startswith("#{"):
        # CodePipeline leaves an unresolvable variable reference as-is.
        raise ValueError(f"The build variable {build} was not resolved. Check the namespace of the test action "
                         "and that its buildspec lists TESTINGBOT_BUILD under env.exported-variables.")
    return {
        "build": build,
        "allow_unknown": bool(params.get("allow_unknown", True)),
        "allow_empty": bool(params.get("allow_empty", False)),
        "max_failed": int(params.get("max_failed", 0)),
        "max_wait": int(params.get("max_wait", DEFAULT_MAX_WAIT)),
    }


def load_credentials(environ=os.environ):
    if environ.get("TESTINGBOT_KEY") and environ.get("TESTINGBOT_SECRET"):
        return environ["TESTINGBOT_KEY"], environ["TESTINGBOT_SECRET"]
    arn = environ.get("TESTINGBOT_SECRET_ARN")
    if not arn:
        raise RuntimeError("Set TESTINGBOT_SECRET_ARN to a Secrets Manager secret holding {\"key\": ..., \"secret\": ...}.")
    value = json.loads(_client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"])
    try:
        return value["key"], value["secret"]
    except KeyError:
        raise RuntimeError(f"Secret {arn} must be a JSON object with \"key\" and \"secret\".") from None


def evaluate(params, tests):
    """Return (verdict, message): verdict is pass, fail or wait."""
    c = api.summarize(tests)
    line = (f"TestingBot build '{params['build']}': {c['total']} test(s), {c['passed']} passed, {c['failed']} failed, "
            f"{c['unknown']} without status, {c['running']} running.")
    if c["running"]:
        return "wait", line
    if not tests:
        return ("pass" if params["allow_empty"] else "fail"), \
            f"TestingBot build '{params['build']}' contains no tests. Did the tests set the 'build' capability?"
    problems = []
    if c["failed"] > params["max_failed"]:
        failed = [t for t in tests if api.classify(t) == "failed"]
        listed = "; ".join(f"{t.get('name') or t.get('id')} [{api.browser_label(t)}] {api.test_url(t)}" for t in failed[:10])
        more = f" (+{len(failed) - 10} more)" if len(failed) > 10 else ""
        problems.append(f"Failed: {listed}{more}")
    if c["unknown"] and not params["allow_unknown"]:
        problems.append(f"{c['unknown']} test(s) reported no pass/fail status and allow_unknown is false.")
    if problems:
        return "fail", " ".join([line] + problems)
    return "pass", line


def handler(event, context):
    job = event["CodePipeline.job"]
    job_id = job["id"]
    data = job.get("data") or {}
    cp = _client("codepipeline")
    external_id = None
    try:
        config = (data.get("actionConfiguration") or {}).get("configuration") or {}
        params = parse_parameters(config.get("UserParameters"))
        external_id = params["build"][:1500]
        token = json.loads(data["continuationToken"]) if data.get("continuationToken") else {"started": time.time()}

        key, secret = load_credentials()
        tests = api.Client(key, secret).build_tests(params["build"])
        verdict, message = evaluate(params, tests)

        if verdict == "wait":
            waited = time.time() - token["started"]
            if waited < params["max_wait"]:
                print(f"{message} Waiting ({int(waited)}s of {params['max_wait']}s).")
                time.sleep(min(CONTINUATION_PAUSE, max(0, params["max_wait"] - waited)))
                cp.put_job_success_result(
                    jobId=job_id, continuationToken=json.dumps(token),
                    executionDetails={"summary": message[:SUMMARY_LIMIT], "externalExecutionId": external_id,
                                      "percentComplete": min(99, int(100 * waited / max(1, params["max_wait"])))})
                return {"verdict": "wait"}
            verdict, message = "fail", f"{message} Gave up after {params['max_wait']}s waiting for running tests."

        print(message)
        if verdict == "pass":
            cp.put_job_success_result(jobId=job_id, executionDetails={
                "summary": message[:SUMMARY_LIMIT], "externalExecutionId": external_id, "percentComplete": 100})
        else:
            cp.put_job_failure_result(jobId=job_id, failureDetails={
                "type": "JobFailed", "message": message[:MESSAGE_LIMIT], "externalExecutionId": external_id})
        return {"verdict": verdict}
    except Exception as e:  # report every failure to CodePipeline, see module docstring
        message = f"{type(e).__name__}: {e}" if not isinstance(e, (ValueError, api.ApiError, RuntimeError)) else str(e)
        print(f"error: {message}")
        details = {"type": "JobFailed", "message": message[:MESSAGE_LIMIT]}
        if external_id:
            details["externalExecutionId"] = external_id
        cp.put_job_failure_result(jobId=job_id, failureDetails=details)
        return {"verdict": "error"}
