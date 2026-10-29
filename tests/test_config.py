import os
import subprocess
import sys

from app import config


def import_config(**env):
    base = {k: v for k, v in os.environ.items() if not k.startswith("WORKLIST_") and k != "RENDER"}
    return subprocess.run([sys.executable, "-c", "import app.config"], cwd=config.ROOT,
                          env={**base, **env}, capture_output=True, text=True)


def test_render_without_secrets_fails_instead_of_using_dev_defaults():
    r = import_config(RENDER="true", WORKLIST_SESSION_SECRET="s")
    assert r.returncode != 0
    assert "WORKLIST_AGENT_TOKEN" in r.stderr


def test_render_with_secrets_and_local_without_them_both_start():
    assert import_config(RENDER="true", WORKLIST_AGENT_TOKEN="t", WORKLIST_SESSION_SECRET="s").returncode == 0
    assert import_config().returncode == 0
