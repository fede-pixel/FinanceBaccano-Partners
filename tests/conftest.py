import os
import tempfile

# I dati dei test non devono mai toccare la cartella reale dell'utente.
os.environ["FBP_DATA_DIR"] = tempfile.mkdtemp(prefix="fbp-test-")

import pytest  # noqa: E402

from app import config  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app, bootstrap  # noqa: E402


@pytest.fixture()
def session(tmp_path):
    bootstrap(tmp_path / "test.db")
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    bootstrap(tmp_path / "test.db")
    app.state.bootstrapped = True
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def data_dir():
    return config.DATA_DIR
