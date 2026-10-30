# Manus Hybrid: $0 coordination + durable state + isolated execution

This is the **single integrated OpenManus application**, not an EVE task form. The mobile Tasks/Agent UI submits jobs without asking the operator to choose a server, agent, or model.

## Components and real constraints

| Component | Implementation | $0 limits |
|---|---|---|
| Mobile dashboard & API | Existing `manus_app/server.py` on Render free web | Idles, sleeps, ephemeral scratch; no long-running Docker daemon |
| Persistent task/project/agent/automation ledger | `manus_app/pg_store.py`, dedicated private-schema Postgres via Supabase free | Two free active projects/account; inactivity pausing; size quotas |
| Generated files and uploaded assets | `manus_app/remote_files.py`, **private** Supabase Storage bucket | Storage/egress limits; cannot rely on Render scratch |
| Code and browser job execution | `manus_app/github_actions.py` + `manus_app/actions_worker.py` | Private Ubuntu runner with included monthly minutes; 25-minute agent job cap |
| Full interactive browser desktop | Existing `app/daytona/sandbox.py` / Docker infrastructure | **Not provided for free by this architecture** |
| Model inference | Existing OpenManus provider routing with legitimate configured free-model allowance | Provider quota, throttling and availability |

There is **no unlimited, always-on cloud VM or unlimited inference**. The GitHub runner is an ephemeral virtual machine per task, and no VNC/live-desktop session is exposed. A real end-to-end task still requires infrastructure credential provisioning and verification.

## One-time owner-controlled configuration (never exposed in task UI)

1. Use a **dedicated free Supabase project**, not another application's data. Create the private Storage bucket `manus-private`. The API uses `MANUS_DATABASE_URL` (a **server-only session-pooler** Postgres DSN over TLS), `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. Keep the service-role key on the backend only. The database creates its tables inside `manus_private`, not the public PostgREST schema. Verify project spend controls before deploying.
2. Use a **dedicated private GitHub repository** to host the runner workflow. Copy `docs/private-runner-workflow.yml` into that repository at `.github/workflows/manus-agent-job.yml`. The private repository keeps user prompts and execution logs out of public Actions logs. It automatically checks out the OpenManus code. Before real execution, the workflow needs the integration commit merged on OpenManus `main`; leave PR #3 draft until verified.
3. In the private GitHub runner repository, set variable `MANUS_SERVICE_URL` to the HTTPS Render URL. Set repository secrets `MANUS_WORKER_TOKEN` (same random secret as Render) and the legitimately obtained `OPENROUTER_API_KEY`. Never include tokens in code or workflow inputs.
4. In the Render service, configure `MANUS_GITHUB_REPO` (private repo), and `MANUS_GITHUB_ACTIONS_TOKEN` (fine-grained GitHub token with **Actions: write** on only that private repository). `MANUS_WORKER_TOKEN` must match the runner secret. `MANUS_APP_TOKEN` is generated independently in Render for operator access.
5. Only after verifying the GitHub account's free-usage controls and *no-charge policy*, set `MANUS_GITHUB_NO_CHARGE_VERIFIED=1`. With that flag unset, dispatcher refuses to contact GitHub. GitHub's limits and billing state must be checked independently of Render.
6. `render.yaml` deliberately selects only `plan: free` and disables auto-deploy. `MANUS_DURABLE_REQUIRED=1` ensures startup fails if remote persistence is absent. `MANUS_DAYTONA_SPEND_APPROVED=0` prevents billable Daytona allocation.

## Execution sequence

`PWA → /api/tasks → private Postgres → GitHub workflow_dispatch (opaque ID only) → runner VM → authenticated worker API → OpenManus DockerSandbox → progress/artifacts/result → Postgres + private storage → PWA`.

Neither prompt text nor the workspace token is included in dispatch arguments. The worker token is used outside the Docker sandbox, not injected into executed user code. The worker fetches approved input files and sends bounded artifacts back before marking a job complete.

A user hitting **Stop** marks the persistent job cancelled. The worker polls for cancellation and cleans up its sandbox. Jobs can be interrupted by GitHub VM expiration, Render suspension, Supabase pausing or provider quota; the UI must report failure rather than invent success.

## Important remaining production acceptance criteria

- No live Render Manus service exists yet; the prior Render create action was blocked.
- No dedicated Supabase project/bucket/DB URL or private runner repository is provisioned through these changes.
- No GitHub workflow has executed an actual OpenManus task, browser job or real model invocation through this path.
- GitHub's workflow_dispatch must be installed in a **private repository's default branch** before the API can dispatch to it.
- `manus_app/server.py` currently schedules recurring automations only while the web process is awake. A separate free scheduled wake-up path is needed for reliable closed-app automations.
- Remote Docker execution is isolated per ephemeral GitHub VM; this is not a persistent VNC desktop. Existing Daytona supports fuller desktop but is metered and explicitly disabled under the $0 requirement.
- Need to verify one actual user task with code execution, file output, safe stop, restart durability, model quota failure, and S23 Ultra browser installation before calling the product complete.

## Verification

`python -m pytest -q tests/test_workspace.py tests/test_existing_sandbox.py tests/test_durable_backends.py tests/test_github_runner.py tests/test_actions_worker.py tests/test_worker_api.py`

The tests exercise the database/backend selection gates, mock private Supabase artifact HTTP transfers, mock GitHub dispatcher with a private-repo check, worker credential rejection, authenticated task API transitions and artifact round trip. These are contract tests, **not** live external infrastructure tests.
