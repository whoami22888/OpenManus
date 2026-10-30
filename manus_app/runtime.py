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
    docker_socket = Path('/var/run/docker.sock').exists()
    docker_py = importlib.util.find_spec('docker') is not None
    model_key = bool(os.environ.get('OPENROUTER_API_KEY') or os.environ.get('OPENAI_API_KEY'))
    # Existing config is supported, but without inspecting credentials we cannot assert it works.
    model_present = model_key or bool(os.environ.get('MANUS_USE_EXISTING_CONFIG'))
    return {'model_configured':model_present,
            'sandbox_available':docker_socket and docker_py,
            'ready':model_present and docker_socket and docker_py,
            'mode':'OpenManus · Docker workspace',
            'reason': ('ready' if model_present and docker_socket and docker_py else
                       'Cloud workspace requires a Docker-capable host and configured free model credentials. No local host-shell fallback.')}


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
        if self.sandbox:
            await self.sandbox.cleanup()
