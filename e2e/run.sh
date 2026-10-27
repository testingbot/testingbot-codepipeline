#!/usr/bin/env bash
# End-to-end test in a real AWS account. Needs: a Secrets Manager secret named
# testingbot-codepipeline-e2e holding {"key","secret"} (its ARN in SECRET_ARN), an S3 bucket for sam packaging
# (SAM_BUCKET), the sam CLI. Leaves both stacks up; ./run.sh teardown removes them.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_REGION=${AWS_REGION:-us-east-1}
# The aws CLI is only guaranteed to read AWS_DEFAULT_REGION, and sam ignores both
# for its S3 upload, so a profile region (e.g. "auto" for R2) would win.
export AWS_DEFAULT_REGION=$AWS_REGION
GATE=testingbot-codepipeline-gate-e2e
STACK=testingbot-codepipeline-e2e

if [[ "${1:-}" == "teardown" ]]; then
  bucket=$(aws cloudformation describe-stacks --stack-name $STACK --query "Stacks[0].Outputs[?OutputKey=='Bucket'].OutputValue" --output text 2>/dev/null || true)
  if [[ -n "$bucket" && "$bucket" != "None" ]]; then
    python3 - "$bucket" <<'PY'
import json, subprocess, sys
b = sys.argv[1]
out = json.loads(subprocess.check_output(["aws", "s3api", "list-object-versions", "--bucket", b, "--output", "json"]) or b"{}")
objs = [{"Key": o["Key"], "VersionId": o["VersionId"]} for k in ("Versions", "DeleteMarkers") for o in out.get(k) or []]
for i in range(0, len(objs), 1000):
    subprocess.check_call(["aws", "s3api", "delete-objects", "--bucket", b, "--delete", json.dumps({"Objects": objs[i:i+1000]})], stdout=subprocess.DEVNULL)
PY
  fi
  aws cloudformation delete-stack --stack-name $STACK && aws cloudformation wait stack-delete-complete --stack-name $STACK
  sam delete --stack-name $GATE --no-prompts --region "$AWS_REGION"
  exit 0
fi

: "${SECRET_ARN:?set SECRET_ARN}" "${SAM_BUCKET:?set SAM_BUCKET}"

(cd .. && sam build --region "$AWS_REGION" --template template.yaml --build-dir e2e/.aws-sam/build >/dev/null && \
  sam deploy --template-file e2e/.aws-sam/build/template.yaml --stack-name $GATE --region "$AWS_REGION" --s3-bucket "$SAM_BUCKET" \
    --s3-prefix e2e --capabilities CAPABILITY_IAM --no-fail-on-empty-changeset \
    --parameter-overrides TestingBotSecretArn="$SECRET_ARN" >/dev/null)
out() { aws cloudformation describe-stacks --stack-name "$1" --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text; }
aws cloudformation deploy --stack-name $STACK --template-file pipeline.yaml --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset --parameter-overrides SecretArn="$SECRET_ARN" \
  GateFunctionName="$(out $GATE GateFunctionName)" GateFunctionArn="$(out $GATE GateFunctionArn)" >/dev/null
bucket=$(out $STACK Bucket); pipeline=$(out $STACK Pipeline)

run() {  # $1 = pass|fail; prints the execution's final status and the gate's message
  rm -f source/E2E_FAIL; [[ "$1" == fail ]] && touch source/E2E_FAIL
  # Ships the working tree's package in the zip, so the run tests this
  # checkout rather than whatever is on PyPI (PYPI=1 tests the release).
  rm -rf source/pkg; [[ -z "${PYPI:-}" ]] && mkdir source/pkg && cp -R ../src ../pyproject.toml ../README.md ../LICENSE source/pkg/
  (cd source && rm -f ../source.zip && zip -q -r ../source.zip . -x '*__pycache__*') && rm -rf source/E2E_FAIL source/pkg
  aws s3 cp --quiet source.zip "s3://$bucket/source.zip"
  id=$(aws codepipeline start-pipeline-execution --name "$pipeline" --query pipelineExecutionId --output text)
  while :; do
    sleep 15
    # A just-started execution can 404 for a few seconds (eventual consistency).
    status=$(aws codepipeline get-pipeline-execution --pipeline-name "$pipeline" --pipeline-execution-id "$id" \
      --query pipelineExecution.status --output text 2>/dev/null || echo InProgress)
    [[ "$status" == InProgress ]] || break
  done
  echo "== $1 run: $status ($id)"
  aws codepipeline list-action-executions --pipeline-name "$pipeline" --filter pipelineExecutionId="$id" \
    --query "actionExecutionDetails[].[actionName,status,output.executionResult.externalExecutionSummary]" --output text
}
# RUNS="fail" ./run.sh runs only the failing case.
for r in ${RUNS:-pass fail}; do run "$r"; done
