## Already-existing OpenManus sandbox backends

The original OpenManus repository contains:
- `app/sandbox/core/sandbox.py`: DockerSandbox, with isolated terminal and file operations. Requires a Docker-capable worker host.
- `app/sandbox/core/manager.py`: sandbox lifecycle and cleanup.
- `app/daytona/sandbox.py`: remote Daytona sandbox creation with VNC/website previews.
- `app/agent/sandbox_agent.py`: SandboxManus coordinating shell, browser, file and vision tools. Existing remote implementation, **not newly written**.
- `app/tool/sandbox/sb_{shell,browser,files,vision}_tool.py`: existing remote tools.

The integrated `manus_app/runtime.py` now discovers both implementations. Docker remains the automatic first choice on a compatible host; the Daytona adapter can be selected server-side *without any task-by-task model/server picker*.

### Do not create billable resources without approval

Daytona bills cloud sandbox resources by usage. It is **not** a zero-cost unlimited runtime. The remote adapter requires all of: `daytona` SDK, `DAYTONA_API_KEY`, `MANUS_DAYTONA_SPEND_APPROVED=1`, and a separately configured legitimate model provider. Do not set the spend approval flag under the owner's present strict $0 instruction. The gate is intentional: merely adding a credential must not start a billable sandbox.

`pip install daytona` is only necessary for an approved Daytona-based runtime; never install or provision it on the assumption that the repo contains a live sandbox. The existing Daytona implementation may need API/SDK migration: this PR has **not** run a live remote computer and must not be accepted as end-to-end verified until cloud preview, shell, browser, file, model, artifact, and teardown pass. The checked-in implementation includes an unsafe default VNC password; the optional adapter generates a strong password before creating any remote sandbox.

Render free web services don't expose a Docker host to agent code and don't provide durable disks. They can host frontend/coordination, but not this Docker backend without external compute.

### Verification

`pytest -q tests/test_workspace.py tests/test_existing_sandbox.py` ensures the application API tests and the no-unapproved-remote-allocation gate remain enforced. It does NOT create a remote sandbox.

No new server picker is added to the mobile UI.
