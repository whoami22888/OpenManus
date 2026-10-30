# Run the integrated workspace in OpenManus

PR #3 adds the workspace to `whoami22888/OpenManus`; it is not a separate framework or a ZIP to unzip. Use a checked-out repository branch that includes the PR. The change adds `manus_app/`, `requirements-workspace.txt`, workspace tests and a targeted GitHub Actions workflow. It also modifies `app/sandbox/client.py` and `setup_android.sh`; review those changes before integration.

To run the mobile web application on a Docker-capable server, install the dependencies and serve `manus_app.server:app`. The user does **not** choose a server, model or agent in the app; these are set by deployment environment. The UI can be installed to the Android home screen as a PWA.

The unbundled original repository is required. The separately uploaded modded APKs are **not** a source of cloud entitlements or backend compute. The main app uses original OpenManus agents and DockerSandbox rather than the EVE Android task form.

Run the current workspace regression suite shown in `docs/FREE_CLOUD_EXECUTION.md`. Passing local tests does not establish real Docker execution, cloud durability, a live model provider or Android-device acceptance; those remain unverified until exercised in the configured environment.
