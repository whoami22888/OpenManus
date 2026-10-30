"""Remote runner dispatch uses only opaque IDs and rejects public/billable hosts."""
import httpx
import pytest

from manus_app.github_actions import DispatchUnavailable, GitHubActionsDispatcher


ID = "tsk_" + "a" * 32
TOKEN = "private-gh-action-token-1234567890"


@pytest.mark.asyncio
async def test_github_dispatch_private_and_id_only(monkeypatch):
    monkeypatch.setenv("MANUS_GITHUB_NO_CHARGE_VERIFIED", "1")
    seen = []

    def handler(req):
        seen.append(req)
        if req.method == "GET":
            return httpx.Response(200, json={"private": True})
        assert req.method == "POST"
        assert req.url.path.endswith("/manus-agent-job.yml/dispatches")
        assert req.headers["authorization"] == "Bearer " + TOKEN
        assert (
            req.content
            == b'{"ref":"main","inputs":{"task_id":"tsk_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'
        )
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await GitHubActionsDispatcher(
            TOKEN, "owner/private-runner", client=client
        ).dispatch(ID)
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_public_runner_not_used(monkeypatch):
    monkeypatch.setenv("MANUS_GITHUB_NO_CHARGE_VERIFIED", "1")
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json={"private": False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DispatchUnavailable, match="private"):
            await GitHubActionsDispatcher(
                TOKEN, "owner/public", client=client
            ).dispatch(ID)
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_billing_gate_blocks_any_network(monkeypatch):
    monkeypatch.delenv("MANUS_GITHUB_NO_CHARGE_VERIFIED", raising=False)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(
                AssertionError("network should not be used")
            )
        )
    ) as client:
        with pytest.raises(DispatchUnavailable, match="billing"):
            await GitHubActionsDispatcher(
                TOKEN, "owner/private", client=client
            ).dispatch(ID)


@pytest.mark.asyncio
async def test_reject_invalid_id(monkeypatch):
    monkeypatch.setenv("MANUS_GITHUB_NO_CHARGE_VERIFIED", "1")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: (_ for _ in ()).throw(
                AssertionError("network should not be used")
            )
        )
    ) as client:
        with pytest.raises(DispatchUnavailable, match="ID"):
            await GitHubActionsDispatcher(
                TOKEN, "owner/private", client=client
            ).dispatch("../evil")
