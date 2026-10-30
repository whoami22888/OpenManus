"""Adapters to the existing OpenManus agent and Docker sandbox.

No host-shell fallback: exposing unrestricted host subprocesses to a public cloud
application would make 'sandbox' a misleading name and create a takeover risk.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from typing import Callable


class RuntimeUnavailable(RuntimeError):
    pass


def capabilities() -> dict:
    """Report only usable backends; never silently allocate billable cloud resources."""
    docker_socket = Path('/var/run/docker.sock').exists()
    docker_sdk = importlib.util.find_spec('docker') is not None
    daytona_sdk = importlib.util.find_spec('daytona') is not None
    daytona_key = bool(os.environ.get('DAYTONA_API_KEY'))
    # Explicit operator approval required: Daytona charges for running resources.
    daytona_approved = os.environ.get('MANUS_DAYTONA_SPEND_APPROVED') == '1'
    model_present = bool(
        os.environ.get('OPENROUTER_API_KEY')
        or os.environ.get('OPENAI_API_KEY')
        or os.environ.get('MANUS_USE_EXISTING_CONFIG')
    )
    docker_ready = docker_socket and docker_sdk
    daytona_ready = daytona_sdk and daytona_key and daytona_approved
    provider = 'docker' if docker_ready else ('daytona' if daytona_ready else None)
    ready = model_present and provider is not None
    if not model_present:
        reason = 'A legitimate model provider must be configured once on the server.'
    elif docker_ready:
        reason = 'Docker sandbox is available.'
    elif daytona_sdk and daytona_key and not daytona_approved:
        reason = 'Daytona code and credentials detected, but remote sandbox creation is disabled until its billable usage is explicitly approved.'
    elif not daytona_key:
        reason = 'Docker daemon unavailable. The repository also contains a Daytona remote sandbox adapter; no Daytona credentials are configured.'
    else:
        reason = 'No verified container sandbox is available. Do not substitute unisolated host shell execution.'
    return {
        'model_configured': model_present,
        'sandbox_available': bool(provider),
        'ready': ready,
        'mode': 'OpenManus · Docker workspace' if provider != 'daytona' else 'OpenManus · Daytona cloud computer',
        'provider': provider,
        'existing_sandboxes': {
            'docker': {'source_path': 'app/sandbox/core/sandbox.py', 'available': docker_ready},
            'daytona': {'source_path': 'app/daytona/sandbox.py',
                        'sdk_installed': daytona_sdk,
                        'credentials_configured': daytona_key,
                        'spend_approved': daytona_approved,
                        'available': daytona_ready},
        },
        'reason': 'ready' if ready else reason,
    }


class SandboxExecutor:
    def __init__(self):
        self.sandbox = None
        self.output_dir = None
        self.input_dir = None

    def set_artifact_dir(self, value):
        self.output_dir = Path(value)

    def set_input_dir(self, value):
        self.input_dir = Path(value)

    async def run(self, prompt: str, agent_instructions: str, event: Callable[[str],None]) -> str:
        caps=capabilities()
        if not caps['ready']:
            raise RuntimeUnavailable(caps['reason'])
        if caps.get('provider') == 'daytona':
            from manus_app.daytona_runtime import run_daytona
            return await run_daytona(self, prompt, agent_instructions, event)
        # Import the existing OpenManus engine only after a sandbox and model are ready.
        from app.agent.manus import Manus
        from app.config import config, SandboxSettings
        from app.sandbox.core.sandbox import DockerSandbox
        from app.tool.base import BaseTool, ToolResult
        from app.tool.tool_collection import ToolCollection
        from app.tool.terminate import Terminate
        from pydantic import PrivateAttr

        # Provider selection happens once on the server, not during each task.
        if os.environ.get('OPENROUTER_API_KEY'):
            settings=config.llm['default']
            settings.base_url='https://openrouter.ai/api/v1'
            settings.model=os.environ.get('MANUS_MODEL','openrouter/free')
            settings.api_key=os.environ['OPENROUTER_API_KEY']
            settings.api_type='openai'
        elif os.environ.get('OPENAI_API_KEY'):
            settings=config.llm['default']
            settings.api_key=os.environ['OPENAI_API_KEY']
            settings.model=os.environ.get('MANUS_MODEL','gpt-4o-mini')

        image=os.environ.get('MANUS_SANDBOX_IMAGE','mcr.microsoft.com/playwright/python:v1.55.0-noble')
        bindings = {}
        if self.output_dir is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            bindings[str(self.output_dir.resolve())] = '/workspace/output'
        if self.input_dir is not None:
            self.input_dir.mkdir(parents=True,exist_ok=True)
            bindings[str(self.input_dir.resolve())] = '/workspace/input'
        sandbox = DockerSandbox(SandboxSettings(image=image, work_dir='/workspace',
                             network_enabled=True, cpu_limit=1.0, memory_limit='1024m',timeout=240),
                             volume_bindings=bindings)
        self.sandbox=sandbox
        event('Allocating an isolated cloud workspace')
        await sandbox.create()

        class ShellTool(BaseTool):
            name: str = 'sandbox_shell'
            description: str = 'Run a shell command INSIDE the isolated Linux workspace, not on the API host.'
            parameters: dict = {'type':'object','properties':{'command':{'type':'string'}},'required':['command']}
            async def execute(self,command:str,**kwargs):
                return ToolResult(output=(await sandbox.run_command(command,timeout=180))[:18000])

        class FileTool(BaseTool):
            name: str = 'sandbox_file'
            description: str = 'Read or write a file in /workspace inside the cloud computer.'
            parameters: dict = {'type':'object','properties':{'action':{'type':'string','enum':['read','write']},
                            'path':{'type':'string'},'content':{'type':'string'}},'required':['action','path']}
            async def execute(self,action:str,path:str,content:str='',**kwargs):
                from pathlib import PurePosixPath
                target=PurePosixPath('/workspace')/path.lstrip('/')
                if '..' in PurePosixPath(path).parts or not str(target).startswith('/workspace/'):
                    return ToolResult(error='Path must remain in /workspace')
                if action=='read':return ToolResult(output=(await sandbox.read_file(str(target)))[:24000])
                if action=='write':
                    await sandbox.write_file(str(target),content)
                    return ToolResult(output='Saved '+str(target))
                return ToolResult(error='Unsupported action')

        class BrowserTool(BaseTool):
            name: str = 'sandbox_browser'
            description: str = 'Open a public website with Playwright in the isolated cloud computer, and return the page title and visible text.'
            parameters: dict = {'type':'object','properties':{'url':{'type':'string'}},'required':['url']}
            async def execute(self,url:str,**kwargs):
                from urllib.parse import urlsplit
                import json,shlex
                value=urlsplit(url)
                if value.scheme!='https' or not value.hostname or value.username or value.password:
                    return ToolResult(error='Only public HTTPS webpages are supported')
                # Do not allow direct access to local/metadata IPs. A production egress
                # proxy must also enforce DNS-resolution and redirect policies.
                import ipaddress
                try:
                    addr=ipaddress.ip_address(value.hostname)
                    if not addr.is_global:return ToolResult(error='Private network targets denied')
                except ValueError:pass
                script='''import asyncio,json,sys
from playwright.async_api import async_playwright
async def main():
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True)
  page=await browser.new_page()
  await page.goto(sys.argv[1],wait_until="domcontentloaded",timeout=18000)
  print(json.dumps({"title":await page.title(),"text":(await page.locator("body").inner_text())[:9000]}))
  await browser.close()
asyncio.run(main())
'''
                cmd='python -c '+shlex.quote(script)+' '+shlex.quote(url)
                return ToolResult(output=(await sandbox.run_command(cmd,timeout=45))[:14000])

        try:
            agent=await Manus.create()
            agent.available_tools=ToolCollection(ShellTool(),FileTool(),BrowserTool(),Terminate(),
                                                *agent.mcp_clients.tools)
            # User task remains primary; these instructions give the saved agent a role.
            agent.system_prompt=agent.system_prompt+'\n\nAgent instructions:\n'+agent_instructions[:8000]+'\n\nLibrary inputs are available in /workspace/input. Deliver ALL generated files in /workspace/output so they are saved to the user library before sandbox cleanup.'
            event('OpenManus agent started: planning and executing tools')
            result=await asyncio.wait_for(agent.run(prompt),timeout=3600)
            return result
        finally:
            try:
                if 'agent' in locals():await agent.cleanup()
            finally:
                await sandbox.cleanup()

    async def cancel(self):
        if getattr(self, 'remote_agent', None) is not None:
            await self.remote_agent.cleanup()
            self.remote_agent = None
        if self.sandbox:
            await self.sandbox.cleanup()
