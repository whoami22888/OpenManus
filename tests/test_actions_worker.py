import asyncio
import pytest
from manus_app.actions_worker import run_job


def test_missing_cloud_credentials_prevents_execution(monkeypatch):
    for key in ("MANUS_SERVICE_URL", "MANUS_WORKER_TOKEN", "MANUS_TASK_ID"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="HTTPS endpoint"):
        asyncio.run(run_job())


def test_worker_rejects_untrusted_task_id(monkeypatch):
    monkeypatch.setenv("MANUS_SERVICE_URL", "https://example.org")
    monkeypatch.setenv("MANUS_WORKER_TOKEN", "a"*32)
    monkeypatch.setenv("MANUS_TASK_ID", "../../secrets")
    with pytest.raises(RuntimeError, match="valid task ID"):
        asyncio.run(run_job())
