# TestingBot for AWS CodePipeline and CodeBuild

[![test](https://github.com/testingbot/testingbot-codepipeline/actions/workflows/test.yml/badge.svg)](https://github.com/testingbot/testingbot-codepipeline/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/testingbot-codepipeline)](https://pypi.org/project/testingbot-codepipeline/)

Run [TestingBot](https://testingbot.com) Selenium and Appium tests from AWS CodePipeline, see the results in CodeBuild
test reports, and stop a deploy when TestingBot tests fail.

This repository has two parts. Use either one, or both:

| Part | Where it runs | What it does |
| --- | --- | --- |
| `testingbot-codebuild` CLI | your CodeBuild buildspec | Starts and stops [TestingBot Tunnel](https://testingbot.com/support/tunnel), names the TestingBot build after the CodeBuild run, and writes the TestingBot results as a JUnit report for a CodeBuild report group. |
| CodePipeline gate (Lambda) | a CodePipeline *Invoke* action | Passes or fails the pipeline on the TestingBot build result, with links to every failing test. |

Both use only the Python standard library. A stock CodeBuild image and the Lambda Python runtime need nothing extra.

## Credentials

Store your TestingBot key and secret, from the [member area](https://testingbot.com/members/user/edit), in AWS Secrets
Manager as one secret, for example named `testingbot`:

```json
{"key": "YOUR_KEY", "secret": "YOUR_SECRET"}
```

## CodeBuild: tunnel and test reports

```yaml
version: 0.2

env:
  secrets-manager:
    TESTINGBOT_KEY: testingbot:key
    TESTINGBOT_SECRET: testingbot:secret
  exported-variables:
    - TESTINGBOT_BUILD

phases:
  install:
    commands:
      - pip install --quiet testingbot-codepipeline
  pre_build:
    commands:
      - eval "$(testingbot-codebuild env)"
      - testingbot-codebuild tunnel start
  build:
    commands:
      - npm run test:e2e
  post_build:
    commands:
      - testingbot-codebuild tunnel stop
      - testingbot-codebuild report

reports:
  testingbot:
    files:
      - testingbot-reports/testingbot.xml
    file-format: JUNITXML
```

Your tests must send the TestingBot `build` and `tunnel-identifier` capabilities, read from the environment:

```js
'tb:options': {
  build: process.env.TESTINGBOT_BUILD,
  'tunnel-identifier': process.env.TESTINGBOT_TUNNEL_IDENTIFIER
}
```

A full example is in [`examples/buildspec.yml`](examples/buildspec.yml).

### Commands

| Command | Description |
| --- | --- |
| `testingbot-codebuild env` | Prints `export TESTINGBOT_BUILD=… TESTINGBOT_TUNNEL_IDENTIFIER=…` for `eval`. The default is `codebuild-<project>-<build number>`. Set `TESTINGBOT_BUILD` yourself to override it. |
| `testingbot-codebuild tunnel start` | Downloads the self-contained tunnel for the build's architecture (x64 or arm64, no Java needed) and verifies its SHA-256. Then it starts the tunnel in the background and blocks until `/readyz` reports it is forwarding. Pass extra tunnel flags after `--`, e.g. `tunnel start -- --nobump`. Options: `--identifier`, `--tunnel-version` (default `5.0`), `--metrics-port` (default `8003`), `--timeout` (default `180`), `--logfile`, `--install-dir` (default `~/.testingbot-tunnel`, cacheable). |
| `testingbot-codebuild tunnel stop` | Stops the tunnel with SIGTERM, so it closes gracefully. Put it in `post_build`, which also runs when the tests fail. |
| `testingbot-codebuild report` | Fetches every test in the TestingBot build and writes JUnit XML, one suite per browser and OS. Each test links to its TestingBot video and logs. Options: `--build`, `--output` (default `testingbot-reports/testingbot.xml`), `--fail-on-failed-tests`, `--fail-on-empty`, `--wait` (seconds to wait for tests still running, default `60`). |

Tests that never reported a pass/fail status to TestingBot appear as *skipped*, not failed.

The tunnel reads the key and secret from the environment, so they never appear on a command line or in the build log.

## CodePipeline: gate a deploy on TestingBot results

1. Deploy the gate from the
   [AWS Serverless Application Repository](https://serverlessrepo.aws.amazon.com/applications/us-east-1/664425719481/testingbot-codepipeline-gate)
   (choose **Deploy**, tick *I acknowledge that this app creates custom IAM roles*), or from this repository with
   `sam deploy --guided`. Both ask for `TestingBotSecretArn`, the ARN of the secret above.
2. In the CodeBuild action that runs your tests, set the **variable namespace** to `TestVariables` and keep
   `TESTINGBOT_BUILD` under `env.exported-variables` in the buildspec.
3. Add an action after it: provider **AWS Lambda**, function `GateFunctionName` from the stack outputs, and user
   parameters:

   ```json
   {"build": "#{TestVariables.TESTINGBOT_BUILD}"}
   ```

4. Allow the pipeline role `lambda:InvokeFunction` on the gate.

CloudFormation for steps 2-4 is in [`examples/pipeline.yml`](examples/pipeline.yml).

| User parameter | Default | Meaning |
| --- | --- | --- |
| `build` | required | TestingBot build identifier. A bare string instead of JSON is read as this. |
| `max_failed` | `0` | Failed tests tolerated before the gate fails. |
| `allow_unknown` | `true` | Whether tests without a reported pass/fail status are acceptable. |
| `allow_empty` | `false` | Whether a build with no tests passes. The default catches tests that forgot the `build` capability. |
| `max_wait` | `300` | Seconds to wait for tests still running. The gate waits through CodePipeline continuation tokens, so no Lambda stays open. |

The gate always reports a result to CodePipeline, also on bad parameters or credentials, so the pipeline never waits
for the action to time out.

## Development

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
```

## License

MIT
