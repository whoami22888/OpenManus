# My Manus Workspace — integrated application for OpenManus

This is **one application**, not an additional Android task tab. It uses the existing OpenManus agent, planning flow and Docker sandbox for execution and provides a mobile-first app for projects, tasks, saved agents, automations, a library, task progress and the cloud computer status.

## Launch

Install OpenManus requirements plus `requirements-workspace.txt`. In the **OpenManus repository root**:

```bash
python -m pip install -r requirements.txt -r requirements-workspace.txt
# Local development (localhost only); does not require a login.
python -m uvicorn manus_app.server:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. On Android Chrome, add the cloud URL to the home screen for full-screen PWA behavior. It always opens directly to Tasks, never a server/model/agent selection panel.

For an internet-accessible deployment, configure at service provisioning time:

```text
MANUS_PUBLIC_MODE=1
MANUS_APP_TOKEN=<random-long-secret>
MANUS_DATA_DIR=/persistent/workspace
OPENROUTER_API_KEY=<valid-operator-authorized-key>
MANUS_MODEL=openrouter/free
```

The server-side agent automatically chooses this model and the default agent; users enter tasks immediately. Existing OpenManus `config/config.example.toml` is available as a secondary configuration. Model calls require valid authorized access and are subject to free provider quotas. No modded APK provides unlimited cloud rights.

## Cloud computers

A Docker-capable cloud host with access to an isolated Docker daemon/socket is needed. The runtime uses the **existing** `app.sandbox.core.sandbox.DockerSandbox` and **existing** `app.agent.manus.Manus`, but replaces host-level tools with Docker-container shell, file and browser tools. There is deliberately **no host subprocess fallback** for cloud execution. Resource limits default to 1 vCPU and 1024 MB per computer, and one active job at a time. Commands and browser pages run inside the task container; browser output returns page text. Advanced computer screen streaming/VNC is not yet implemented.

Do not mount an unrestricted host Docker socket into a publicly exposed shared application without a hardened worker boundary; run this in a trusted single-operator environment, restricted network and container user, with separate egress policies. The current browser tool checks URL scheme and obvious IP targets but is **not a complete SSRF protection boundary**; a hardened proxy plus redirect/DNS controls are prerequisites for processing untrusted content.

Render **free** web services cannot create these Docker workers or supply persistent disk. Deploying this exact application as a full autonomous computer on that plan would give a nonworking sandbox; do not pretend otherwise. The app still runs the dashboard and state APIs on a suitable host, but free host uptime/storage restrictions still apply.

## Working features

- Task submission, queue, execution status, progress events, retry and stop.
- Persistent projects, saved agents with per-agent instructions, pinned items, library upload/download.
- Recurring tasks created and managed in the app (runs only while the server is awake).
- Automatic backend selection for model/agent/computer — no chooser in the default workflow.
- One installable mobile PWA UI modeled on the provided Manus home screenshot.
- SQLite persistence with explicit restart recovery; per-project task grouping.
- Fail-closed public deployment when no application token is configured.

## Remaining acceptance gaps

- **No actual paid or free cloud computer is deployed** by this source change. Render free has no Docker worker host or persistent disk.
- No live provider-key setup/real agent invocation proved in CI (the tests use a deterministic fake agent).
- Computer screen stream (VNC/noVNC), external app connection gallery, granular file-browser tree, background task guarantees, multi-user auth, and packaged Play Store APK remain future milestones.
- Free inference models have rate, context, and availability limits. Unlimited on someone else's modded APK backend is not provided.

## Tests

`python -m pytest tests/test_workspace.py -q`

All API regression tests use real HTTP requests to FastAPI and a deterministic injected executor. They check real storage and state changes but are **not** cloud E2E or physical Android tests.
