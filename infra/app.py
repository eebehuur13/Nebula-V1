#!/usr/bin/env python3
"""CDK entry point for Nebula. Run via `npx aws-cdk@2 synth|diff|deploy` from infra/."""

import os

# jsii prints a large banner on Node versions it hasn't been tested with (local Node is 26).
os.environ.setdefault("JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION", "1")

import aws_cdk as cdk  # noqa: E402

from nebula_stack import NebulaStack  # noqa: E402

# Deploys to the account and region of your AWS credentials (set NEBULA_AWS_REGION to choose a region).
ACCOUNT = os.environ.get("CDK_DEFAULT_ACCOUNT")
REGION = os.environ.get("NEBULA_AWS_REGION") or os.environ.get("CDK_DEFAULT_REGION") or "us-east-2"

app = cdk.App()

NebulaStack(
    app,
    "Nebula",
    env=cdk.Environment(account=ACCOUNT, region=REGION),
    description="Nebula: Rocket Learning's knowledge service for Claude (MCP server, workers, Aurora, S3)",
)

cdk.Tags.of(app).add("project", "nebula")

app.synth()
