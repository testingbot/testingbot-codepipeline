#!/usr/bin/env bash
# Publish (or add a version to) the gate in the AWS Serverless Application
# Repository. A new application is PRIVATE; make it public separately with
#   aws serverlessrepo put-application-policy --application-id <arn> \
#     --statements Principals='*',Actions=Deploy
# SAR_BUCKET must allow serverlessrepo.amazonaws.com s3:GetObject.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${SAR_BUCKET:?set SAR_BUCKET}"
export AWS_REGION=${AWS_REGION:-us-east-1} AWS_DEFAULT_REGION=${AWS_REGION:-us-east-1}
sam build --region "$AWS_REGION" --template template.yaml --build-dir .aws-sam/sar >/dev/null
sam package --region "$AWS_REGION" --template-file .aws-sam/sar/template.yaml --s3-bucket "$SAR_BUCKET" \
  --s3-prefix sar --output-template-file .aws-sam/sar/packaged.yaml >/dev/null
sam publish --region "$AWS_REGION" --template .aws-sam/sar/packaged.yaml
