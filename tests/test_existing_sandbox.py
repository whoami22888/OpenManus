"""No paid remote sandbox can start by default; source is discoverable."""
from pathlib import Path

from manus_app.runtime import capabilities


def test_existing_daytona_source_is_discoverable(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    monkeypatch.delenv("MANUS_DAYTONA_SPEND_APPROVED", raising=False)
    state = capabilities()
    assert (
        state["existing_sandboxes"]["daytona"]["source_path"]
        == "app/daytona/sandbox.py"
    )
    assert not state["existing_sandboxes"]["daytona"]["available"]
    assert state["provider"] != "daytona"


def test_daytona_key_alone_cannot_bill(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "dummy-credential-that-must-never-be-used")
    monkeypatch.delenv("MANUS_DAYTONA_SPEND_APPROVED", raising=False)
    state = capabilities()
    assert state["existing_sandboxes"]["daytona"]["credentials_configured"]
    assert not state["existing_sandboxes"]["daytona"]["available"]


def test_daytona_executor_rejects_without_approval(monkeypatch):
    import asyncio

    from manus_app.daytona_runtime import run_daytona

    monkeypatch.setenv("DAYTONA_API_KEY", "dummy")
    monkeypatch.delenv("MANUS_DAYTONA_SPEND_APPROVED", raising=False)

    class Stub:
        pass

    try:
        asyncio.run(run_daytona(Stub(), "hello", "", lambda _: None))
        assert False, "Remote billing gate was bypassed"
    except RuntimeError as error:
        assert "not approved" in str(error)


def test_daytona_adapter_compiles():
    import py_compile

    py_compile.compile(
        str(Path(__file__).parents[1] / "manus_app" / "daytona_runtime.py"),
        doraise=True,
    )
