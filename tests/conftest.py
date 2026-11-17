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


class FakeClock:
    def __init__(self):
        import time

        # Slightly ahead of wall time, so events received "now" are already due.
        self.now = time.time() + 5

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ScriptedTransport:
    """httpx transport that forwards to the in-app mock order service via TestClient,
    optionally injecting transport-level failures that TestClient cannot produce:

    - "timeout": raise ReadTimeout without reaching the order service
    - "timeout_after_commit": forward (the update is applied), then raise ReadTimeout
    - "network": raise ConnectError
    Anything else (or an empty script) forwards normally.
    """

    def __init__(self, test_client):
        import httpx

        self._httpx = httpx
        self.tc = test_client
        self.script: list[str] = []
        self.calls = 0

    def handle_request(self, request):
        httpx = self._httpx
        self.calls += 1
        action = self.script.pop(0) if self.script else "forward"
        if action == "timeout":
            raise httpx.ReadTimeout("simulated timeout", request=request)
        if action == "network":
            raise httpx.ConnectError("simulated connection refused", request=request)
        resp = self.tc.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        if action == "timeout_after_commit":
            raise httpx.ReadTimeout("simulated timeout after commit", request=request)
        return httpx.Response(resp.status_code, headers=resp.headers, content=resp.content)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def transport(client):
    return ScriptedTransport(client)


@pytest.fixture
def worker(settings, transport, clock):
    import httpx

    from app.worker import OrderApiClient, Worker

    http = httpx.Client(transport=httpx.MockTransport(transport.handle_request))
    api = OrderApiClient("http://testserver/mock-oms", settings.order_api_timeout_seconds, http)
    return Worker(settings, api, clock=clock)
