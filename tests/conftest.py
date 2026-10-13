import os, re, pathlib
import pytest

import tempfile
DB = pathlib.Path(tempfile.gettempdir()) / "erp_test.db"
if DB.exists():
    DB.unlink()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{DB}")
os.environ["ADMIN_PASSWORD"] = "Admin#12345"
os.environ["LOAD_DEMO"] = "1"
os.environ["SECRET_KEY"] = "test-secret"

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.db import Base, engine  # noqa: E402

if engine.dialect.name != "sqlite":
    Base.metadata.drop_all(engine)  # clean schema for PostgreSQL runs


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


def csrf_of(client, path="/"):
    html = client.get(path).text
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else None


@pytest.fixture(scope="session", autouse=True)
def _boot(client):
    yield


@pytest.fixture(scope="session")
def admin(client):
    r = client.post("/login", data={"username": "admin", "password": "Admin#12345"}, follow_redirects=False)
    assert r.status_code == 303
    return client
