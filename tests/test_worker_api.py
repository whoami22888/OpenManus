"""Task queue -> private GitHub dispatcher -> authenticated worker -> persisted result.

No billable workflow or real provider is invoked by these tests.
"""
import pytest
from fastapi.testclient import TestClient

import manus_app.pg_store as pg_store
from manus_app import server
from manus_app.store import Store


TOKEN = "test-manus-access-token-123456789"
WORKER = "test-private-worker-token-123456789"


class FakeDispatcher:
    def __init__(self):
        self.ids = []

    async def dispatch(self, task_id):
        self.ids.append(task_id)


class FakeFiles:
    def __init__(self):
        self.values = {}

    def put(self, file_id, data):
        self.values[file_id] = data

    def get(self, file_id):
        return self.values[file_id]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    dispatcher = FakeDispatcher()
    files = FakeFiles()
    monkeypatch.setenv("MANUS_DATABASE_URL", "mock-postgres")
    monkeypatch.setenv("MANUS_WORKER_TOKEN", WORKER)
    monkeypatch.setattr(server, "get_dispatcher", lambda: dispatcher)
    monkeypatch.setattr(server, "load_file_store", lambda: files)
    monkeypatch.setattr(
        pg_store, "PostgresStore", lambda _: Store(str(tmp_path / "pgmock.sqlite"))
    )
    app = server.create_app(data_dir=tmp_path, app_token=TOKEN, scheduler_enabled=False)
    with TestClient(app) as client:
        yield client, dispatcher, files


def test_private_worker_can_complete_persisted_task(configured):
    client, dispatcher, files = configured
    client_headers = {"Authorization": "Bearer " + TOKEN}
    worker_headers = {"Authorization": "Bearer " + WORKER}
    task_response = client.post(
        "/api/tasks", headers=client_headers, json={"prompt": "Build a web app"}
    )
    assert task_response.status_code == 202
    task = task_response.json()
    assert dispatcher.ids == [task["id"]]
    assert task["status"] == "queued"
    job_path = "/internal/worker/jobs/" + task["id"]
    assert client.get(job_path).status_code == 401
    payload = client.get(job_path, headers=worker_headers).json()
    assert payload["prompt"] == "Build a web app"
    assert payload["agent_instructions"]
    assert (
        client.post(
            job_path + "/status",
            headers=worker_headers,
            json={"status": "completed", "result": "illegal skip"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            job_path + "/status", headers=worker_headers, json={"status": "running"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            job_path + "/events",
            headers=worker_headers,
            json={"message": "Browser opened"},
        ).status_code
        == 200
    )
    saved = client.post(
        job_path + "/artifacts",
        headers=worker_headers,
        files={"file": ("result.md", b"# Result", "text/markdown")},
    )
    assert saved.status_code == 200
    file_id = saved.json()["id"]
    assert files.get(file_id) == b"# Result"
    assert (
        client.get("/api/files/" + file_id, headers=client_headers).content
        == b"# Result"
    )
    assert (
        client.post(
            job_path + "/status",
            headers=worker_headers,
            json={"status": "completed", "result": "Finished"},
        ).status_code
        == 200
    )
    record = client.get("/api/tasks/" + task["id"], headers=client_headers).json()
    assert record["status"] == "completed"
    assert record["result"] == "Finished"
    assert (
        client.post(
            job_path + "/status",
            headers=worker_headers,
            json={"status": "completed", "result": "again"},
        ).status_code
        == 409
    )


def test_cancelled_task_cannot_be_revived_by_worker(configured):
    client, dispatcher, _ = configured
    client_headers = {"Authorization": "Bearer " + TOKEN}
    worker_headers = {"Authorization": "Bearer " + WORKER}
    task = client.post(
        "/api/tasks", headers=client_headers, json={"prompt": "Test cancellation"}
    ).json()
    job_path = "/internal/worker/jobs/" + task["id"]
    assert (
        client.post(
            "/api/tasks/" + task["id"] + "/stop", headers=client_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            job_path + "/status", headers=worker_headers, json={"status": "running"}
        ).status_code
        == 409
    )


def test_hourly_wakeup_dispatches_due_automation(configured, monkeypatch):
    client, dispatcher, _ = configured
    monkeypatch.setenv("MANUS_GITHUB_NO_CHARGE_VERIFIED", "1")
    owner = {"Authorization": "Bearer " + TOKEN}
    worker = {"Authorization": "Bearer " + WORKER}
    automation = client.post(
        "/api/automations",
        headers=owner,
        json={
            "name": "Hourly research",
            "prompt": "Research topic",
            "interval_minutes": 60,
        },
    ).json()
    store = client.app.state.store
    with store._db() as db:
        db.execute("UPDATE automations SET next_run=0 WHERE id=?", (automation["id"],))
    response = client.post("/internal/worker/wake", headers=worker)
    assert response.status_code == 200
    assert response.json()["dispatched"] == 1
    assert len(dispatcher.ids) == 1
    assert (
        client.post("/internal/worker/wake", headers=worker).json()["dispatched"] == 0
    )
