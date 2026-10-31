import os
import sys
import tempfile
from pathlib import Path

# app.database builds its engine at import time and get_settings() is cached,
# so the test DB must be configured before anything from app.* is imported.
_TMP = Path(tempfile.mkdtemp(prefix="videomaker-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'test.db'}"
os.environ["DATA_DIR"] = str(_TMP / "data")
os.environ["ASSETS_DIR"] = str(_TMP / "assets")
os.environ["OUTPUT_DIR"] = str(_TMP / "output")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
import app.models  # noqa: E402,F401


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def tmp_dir():
    return _TMP
