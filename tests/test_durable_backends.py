"""Durable-backend contract checks without external credentials or paid services."""
import httpx
import pytest

from manus_app.remote_files import RemoteFileStore
from manus_app.pg_store import PostgresStore, TABLES


def test_supabase_storage_round_trip_with_authenticated_requests():
    seen = []
    def transport(req):
        seen.append(req)
        assert req.headers["authorization"] == "Bearer owner-secret"
        assert "/storage/v1/object/manus-private/file_" in str(req.url)
        if req.method == "POST":
            assert req.content == b"generated artifact"
            return httpx.Response(200, json={"Key": "file"})
        return httpx.Response(200, content=b"generated artifact")
    with httpx.Client(transport=httpx.MockTransport(transport)) as http:
        store = RemoteFileStore("https://test.supabase.co", "owner-secret", client=http)
        file_id = "file_" + "a" * 32
        store.put(file_id, b"generated artifact")
        assert store.get(file_id) == b"generated artifact"
    assert [r.method for r in seen] == ["POST", "GET"]


def test_remote_storage_rejects_bad_urls_and_ids():
    with pytest.raises(ValueError):
        RemoteFileStore("http://unsafe", "secret")
    s = RemoteFileStore("https://demo.supabase.co", "secret")
    with pytest.raises(ValueError):
        s.put("../../../env", b"x")
    with pytest.raises(ValueError):
        s.get("file_not_valid")
    s.close()


def test_db_table_names_are_allowlisted():
    assert "agents" in TABLES
    assert PostgresStore._table("agents") == "manus_private.agents"
    with pytest.raises(ValueError):
        PostgresStore._table("agents; DROP TABLE agents;")


def test_remote_storage_max_size():
    s = RemoteFileStore("https://demo.supabase.co", "secret")
    with pytest.raises(ValueError):
        s.put("file_" + "a"*32, b"x"*(20*1024*1024+1))
    s.close()
