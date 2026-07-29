import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.source_candidate_probe import SourceCandidateProbe


TEST_TOKEN = "test-only-bridge-token-with-at-least-256-bits-000000"


@pytest.fixture()
def token():
    return TEST_TOKEN


@pytest.fixture()
def client(token):
    with TestClient(
        create_app(
            token,
            fixture_mode=True,
            allow_test_client=True,
            source_candidate_probe=SourceCandidateProbe(None),
        )
    ) as test_client:
        yield test_client


@pytest.fixture()
def auth(token):
    return {"Authorization": f"Bearer {token}"}
