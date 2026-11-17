import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

TEST_SECRET = "whsec_test_secret_for_unit_tests"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        stripe_webhook_secret=TEST_SECRET,
        database_path=str(tmp_path / "test.db"),
        order_api_base_url="http://oms.test",
        worker_enabled=False,  # tests drive the worker explicitly
        max_attempts=3,
        retry_base_seconds=1,
        retry_max_seconds=10,
        retry_jitter=False,
    )


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c
