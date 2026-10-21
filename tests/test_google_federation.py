"""Keyless Google auth, including refreshable ECS session credentials."""

from types import SimpleNamespace

import pytest
from google.auth import aws
from google.auth.exceptions import RefreshError

from nebula.config import settings
from nebula.connectors import aws_google, gdrive


def info():
    return {
        "type": "external_account",
        "audience": "//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/test/providers/aws",
        "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
        "token_url": "https://sts.googleapis.com/v1/token",
        "service_account_impersonation_url":
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/test@example.iam.gserviceaccount.com:generateAccessToken",
    }


def test_supplier_refreshes_aws_credentials():
    class Session:
        calls = 0

        def get_credentials(self):
            self.calls += 1
            value = SimpleNamespace(access_key=f"key-{self.calls}", secret_key="secret", token="temporary")
            return SimpleNamespace(get_frozen_credentials=lambda: value)

    session = Session()
    supplier = aws_google.TaskRoleSupplier("us-east-2", session=session)
    assert supplier.get_aws_region(None, None) == "us-east-2"
    assert supplier.get_aws_security_credentials(None, None).access_key_id == "key-1"
    refreshed = supplier.get_aws_security_credentials(None, None)
    assert refreshed.access_key_id == "key-2"
    assert refreshed.session_token == "temporary"


def test_supplier_fails_without_workload_credentials():
    supplier = aws_google.TaskRoleSupplier("us-east-2", session=SimpleNamespace(get_credentials=lambda: None))
    with pytest.raises(RefreshError, match="No AWS workload"):
        supplier.get_aws_security_credentials(None, None)


def test_federation_uses_supplier_not_ec2_metadata(monkeypatch):
    monkeypatch.setattr(aws_google.boto3, "Session", lambda **kwargs: object())
    config = {**info(), "credential_source": {"url": "http://example.invalid/never-fetch"}}
    credentials = aws_google.federated_credentials(config, region="us-east-2", scopes=[gdrive.SCOPE])
    assert isinstance(credentials, aws.Credentials)
    assert credentials.service_account_email == "test@example.iam.gserviceaccount.com"
    assert credentials.has_scopes([gdrive.SCOPE])


def test_federation_rejects_other_token_types():
    with pytest.raises(ValueError, match="AWS workload"):
        aws_google.federated_credentials({**info(), "subject_token_type": "id_token"},
                                        region="us-east-2", scopes=[gdrive.SCOPE])


def test_drive_loads_federation_and_separates_write_scope(monkeypatch):
    import json

    monkeypatch.setenv("NEBULA_GOOGLE_SERVICE_ACCOUNT_JSON", json.dumps(info()))
    monkeypatch.setattr(aws_google.boto3, "Session", lambda **kwargs: object())
    settings.cache_clear()
    try:
        read = gdrive.load_credentials()
        write = gdrive.load_credentials(write=True)
        assert read.has_scopes([gdrive.SCOPE])
        assert not read.has_scopes(["https://www.googleapis.com/auth/drive"])
        assert write.has_scopes(["https://www.googleapis.com/auth/drive"])
        monkeypatch.setenv("NEBULA_GOOGLE_IMPERSONATE_EMAIL", "person@example.com")
        settings.cache_clear()
        with pytest.raises(ValueError, match="Domain-wide"):
            gdrive.load_credentials()
    finally:
        settings.cache_clear()
