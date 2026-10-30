"""Run one remote OpenManus task on an ephemeral GitHub-hosted VM."""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path

import httpx

from manus_app.runtime import SandboxExecutor


MAX = 20 * 1024 * 1024


async def run_job():
    endpoint = os.getenv("MANUS_SERVICE_URL", "").rstrip("/")
    task_id = os.getenv("MANUS_TASK_ID", "")
    token = os.getenv("MANUS_WORKER_TOKEN", "")
    if (
        not endpoint.startswith("https://")
        or not re.fullmatch(r"tsk_[0-9a-f]{32}", task_id)
        or len(token) < 24
    ):
        raise RuntimeError(
            "HTTPS endpoint, valid task ID and worker secret are required"
        )
    path = f"/internal/worker/jobs/{task_id}"
    async with httpx.AsyncClient(
        base_url=endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=45
    ) as client:

        async def request(method, uri, **kwargs):
            response = await client.request(method, uri, **kwargs)
            response.raise_for_status()
            return response

        task = (await request("GET", path)).json()
        if task["status"] != "queued":
            raise RuntimeError("Task already started or cancelled")
        await request("POST", path + "/status", json={"status": "running"})
        with tempfile.TemporaryDirectory(prefix="manus_cloud_job_") as folder:
            inputs, outputs = Path(folder) / "input", Path(folder) / "output"
            inputs.mkdir()
            outputs.mkdir()
            for item in (await request("GET", path + "/files")).json()[:100]:
                fid = item["id"]
                if not re.fullmatch(r"file_[0-9a-f]{32}", fid):
                    continue
                body = (await request("GET", path + "/files/" + fid)).content
                if len(body) <= MAX:
                    (inputs / (fid + "_" + Path(item["name"]).name)).write_bytes(body)
            executor = SandboxExecutor()
            executor.set_input_dir(inputs)
            executor.set_artifact_dir(outputs)
            events = asyncio.Queue()

            def report(message):
                events.put_nowait(str(message)[:3500])

            job = asyncio.create_task(
                executor.run(task["prompt"], task["agent_instructions"], report)
            )
            deadline = asyncio.get_running_loop().time() + 1500
            try:
                while not job.done():
                    while not events.empty():
                        await request(
                            "POST",
                            path + "/events",
                            json={"message": events.get_nowait()},
                        )
                    await asyncio.sleep(3)
                    if (await request("GET", path)).json()["status"] == "cancelled":
                        job.cancel()
                        await executor.cancel()
                        raise RuntimeError("Cloud task cancelled")
                    if asyncio.get_running_loop().time() > deadline:
                        job.cancel()
                        await executor.cancel()
                        raise TimeoutError("Cloud task exceeded 25 minutes")
                result = await job
                while not events.empty():
                    await request(
                        "POST", path + "/events", json={"message": events.get_nowait()}
                    )
                count = 0
                for artifact in outputs.rglob("*"):
                    if count >= 50:
                        break
                    if artifact.is_symlink() or not artifact.is_file():
                        continue
                    if not artifact.resolve().is_relative_to(outputs.resolve()):
                        continue
                    if artifact.stat().st_size > MAX:
                        continue
                    await request(
                        "POST",
                        path + "/artifacts",
                        files={
                            "file": (
                                artifact.name,
                                artifact.read_bytes(),
                                "application/octet-stream",
                            )
                        },
                    )
                    count += 1
                await request(
                    "POST",
                    path + "/status",
                    json={"status": "completed", "result": str(result)[:200000]},
                )
            except BaseException as error:
                try:
                    if (await request("GET", path)).json()["status"] != "cancelled":
                        await request(
                            "POST",
                            path + "/status",
                            json={
                                "status": "failed",
                                "error": f"{type(error).__name__}: {str(error)[:250]}",
                            },
                        )
                except httpx.HTTPError:
                    pass
                raise
            finally:
                if not job.done():
                    job.cancel()
                await executor.cancel()


if __name__ == "__main__":
    asyncio.run(run_job())
