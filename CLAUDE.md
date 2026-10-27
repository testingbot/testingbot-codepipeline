# CLAUDE.md

## What this is

The AWS counterpart of the Bitbucket `testingbot-report-pipe` and the GitHub `testingbot-tunnel-action`. It is one Python
package, `src/testingbot_codepipeline`, shipped two ways:

* a CLI, `testingbot-codebuild` (`cli.py`), installed with pip inside a CodeBuild buildspec, and
* a Lambda handler, `gate.handler` (`gate.py`), deployed by `template.yaml` (SAM; also the Serverless Application
  Repository metadata), used as a CodePipeline *Invoke* action.

`api.py` (TestingBot REST client and test classification), `junit.py` and `tunnel.py` are shared.

**Standard library only.** This is deliberate. The CLI must `pip install` cleanly on any CodeBuild image, and the Lambda
must deploy without a layer. `boto3` is imported lazily in `gate._client` because only the Lambda runtime has it. Do not
add dependencies.

## Commands

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
docker run --rm -v "$PWD":/src:ro -w /src -e PYTHONPATH=/src/src:/src/tests python:3.9-slim \
  python -m unittest discover -s tests            # oldest supported Python
```

## Things that matter

* **Test classification lives in `api.classify`** and both the report and the gate use it. `state != COMPLETE` is
  *running*. `unknown` (DB `success == 2`, the column default) is a test whose status was never reported, so it is never
  treated as a failure by default. Many suites never call the status API.
* **API fixture shape**: `tests/stub.py` follows a real `/v1/builds/:id` response, not the OpenAPI spec.
  `browser_version` is a string, `browser` already contains the version, and `status_message` can be `null`. The server
  side is `app/api/testingbot/builds_api.rb` in the web repo: `count` maxes at 500, so `build_tests` paginates on
  `meta.total`.
* **Credentials**: the tunnel gets `TESTINGBOT_KEY`/`TESTINGBOT_SECRET` through its environment (it reads them itself,
  `App.java`), never argv. `Client.__repr__` hides the auth header. `test_gate` asserts the secret never reaches a
  CodePipeline call.
* **The gate must always report a job result.** Every path in `handler`, including exceptions, ends in
  `put_job_success_result` or `put_job_failure_result`. Otherwise the pipeline sits until the action times out.
  Waiting for running tests uses continuation tokens holding `{"started": epoch}`, with a short sleep so CodePipeline
  does not spin.
* **Tunnel readiness** comes from `/readyz` on the metrics port, not `--readyfile`. The tunnel docs call the ready file
  legacy, and `/readyz` only turns 200 after the startup self-test passes. The child is started with
  `start_new_session=True` so it outlives the buildspec command. The `ResourceWarning: subprocess is still running` in
  the tunnel tests is that intended detachment.
* Release assets are `testingbot-tunnel-{linux,macos}-{x64,arm64}.tar.gz` plus `.sha256` on the GitHub release,
  containing `<name>/bin/testingbot-tunnel` with a bundled runtime. Bump `tunnel.DEFAULT_VERSION` with tunnel releases.

## Releasing

Bump `__version__` in `src/testingbot_codepipeline/__init__.py` (and `SemanticVersion` in `template.yaml`), then publish
a GitHub release tagged `v<version>`. `publish.yml` refuses a tag that does not match `__version__`, builds, and uploads
to PyPI through trusted publishing (OIDC, environment `pypi`). No token is stored anywhere.
