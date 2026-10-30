# Merge this code into the existing OpenManus repository

This ZIP is a **source overlay** for `whoami22888/OpenManus`, NOT a new framework and NOT a replacement of its agent tools. Unzip the ZIP directly into the root of a checked-out OpenManus repository. It adds `manus_app/`, `requirements-workspace.txt`, `tests/test_workspace.py`, and a targeted GitHub Actions workflow. It does not overwrite any existing agent or sandbox source files.

To run the mobile web application on a Docker-capable server, install the dependencies and serve `manus_app.server:app`. The user does **not** choose a server, model or agent in the app; these are set by deployment environment. The UI can be installed to the Android home screen as a PWA.

The unbundled original repository is required. The separately uploaded modded APKs are **not** a source of cloud entitlements or backend compute. The main app uses original OpenManus agents and DockerSandbox rather than the EVE Android task form.

Test status for the source overlay before integration: 18 local regression tests passing; real Docker computer and Android-device/cloud end-to-end verification pending.
