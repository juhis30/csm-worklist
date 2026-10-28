import pytest

from app import config
from app.bootstrap import bootstrap


@pytest.fixture(scope="session")
def world_db(tmp_path_factory):
    """A smaller but complete world (30% of accounts) with two weekly runs, built once per session."""
    path = tmp_path_factory.mktemp("db") / "test.db"
    config.DB_PATH = path
    conn = bootstrap(path, catch_up=1, scale=0.3, quiet=True)
    yield conn
    conn.close()
