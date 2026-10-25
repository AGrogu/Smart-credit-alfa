import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
(ROOT / "work").mkdir(exist_ok=True)
TEST_DIR = tempfile.TemporaryDirectory(prefix="pytest-", dir=ROOT / "work")
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or "sqlite:///" + str(Path(TEST_DIR.name) / "test.db")
os.environ["LOGIN_IP_LIMIT"] = "10000"
os.environ["LOGIN_ACCOUNT_LIMIT"] = "1000"
os.environ["SEED_DEMO"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    with TestClient(app) as client:
        assert client.get("/api/health/ready").status_code == 200
        yield


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def login(client, account="good"):
    response = client.post("/api/auth/login", json={"email": f"{account}@demo.local", "password": "Demo2026!"})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()


@pytest.fixture
def owner(client):
    login(client)
    return client
