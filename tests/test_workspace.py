import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from manus_app.runtime import capabilities
from manus_app.server import create_app
from manus_app.store import Store


TOKEN = "test-operator-token-1234567890"
AUTH = {"Authorization": "Bearer " + TOKEN}


class FakeAgent:
    async def run(self, prompt, instructions, event):
        event("Planning work")
        await asyncio.sleep(0.03)
        return "Completed: " + prompt

    async def cancel(self):
        pass


@pytest.fixture
def app(tmp_path):
    return create_app(
        tmp_path, app_token=TOKEN, executor_factory=FakeAgent, scheduler_enabled=False
    )


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def test_health_not_sensitive(client):
    assert client.get("/health").status_code == 200
    assert client.get("/api/bootstrap").status_code == 401
    assert client.get("/api/bootstrap", headers=AUTH).status_code == 200


def test_auth_reject_wrong(client):
    assert (
        client.post(
            "/api/tasks",
            json={"prompt": "build project"},
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )


def test_default_agent_available(client):
    data = client.get("/api/bootstrap", headers=AUTH).json()
    assert data["agents"][0]["id"] == "default"
    assert "reason" in data["computer"]


def test_create_task_real_async_execution(client):
    t = client.post("/api/tasks", json={"prompt": "build a program"}, headers=AUTH)
    assert t.status_code == 202
    task_id = t.json()["id"]
    for _ in range(30):
        record = client.get("/api/tasks/" + task_id, headers=AUTH).json()
        if record["status"] == "completed":
            break
        time.sleep(0.015)
    assert record["status"] == "completed"
    assert record["result"] == "Completed: build a program"
    ev = client.get(f"/api/tasks/{task_id}/events", headers=AUTH).json()
    assert any("Planning" in x["message"] for x in ev)


def test_task_persists_between_requests(client, app):
    id = client.post(
        "/api/tasks", json={"prompt": "research a thing"}, headers=AUTH
    ).json()["id"]
    assert app.state.store.one("tasks", id)["prompt"] == "research a thing"


def test_project_agent_and_task_assignment(client):
    p = client.post(
        "/api/projects", json={"name": "Mobile client"}, headers=AUTH
    ).json()
    a = client.post(
        "/api/agents",
        json={"name": "Developer", "instructions": "Write and review code"},
        headers=AUTH,
    ).json()
    t = client.post(
        "/api/tasks",
        json={
            "prompt": "Implement mobile client",
            "project_id": p["id"],
            "agent_id": a["id"],
        },
        headers=AUTH,
    ).json()
    assert t["project_id"] == p["id"] and t["agent_id"] == a["id"]


def test_unknown_assignee_rejected(client):
    assert (
        client.post(
            "/api/tasks",
            json={"prompt": "valid task", "agent_id": "unknown"},
            headers=AUTH,
        ).status_code
        == 404
    )


def test_pin_round_trip(client):
    t = client.post("/api/tasks", json={"prompt": "make test"}, headers=AUTH).json()
    r = client.post(f"/api/pins/tasks/{t['id']}", json={"pinned": True}, headers=AUTH)
    assert r.status_code == 200 and r.json()["pinned"] == 1
    b = client.get("/api/bootstrap", headers=AUTH).json()
    assert any(x["id"] == t["id"] and x["pinned"] for x in b["tasks"])


def test_automations_are_saved_and_can_pause(client):
    data = client.post(
        "/api/automations",
        json={"name": "Hourly check", "prompt": "Check source", "interval_minutes": 60},
        headers=AUTH,
    ).json()
    assert data["enabled"] == 1
    paused = client.post(
        f"/api/automations/{data['id']}/toggle?enabled=false", headers=AUTH
    ).json()
    assert paused["enabled"] == 0


def test_scheduler_claim_is_atomic(tmp_path):
    store = Store(str(tmp_path / "db.sqlite"))
    item = store.automation("Daily", "Check source", 1440)
    with store._db() as db:
        db.execute("UPDATE automations SET next_run=0 WHERE id=?", (item["id"],))
    assert len(store.pop_due()) == 1
    assert len(store.pop_due()) == 0


def test_due_automation_submits_and_completes_task(tmp_path, monkeypatch):
    # Shorten only the scheduler's idle interval; exercise its real claim,
    # submission, executor, and persisted task result.
    original_sleep = asyncio.sleep

    async def scheduler_sleep(seconds):
        await original_sleep(0.01 if seconds == 10 else seconds)

    monkeypatch.setattr("manus_app.server.asyncio.sleep", scheduler_sleep)
    app = create_app(tmp_path, app_token=TOKEN, executor_factory=FakeAgent)
    item = app.state.store.automation("Due now", "Check source", 60)
    with app.state.store._db() as db:
        db.execute("UPDATE automations SET next_run=0 WHERE id=?", (item["id"],))
    with TestClient(app):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            tasks = app.state.store.all("tasks")
            if tasks and tasks[0]["status"] == "completed":
                break
            time.sleep(0.01)
        assert len(tasks) == 1
        assert tasks[0]["status"] == "completed"
        assert tasks[0]["result"] == "Completed: Check source"


def test_upload_download_and_filename_sanitized(client):
    r = client.post(
        "/api/files",
        files={"file": ("../outside.txt", b"hello", "text/plain")},
        headers=AUTH,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "outside.txt"
    assert client.get("/api/files/" + data["id"], headers=AUTH).content == b"hello"


def test_bad_file_or_task_id(client):
    assert client.get("/api/files/no", headers=AUTH).status_code == 404
    assert client.get("/api/tasks/no", headers=AUTH).status_code == 404


def test_oversize_file(client):
    r = client.post(
        "/api/files",
        files={
            "file": ("x.bin", b"a" * (20 * 1024 * 1024 + 1), "application/octet-stream")
        },
        headers=AUTH,
    )
    assert r.status_code == 413


def test_frontend_static_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "My Manus Workspace" in r.text
    assert "Computers" in r.text
    assert client.get("/assets/app.js").status_code == 200


def test_public_mode_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MANUS_PUBLIC_MODE", "1")
    with pytest.raises(ValueError):
        create_app(tmp_path, app_token="")


def test_runtime_reports_true_prerequisites():
    c = capabilities()
    assert isinstance(c["ready"], bool)
    assert c["mode"] == "OpenManus · Docker workspace"


def test_restart_does_not_mark_incomplete_done(tmp_path):
    s = Store(str(tmp_path / "db.sqlite"))
    t = s.create_task("background pending")
    s.recover()
    assert s.one("tasks", t["id"])["status"] == "interrupted"


def test_completed_task_generated_files_saved_in_library(tmp_path):
    class Writer:
        def set_artifact_dir(self, directory):
            self.directory = directory

        async def run(self, prompt, instructions, event):
            (self.directory / "output.md").write_text("# Created by agent")
            event("Created result")
            return "Done"

        async def cancel(self):
            pass

    app = create_app(
        tmp_path, app_token=TOKEN, executor_factory=Writer, scheduler_enabled=False
    )
    with TestClient(app) as c:
        t = c.post("/api/tasks", json={"prompt": "write output"}, headers=AUTH).json()
        for _ in range(100):
            if app.state.store.one("tasks", t["id"])[
                "status"
            ] == "completed" and app.state.store.all("files"):
                break
            time.sleep(0.01)
        files = c.get("/api/bootstrap", headers=AUTH).json()["files"]
        assert len(files) == 1
        assert files[0]["name"] == "output.md"
        assert (
            c.get("/api/files/" + files[0]["id"], headers=AUTH).content
            == b"# Created by agent"
        )


def test_recent_remote_job_survives_restart(tmp_path):
    store = Store(str(tmp_path / "remote.sqlite"))
    task = store.create_task("preserve active remote job")
    store.recover_stale(age_seconds=2700)
    assert store.one("tasks", task["id"])["status"] == "queued"


def test_expired_remote_job_interrupted(tmp_path):
    store = Store(str(tmp_path / "remote.sqlite"))
    task = store.create_task("remote job whose runner disappeared")
    with store._db() as db:
        db.execute("UPDATE tasks SET updated=0 WHERE id=?", (task["id"],))
    store.recover_stale(age_seconds=2700)
    assert store.one("tasks", task["id"])["status"] == "interrupted"
