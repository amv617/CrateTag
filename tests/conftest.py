"""Keep the tests' activity log out of the real log folder."""
import os
import tempfile

import pytest

os.environ.setdefault("CRATETAG_LOG_DIR", tempfile.mkdtemp(prefix="cratetag-test-logs-"))


@pytest.fixture
def fresh_log(tmp_path, monkeypatch):
    from cratetag import activity
    monkeypatch.setenv("CRATETAG_LOG_DIR", str(tmp_path / "logs"))
    activity.reset()
    yield tmp_path / "logs" / "activity.log"
    activity.reset()
