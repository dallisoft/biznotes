"""업로드 권한(§13.3) — 공유되지 않은 타인 노트북에 .hwp를 올리면 본문을 읽기 전에 거부한다.

권한 판정(`_notebook_with_permission` → `get_permission` → `resolve_permission`)은 실경로이고
DB 세션만 가짜다.
"""

import uuid
from typing import Any

import pytest
from conftest import FakeResult, make_settings
from fastapi import HTTPException

from biznotes_core.api.v1 import sources
from biznotes_core.models import Notebook, User

HWP = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 64 + b"HWP Document File"


class FakeDB:
    def __init__(self, notebook: Notebook, shares: list[str]):
        self.notebook = notebook
        self.shares = shares

    async def get(self, model: Any, key: Any) -> Any:
        return self.notebook if model is Notebook and key == self.notebook.id else None

    async def execute(self, stmt: Any) -> FakeResult:
        sql = str(stmt)
        if "notebook_shares" in sql:
            return FakeResult([(p,) for p in self.shares])
        return FakeResult([])  # user_groups


class NeverRead:
    filename = "공문.hwp"

    def __init__(self) -> None:
        self.reads = 0

    async def read(self, size: int = -1) -> bytes:
        self.reads += 1
        return HWP


@pytest.fixture
def stranger_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    puts: list[Any] = []
    monkeypatch.setattr(sources, "get_settings", lambda: make_settings(hwp_ingest_enabled=True))
    monkeypatch.setattr(sources, "put_object", lambda *a, **k: puts.append(a))
    owner = User(id=uuid.uuid4(), role="user")
    me = User(id=uuid.uuid4(), role="user")
    nb = Notebook(id=uuid.uuid4(), owner_id=owner.id, visibility="private", title="남의 노트북")
    return {"me": me, "nb": nb, "puts": puts}


async def test_unshared_notebook_upload_is_rejected(stranger_env) -> None:
    upload = NeverRead()
    db = FakeDB(stranger_env["nb"], shares=[])
    with pytest.raises(HTTPException) as exc:
        await sources.upload_file_source(stranger_env["nb"].id, upload, stranger_env["me"], db)
    assert exc.value.status_code == 404
    assert upload.reads == 0
    assert stranger_env["puts"] == []


async def test_read_only_share_cannot_upload(stranger_env) -> None:
    upload = NeverRead()
    db = FakeDB(stranger_env["nb"], shares=["read"])
    with pytest.raises(HTTPException) as exc:
        await sources.upload_file_source(stranger_env["nb"].id, upload, stranger_env["me"], db)
    assert exc.value.status_code == 403
    assert upload.reads == 0
    assert stranger_env["puts"] == []
