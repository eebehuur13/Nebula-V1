"""Short-lived Google credentials backed by the ECS task role, without a private key."""

from __future__ import annotations

from typing import Any

import boto3
from google.auth import aws
from google.auth.exceptions import RefreshError


class TaskRoleSupplier(aws.AwsSecurityCredentialsSupplier):
    """Use boto3's refreshable credential chain, including the Fargate container provider."""

    def __init__(self, region: str, *, session: Any = None):
        self.region = region
        self.session = session if session is not None else boto3.Session(region_name=region)

    def get_aws_region(self, context, request) -> str:
        return self.region

    def get_aws_security_credentials(self, context, request) -> aws.AwsSecurityCredentials:
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RefreshError("No AWS workload credentials are available for Google federation.")
        current = credentials.get_frozen_credentials()
        return aws.AwsSecurityCredentials(current.access_key, current.secret_key, current.token)


def federated_credentials(info: dict, *, region: str, scopes: list[str]) -> aws.Credentials:
    if info.get("subject_token_type") != "urn:ietf:params:aws:token-type:aws4_request":
        raise ValueError("Google federation must use an AWS workload identity.")
    # Deliberately use the AWS SDK rather than EC2-only metadata URLs from generated configs.
    config = {key: value for key, value in info.items() if key != "credential_source"}
    return aws.Credentials.from_info(
        config, scopes=scopes, aws_security_credentials_supplier=TaskRoleSupplier(region)
    )
