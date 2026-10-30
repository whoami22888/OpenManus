"""Bounded remote GitHub Actions job dispatcher.

Only a PRIVATE, explicitly zero-charge-verified repository can host user tasks.
Task prompts and access keys are never sent in workflow_dispatch inputs.
"""
import os
import re

import httpx

TASK_ID = re.compile(r"^tsk_[0-9a-f]{32}$")
REPOSITORY = re.compile(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$")


class DispatchUnavailable(RuntimeError):
    pass


class GitHubActionsDispatcher:
    def __init__(self, token: str, repository: str, *, ref: str = "main",
                 workflow: str = "manus-agent-job.yml", client=None):
        if len(token) < 24 or not REPOSITORY.fullmatch(repository):
            raise DispatchUnavailable("Valid private runner repository and scoped token required")
        self.token = token
        self.repo = repository
        self.ref = ref
        self.workflow = workflow
        self.client = client

    async def dispatch(self, task_id: str) -> None:
        if not TASK_ID.fullmatch(task_id):
            raise DispatchUnavailable("Invalid task ID")
        if os.getenv("MANUS_GITHUB_NO_CHARGE_VERIFIED") != "1":
            raise DispatchUnavailable("GitHub runner billing must be verified at $0")
        headers = {"Authorization": f"Bearer {self.token}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        async def run(client):
            metadata = await client.get(f"https://api.github.com/repos/{self.repo}", headers=headers)
            if metadata.status_code != 200 or not metadata.json().get("private", False):
                raise DispatchUnavailable("Worker repository must be private and accessible")
            result = await client.post(
                f"https://api.github.com/repos/{self.repo}/actions/workflows/{self.workflow}/dispatches",
                headers=headers,
                json={"ref": self.ref, "inputs": {"task_id": task_id}},
            )
            if result.status_code != 204:
                raise DispatchUnavailable(f"Workflow dispatch failed (HTTP {result.status_code})")
        if self.client is not None:
            await run(self.client)
        else:
            async with httpx.AsyncClient(timeout=18) as client:
                await run(client)


def get_dispatcher():
    if os.getenv("MANUS_RUNNER_BACKEND") != "github":
        return None
    return GitHubActionsDispatcher(
        os.environ.get("MANUS_GITHUB_ACTIONS_TOKEN", ""),
        os.environ.get("MANUS_GITHUB_REPO", ""),
        workflow="manus-agent-job.yml"
    )
