"""Use the repository's EXISTING SandboxManus / Daytona implementation.

Never create a metered remote sandbox unless the operator explicitly opts in.
No credentials or provider selection appear in the Android/mobile task UI.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from typing import Callable


async def run_daytona(executor, prompt: str, agent_instructions: str,
                      event: Callable[[str], None]) -> str:
    if os.environ.get("MANUS_DAYTONA_SPEND_APPROVED") != "1":
        raise RuntimeError("Daytona remote sandbox not approved for metered use")
    key = os.environ.get("DAYTONA_API_KEY", "")
    if not key:
        raise RuntimeError("Missing Daytona credentials")

    # Set configuration BEFORE importing modules that construct Daytona clients.
    from app.config import config
    config.daytona.daytona_api_key = key
    # Never use the checked-in example/default VNC password.
    config.daytona.VNC_password = secrets.token_urlsafe(32)

    from app.agent.sandbox_agent import SandboxManus

    agent = None
    try:
        event("Allocating existing OpenManus Daytona cloud computer")
        agent = await SandboxManus.create()
        executor.remote_agent = agent
        sandbox = agent.sandbox
        if not sandbox:
            raise RuntimeError("Existing sandbox agent returned no computer")

        preview_links = agent.sandbox_link or {}
        if preview_links:
            links = next(iter(preview_links.values()))
            # The service API remains authenticated. Preview URLs should not be logged
            # into unauthenticated health endpoints or committed to source control.
            executor.preview_url = links.get("vnc")
            event("Remote browser/desktop preview is available to this task")
        for folder in ("/workspace/input", "/workspace/output"):
            try:
                await asyncio.to_thread(sandbox.fs.create_folder, folder, "755")
            except Exception:
                # Some prebuilt sandbox images already provide these folders.
                info = await asyncio.to_thread(sandbox.fs.get_file_info, folder)
                if not info.is_dir:
                    raise

        input_dir = getattr(executor, "input_dir", None)
        if input_dir:
            for item in sorted(Path(input_dir).iterdir()):
                if item.is_file() and not item.is_symlink() and item.stat().st_size <= 20*1024*1024:
                    await asyncio.to_thread(
                        sandbox.fs.upload_file, item.read_bytes(),
                        "/workspace/input/" + item.name
                    )

        agent.system_prompt += (
            "\n\nAgent instructions:\n" + agent_instructions[:8000]
            + "\n\nInput files: /workspace/input. Save all deliverables in"
              " /workspace/output for automatic library collection."
        )
        event("Existing OpenManus remote browser, terminal, files and vision tools ready")
        result = await agent.run(prompt)

        output_dir = getattr(executor, "output_dir", None)
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)
            # Bounded traversal: avoid unbounded copies on a metered computer.
            pending = [("/workspace/output", Path(output_dir), 0)]
            copied = 0
            while pending and copied < 50:
                remote, local, depth = pending.pop()
                if depth > 3:
                    continue
                entries = await asyncio.to_thread(sandbox.fs.list_files, remote)
                for entry in entries:
                    name = Path(entry.name).name
                    if name in ("", ".", ".."):
                        continue
                    rpath, lpath = remote + "/" + name, local / name
                    if entry.is_dir:
                        if depth < 3:
                            pending.append((rpath, lpath, depth+1))
                    elif entry.size <= 20*1024*1024 and copied < 50:
                        lpath.parent.mkdir(parents=True, exist_ok=True)
                        data = await asyncio.to_thread(sandbox.fs.download_file, rpath)
                        if len(data) <= 20*1024*1024:
                            lpath.write_bytes(data)
                            copied += 1
            if copied:
                event(f"Saved {copied} remote sandbox artifacts to your Library")
        return result
    finally:
        if agent is not None:
            try:
                await agent.cleanup()
            finally:
                executor.remote_agent = None
