"""One-app Manus-style workspace API; mounts the bundled frontend at /.

Production requirement: MANUS_APP_TOKEN >=24 chars, persistent MANUS_DATA_DIR,
Docker-capable workspace host and an authorized model provider. No fake results.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from manus_app.runtime import SandboxExecutor, capabilities
from manus_app.store import Store, uid
from manus_app.remote_files import load_file_store


STATIC = Path(__file__).parent / "static"


class TaskPayload(BaseModel):
    prompt: str = Field(min_length=2, max_length=30000)
    agent_id: str | None = None
    project_id: str | None = None


class ProjectPayload(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    description: str = Field(default="", max_length=1200)


class AgentPayload(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    instructions: str = Field(min_length=4, max_length=8000)


class AutomationPayload(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    prompt: str = Field(min_length=2, max_length=30000)
    interval_minutes: int = Field(ge=5, le=60 * 24 * 30)
    project_id: str | None = None
    agent_id: str | None = None


class PinPayload(BaseModel):
    pinned: bool


class Executor(Protocol):
    async def run(self, prompt: str, agent_instructions: str, event):
        ...

    async def cancel(self):
        ...


def create_app(
    data_dir: str | Path | None = None,
    app_token: str | None = None,
    executor_factory=None,
    scheduler_enabled: bool = True,
) -> FastAPI:
    root = Path(data_dir or os.getenv("MANUS_DATA_DIR", "./.manus-data"))
    root.mkdir(parents=True, exist_ok=True)
    (root / "files").mkdir(exist_ok=True)
    dsn = os.getenv("MANUS_DATABASE_URL")
    remote_files = load_file_store()
    if dsn:
        from manus_app.pg_store import PostgresStore
        if remote_files is None:
            raise ValueError("Remote database requires durable Supabase Storage")
        db = PostgresStore(dsn)
    else:
        if os.getenv("MANUS_DURABLE_REQUIRED") == "1":
            raise ValueError("Durable mode requires MANUS_DATABASE_URL and Supabase Storage")
        db = Store(str(root / "workspace.sqlite3"))
    token = app_token if app_token is not None else os.getenv("MANUS_APP_TOKEN", "")
    if token and len(token) < 24:
        raise ValueError("MANUS_APP_TOKEN must be at least 24 characters")
    if not token and os.getenv("MANUS_PUBLIC_MODE") == "1":
        raise ValueError("Public deployment requires MANUS_APP_TOKEN")
    factory = executor_factory or SandboxExecutor
    running: dict[str, tuple[asyncio.Task, Executor]] = {}
    gate = asyncio.Semaphore(1)

    @asynccontextmanager
    async def lifespan(application):
        db.recover()
        schedule = asyncio.create_task(scheduler()) if scheduler_enabled else None
        application.state.scheduler_task = schedule
        try:
            yield
        finally:
            if schedule:
                schedule.cancel()
                try:
                    await schedule
                except asyncio.CancelledError:
                    pass
            jobs = list(running.values())
            for task, _ in jobs:
                task.cancel()
            if jobs:
                await asyncio.gather(*(job for job, _ in jobs), return_exceptions=True)

    app = FastAPI(
        title="Manus Workspace",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.store = db
    app.state.running = running
    app.state.scheduler_task = None

    def auth(request: Request, authorization: str | None = Header(default=None)):
        if not token:
            # No token is safe only for local development.
            peer = request.client.host if request.client else ""
            if peer not in ("127.0.0.1", "::1", "testclient"):
                raise HTTPException(
                    status_code=503, detail="Public access requires MANUS_APP_TOKEN"
                )
            return
        provided = authorization or ""
        candidate = provided[7:] if provided.startswith("Bearer ") else ""
        if not hmac.compare_digest(candidate, token):
            raise HTTPException(
                status_code=401, detail="Unlock this workspace with its access token"
            )

    def exists(table: str, id: str | None):
        if id and not db.one(table, id):
            raise HTTPException(404, f"{table} not found")

    async def execute_task(task_id: str):
        async with gate:
            current = db.one("tasks", task_id)
            if not current or current["status"] == "cancelled":
                return
            executor = factory()
            artifact_dir = root / "artifacts" / task_id
            artifact_dir.mkdir(parents=True, exist_ok=True)
            if hasattr(executor, "set_artifact_dir"):
                executor.set_artifact_dir(artifact_dir)
            if hasattr(executor, "set_input_dir"):
                input_dir = root / "task-inputs" / task_id
                input_dir.mkdir(parents=True, exist_ok=True)
                for item in db.all("files"):
                    if item["project_id"] not in (None, current["project_id"]):
                        continue
                    src = root / "files" / item["id"]
                    if src.is_file():
                        shutil.copyfile(
                            src,
                            input_dir / (item["id"] + "_" + Path(item["name"]).name),
                        )
                executor.set_input_dir(input_dir)
            running[task_id] = (asyncio.current_task(), executor)
            agent = db.one("agents", current["agent_id"] or "default") or db.one(
                "agents", "default"
            )
            try:
                db.task_status(task_id, "running")

                def report(msg: str):
                    db.event(task_id, "progress", msg)

                result = await executor.run(
                    current["prompt"], agent["instructions"], report
                )
                # Do not report complete if cancellation beat the worker return.
                if db.one("tasks", task_id)["status"] != "cancelled":
                    db.task_status(task_id, "completed", result=str(result)[:200000])
            except asyncio.CancelledError:
                if db.one("tasks", task_id)["status"] != "cancelled":
                    db.task_status(
                        task_id, "interrupted", error="Agent execution was interrupted"
                    )
                raise
            except Exception as ex:
                if db.one("tasks", task_id)["status"] != "cancelled":
                    db.task_status(task_id, "failed", error=str(ex)[:400])
            finally:
                # Preserve generated workspace outputs without following agent-created
                # symlinks or accepting oversized/unbounded files.
                try:
                    count = 0
                    for item in artifact_dir.rglob("*"):
                        if count >= 50:
                            break
                        if item.is_symlink() or not item.is_file():
                            continue
                        if not item.resolve().is_relative_to(artifact_dir.resolve()):
                            continue
                        if item.stat().st_size > 20 * 1024 * 1024:
                            continue
                        file_id = uid("file")
                        if remote_files:
                            remote_files.put(file_id, item.read_bytes())
                        else:
                            shutil.copyfile(item, root / "files" / file_id)
                        file = db.add_file(
                            str(item.relative_to(artifact_dir))[:180],
                            item.stat().st_size,
                            "application/octet-stream",
                            current["project_id"],
                            id=file_id,
                        )
                        db.event(
                            task_id, "artifact", "Saved " + file["name"] + " to Library"
                        )
                        count += 1
                except OSError:
                    db.event(
                        task_id, "warning", "Some task artifacts could not be saved"
                    )
                running.pop(task_id, None)

    def submit(payload: TaskPayload):
        exists("agents", payload.agent_id)
        exists("projects", payload.project_id)
        task = db.create_task(
            payload.prompt.strip(), payload.project_id, payload.agent_id
        )
        job = asyncio.create_task(execute_task(task["id"]))
        # Keep a strong reference until the worker starts (also catches queued tasks).
        running[task["id"]] = (job, None)
        return task

    async def scheduler():
        while True:
            await asyncio.sleep(10)
            for item in db.pop_due():
                submit(
                    TaskPayload(
                        prompt=item["prompt"],
                        project_id=item["project_id"],
                        agent_id=item["agent_id"],
                    )
                )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "manus-workspace"}

    @app.get("/api/bootstrap", dependencies=[Depends(auth)])
    def bootstrap():
        return {
            "tasks": db.all("tasks", "updated DESC")[:100],
            "projects": db.all("projects"),
            "agents": db.all("agents"),
            "files": db.all("files"),
            "automations": db.all("automations"),
            "computer": capabilities(),
        }

    @app.post("/api/tasks", status_code=202, dependencies=[Depends(auth)])
    async def create_task(payload: TaskPayload):
        return submit(payload)

    @app.get("/api/tasks/{id}", dependencies=[Depends(auth)])
    def get_task(id: str):
        data = db.one("tasks", id)
        if not data:
            raise HTTPException(404, "Task not found")
        return data

    @app.get("/api/tasks/{id}/events", dependencies=[Depends(auth)])
    def get_events(id: str, last: int = 0):
        exists("tasks", id)
        return db.events(id, last)

    @app.post("/api/tasks/{id}/stop", dependencies=[Depends(auth)])
    async def stop_task(id: str):
        data = db.one("tasks", id)
        if not data:
            raise HTTPException(404, "Task not found")
        if data["status"] not in ("running", "queued"):
            return data
        cancelled = db.task_status(id, "cancelled")
        pair = running.get(id)
        if pair:
            job, executor = pair
            if executor is not None:
                await executor.cancel()
            job.cancel()
        return cancelled

    @app.post("/api/tasks/{id}/retry", status_code=202, dependencies=[Depends(auth)])
    async def retry_task(id: str):
        record = db.one("tasks", id)
        if not record:
            raise HTTPException(404, "Task not found")
        return submit(
            TaskPayload(
                prompt=record["prompt"],
                project_id=record["project_id"],
                agent_id=record["agent_id"],
            )
        )

    @app.post("/api/projects", dependencies=[Depends(auth)])
    def create_project(payload: ProjectPayload):
        return db.create_project(payload.name.strip(), payload.description)

    @app.post("/api/agents", dependencies=[Depends(auth)])
    def create_agent(payload: AgentPayload):
        return db.create_agent(payload.name.strip(), payload.instructions)

    @app.post("/api/pins/{kind}/{id}", dependencies=[Depends(auth)])
    def pin(kind: str, id: str, payload: PinPayload):
        if kind not in ("tasks", "projects", "agents"):
            raise HTTPException(404, "Unknown item type")
        try:
            return db.pin(kind, id, payload.pinned)
        except KeyError:
            raise HTTPException(404, "Item not found")

    @app.post("/api/automations", dependencies=[Depends(auth)])
    def create_automation(payload: AutomationPayload):
        exists("agents", payload.agent_id)
        exists("projects", payload.project_id)
        return db.automation(
            payload.name,
            payload.prompt,
            payload.interval_minutes,
            payload.project_id,
            payload.agent_id,
        )

    @app.post("/api/automations/{id}/toggle", dependencies=[Depends(auth)])
    def toggle(id: str, enabled: bool = True):
        try:
            return db.toggle_automation(id, enabled)
        except KeyError:
            raise HTTPException(404, "Automation not found")

    @app.post("/api/files", dependencies=[Depends(auth)])
    async def upload_file(file: UploadFile = File(...), project_id: str | None = None):
        exists("projects", project_id)
        content = await file.read(20 * 1024 * 1024 + 1)
        if len(content) > 20 * 1024 * 1024:
            raise HTTPException(413, "Maximum file size is 20 MB")
        if not file.filename:
            raise HTTPException(422, "Filename is required")
        file_id = uid("file")
        if remote_files:
            remote_files.put(file_id, content)
        else:
            (root / "files" / file_id).write_bytes(content)
        return db.add_file(
            Path(file.filename).name[:180], len(content),
            file.content_type or "application/octet-stream", project_id, id=file_id
        )

    @app.get("/api/files/{id}", dependencies=[Depends(auth)])
    def get_file(id: str):
        record = db.one("files", id)
        if not record:
            raise HTTPException(404, "File not found")
        if remote_files:
            from fastapi.responses import Response
            data = remote_files.get(id)
            from urllib.parse import quote
            return Response(data, media_type="application/octet-stream",
                            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(record["name"])})
        return FileResponse(
            root / "files" / id,
            media_type="application/octet-stream",
            filename=record["name"],
        )

    @app.get("/api/computers", dependencies=[Depends(auth)])
    def computers():
        cap = capabilities()
        return {
            "computers": [
                {
                    "id": "default",
                    "name": "Cloud Computer",
                    "status": "ready" if cap["ready"] else "offline",
                    "description": cap["reason"],
                }
            ],
            "no_selection_required": True,
        }

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/assets", StaticFiles(directory=STATIC), name="assets")
    return app


app = create_app()
