"""Private Supabase Storage bucket adapter; a durable alternative to Render disk."""
from __future__ import annotations

import os
from urllib.parse import quote

import httpx


MAX_FILE_BYTES = 20 * 1024 * 1024


class RemoteFileStore:
    def __init__(
        self, url: str, service_key: str, bucket: str = "manus-private", client=None
    ):
        if not url.startswith("https://") or not service_key:
            raise ValueError("HTTPS Supabase URL and server-only secret required")
        if not bucket.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid bucket")
        self.base = (
            url.rstrip("/") + "/storage/v1/object/" + quote(bucket, safe="") + "/"
        )
        self.headers = {"apikey": service_key, "Authorization": "Bearer " + service_key}
        self.client = client or httpx.Client(timeout=25)
        self.owns_client = client is None

    def _url(self, file_id: str):
        if (
            not file_id.startswith("file_")
            or len(file_id) != 37
            or not all(c in "0123456789abcdef" for c in file_id[5:])
        ):
            raise ValueError("Invalid file ID")
        return self.base + file_id

    def put(self, file_id: str, content: bytes):
        if len(content) > MAX_FILE_BYTES:
            raise ValueError("File exceeds 20 MB")
        r = self.client.post(
            self._url(file_id),
            content=content,
            headers={
                **self.headers,
                "Content-Type": "application/octet-stream",
                "x-upsert": "false",
            },
        )
        r.raise_for_status()

    def get(self, file_id: str) -> bytes:
        r = self.client.get(self._url(file_id), headers=self.headers)
        r.raise_for_status()
        if len(r.content) > MAX_FILE_BYTES:
            raise ValueError("Stored file exceeds 20 MB")
        return r.content

    def delete(self, file_id: str):
        url = self.base.split("/object/")[0] + "/object/"
        bucket = self.base.rsplit("/", 2)[-2]
        r = self.client.delete(
            url + bucket, json={"prefixes": [file_id]}, headers=self.headers
        )
        r.raise_for_status()

    def close(self):
        if self.owns_client:
            self.client.close()


def load_file_store():
    url = os.getenv("SUPABASE_URL", "")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    return RemoteFileStore(url, key) if url and key else None
