"""Deployment regressions. Run from infra with: uv run python -m unittest -q."""

import json
import unittest
from pathlib import Path

import aws_cdk as cdk
from aws_cdk import assertions, aws_cloudfront as cloudfront

from nebula_stack import NebulaStack


# A made-up account, with the two lookups the stack makes answered in advance (us-east-2's zones and
# AWS's public CloudFront origin-facing prefix list), so the test runs without AWS credentials.
TEST_ACCOUNT = "123456789012"
TEST_LOOKUPS = {
    f"availability-zones:account={TEST_ACCOUNT}:region=us-east-2": ["us-east-2a", "us-east-2b", "us-east-2c"],
    f"cc-api-provider:account={TEST_ACCOUNT}:expectedMatchCount=exactly-one:propertiesToReturn.0=PrefixListId:"
    "propertyMatch.PrefixListName=com.amazonaws.global.cloudfront.origin-facing:region=us-east-2:"
    "typeName=AWS$:$:EC2$:$:PrefixList": [{"PrefixListId": "pl-b6a144df", "Identifier": "pl-b6a144df"}],
}


class CloudFrontTransportTest(unittest.TestCase):
    def test_preserves_mcp_host_and_does_not_cache_authenticated_responses(self):
        root = Path(__file__).parent
        context = json.loads((root / "cdk.json").read_text())["context"]
        context.update(TEST_LOOKUPS)
        app = cdk.App(context=context)
        stack = NebulaStack(
            app, "TransportTest",
            env=cdk.Environment(account=TEST_ACCOUNT, region="us-east-2"),
        )
        template = assertions.Template.from_stack(stack)
        template.has_resource_properties(
            "AWS::CloudFront::Distribution",
            {"DistributionConfig": {"DefaultCacheBehavior": {
                "OriginRequestPolicyId": cloudfront.OriginRequestPolicy.ALL_VIEWER.origin_request_policy_id,
                "CachePolicyId": cloudfront.CachePolicy.CACHING_DISABLED.cache_policy_id,
                "ViewerProtocolPolicy": "redirect-to-https",
            }}},
        )
        tasks = template.find_resources("AWS::ECS::TaskDefinition")
        self.assertEqual(len(tasks), 2)
        for task in tasks.values():
            env = {v["Name"]: v["Value"] for v in task["Properties"]["ContainerDefinitions"][0]["Environment"]}
            self.assertEqual(env["NEBULA_PUBLIC_SIGNUP"], "false")
            self.assertEqual(env["NEBULA_ALLOWED_EMAIL_DOMAINS"], "example.org")


if __name__ == "__main__":
    unittest.main()
