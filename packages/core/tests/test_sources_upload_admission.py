"""업로드 라우터 — 허용 확장자·관문 오류 매핑·bounded read(§4.2).

라우터 본문은 실경로다. 권한 확인·DB 이후 단계(용량·중복 검사, 스토리지)만 스텁이다 —
관문을 통과하면 스텁이 `_Admitted`를 던져 그 뒤를 실행하지 않는다.
"""

import io
import uuid
from typing import Any

import pytest
from conftest import make_settings
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from biznotes_core.api import deps
from biznotes_core.api.v1 import sources
from biznotes_core.core.db import get_db
from biznotes_core.models import SystemSetting
from biznotes_core.services.ingestion.admission import MAX_UPLOAD_BYTES

MIB = 1024 * 1024



class _Admitted(Exception):
    pass


class UowDB:
    """ingest_uow가 쓰는 만큼만 — 관문 뒤 단계는 _Admitted로 끝나 롤백된다.

    `get`은 업로드 상한의 관리자 재정의(`system_settings["source_upload"]`, #388) 조회다.
    """

    def __init__(self, upload_override: dict[str, Any] | None = None) -> None:
        self.info: dict[str, Any] = {}
        self.rollbacks = 0
        self.upload_override = upload_override

    async def get(self, model: Any, key: Any, **kw: Any) -> Any:
        if model is SystemSetting and key == "source_upload" and self.upload_override is not None:
            return SystemSetting(key=key, value=self.upload_override)
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1


class FakeUpload:
    """read(size)를 존중하고 내준 바이트를 센다. 크기 없는 read()는 허용하지 않는다."""

    def __init__(self, filename: str, total: int, prefix: bytes = b"%PDF"):
        self.filename = filename
        self.total = total
        self.prefix = prefix
        self.served = 0
        self.sizes: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        assert size is not None and size >= 0, "본문 전체 read() 금지"
        self.sizes.append(size)
        n = min(size, self.total - self.served)
        start = self.served
        self.served += n
        head = self.prefix[start : start + n]
        return head + b"\0" * (n - len(head))


@pytest.fixture
def router_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    env: dict[str, Any] = {"settings": make_settings(), "admitted": []}

    async def permitted(db: Any, notebook_id: Any, user: Any, need_write: bool) -> None:
        return None

    async def after_admission(db: Any, notebook_id: Any, filename: str, size: int) -> str:
        env["admitted"].append((filename, size))
        raise _Admitted()

    monkeypatch.setattr(sources, "_notebook_with_permission", permitted)
    monkeypatch.setattr(sources, "get_settings", lambda: env["settings"])
    monkeypatch.setattr(sources, "ensure_file_source_capacity_and_uniqueness", after_admission)
    return env


async def _upload(upload: FakeUpload, db: UowDB | None = None) -> None:
    await sources.upload_file_source(uuid.uuid4(), upload, user=object(), db=db or UowDB())


async def test_over_limit_stream_returns_413_after_limit_plus_one(router_env) -> None:
    upload = FakeUpload("big.pdf", MAX_UPLOAD_BYTES + 1)
    with pytest.raises(HTTPException) as exc:
        await _upload(upload)
    assert exc.value.status_code == 413
    assert exc.value.detail == "파일이 50MB를 초과합니다"
    assert upload.served == MAX_UPLOAD_BYTES + 1
    assert upload.sizes == [MAX_UPLOAD_BYTES + 1]


async def test_endless_stream_is_not_read_past_limit(router_env) -> None:
    upload = FakeUpload("huge.pdf", 5 * 1024 * 1024 * 1024)
    with pytest.raises(HTTPException) as exc:
        await _upload(upload)
    assert exc.value.status_code == 413
    assert upload.served == MAX_UPLOAD_BYTES + 1


async def test_exactly_limit_is_admitted(router_env) -> None:
    upload = FakeUpload("edge.pdf", MAX_UPLOAD_BYTES)
    with pytest.raises(_Admitted):
        await _upload(upload)
    assert router_env["admitted"] == [("edge.pdf", MAX_UPLOAD_BYTES)]


@pytest.mark.parametrize(
    "filename,prefix,status,detail",
    [
        ("a.pdf", b"<html>", 422, "파일 내용이 확장자와 일치하지 않습니다"),
        ("a.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 422, "지원하지 않는 확장자"),
        ("a.exe", b"MZ", 422, "지원하지 않는 확장자"),
    ],
    ids=["magic", "hwp-unsupported", "ext"],
)
async def test_admission_errors_map_to_422(router_env, filename, prefix, status, detail):
    with pytest.raises(HTTPException) as exc:
        await _upload(FakeUpload(filename, len(prefix) + 10, prefix))
    assert exc.value.status_code == status
    assert detail in exc.value.detail
    assert router_env["admitted"] == []


async def test_empty_file_is_422(router_env) -> None:
    with pytest.raises(HTTPException) as exc:
        await _upload(FakeUpload("a.txt", 0))
    assert (exc.value.status_code, exc.value.detail) == (422, "빈 파일입니다")


async def test_extension_list_names_supported_formats(router_env) -> None:
    with pytest.raises(HTTPException) as exc:
        await _upload(FakeUpload("a.exe", 10))
    assert ".hwp" not in exc.value.detail
    assert ".pdf" in exc.value.detail


def test_router_source_has_no_unbounded_read() -> None:
    import inspect

    src = inspect.getsource(sources)
    assert "file.read()" not in src
    # 상한은 유효값(관리자 재정의 > env, #388)이다 — 읽기 전에 정해진다
    assert src.count("file.read(") == 1
    assert "file.read(max_bytes + 1)" in src
    assert sources.MAX_FILE_SIZE == MAX_UPLOAD_BYTES


def test_multipart_request_maps_codes(router_env) -> None:
    """HTTP 경로로도 같은 매핑 — 422 본문이 관문 메시지다."""
    app = FastAPI()
    app.include_router(sources.router)
    app.dependency_overrides[deps.get_current_user] = lambda: object()
    app.dependency_overrides[get_db] = UowDB
    app.dependency_overrides[sources._source_upload_limiter] = lambda: None
    client = TestClient(app)
    url = f"/notebooks/{uuid.uuid4()}/sources/upload"

    bad = client.post(url, files={"file": ("a.pdf", io.BytesIO(b"not a pdf"), "x/y")})
    assert bad.status_code == 422
    assert bad.json()["detail"] == "파일 내용이 확장자와 일치하지 않습니다"
    # 관문 코드가 실제 HTTP 응답 헤더에 실린다
    assert bad.headers["x-admission-code"] == "magic_mismatch"

    with pytest.raises(_Admitted):
        client.post(url, files={"file": ("a.pdf", io.BytesIO(b"%PDF-1.7 body"), "x/y")})


# ── #388: 설정·재정의된 상한 ────────────────────────────────────────────────


async def test_env_limit_above_default_admits_larger_file(router_env) -> None:
    router_env["settings"] = make_settings(
        source_max_upload_bytes=80 * MIB
    )
    with pytest.raises(_Admitted):
        await _upload(FakeUpload("big.pdf", 60 * MIB))
    assert router_env["admitted"] == [("big.pdf", 60 * MIB)]


async def test_env_limit_rejects_with_same_413_and_message(router_env) -> None:
    router_env["settings"] = make_settings(
        source_max_upload_bytes=10 * MIB
    )
    upload = FakeUpload("big.pdf", 10 * MIB + 1)
    with pytest.raises(HTTPException) as exc:
        await _upload(upload)
    assert exc.value.status_code == 413
    assert exc.value.detail == "파일이 10MB를 초과합니다"
    assert exc.value.headers == {"X-Admission-Code": "too_large"}
    assert upload.served == 10 * MIB + 1  # 상한+1까지만 읽는다


async def test_admin_override_wins_over_env(router_env) -> None:
    db = UowDB({"max_upload_bytes": 5 * MIB})
    upload = FakeUpload("big.pdf", 20 * MIB)
    with pytest.raises(HTTPException) as exc:
        await _upload(upload, db)
    assert (exc.value.status_code, exc.value.detail) == (413, "파일이 5MB를 초과합니다")
    assert upload.served == 5 * MIB + 1

    with pytest.raises(_Admitted):
        await _upload(FakeUpload("edge.pdf", 5 * MIB), db)


async def test_out_of_range_stored_override_falls_back_to_env(router_env) -> None:
    db = UowDB({"max_upload_bytes": 0})
    upload = FakeUpload("big.pdf", MAX_UPLOAD_BYTES + 1)
    with pytest.raises(HTTPException) as exc:
        await _upload(upload, db)
    assert exc.value.detail == "파일이 50MB를 초과합니다"
    assert upload.served == MAX_UPLOAD_BYTES + 1


@pytest.mark.parametrize(
    ("env_bytes", "override"),
    [(MAX_UPLOAD_BYTES, None), (80 * MIB, None), (80 * MIB, {"max_upload_bytes": 7 * MIB + 3})],
    ids=["default", "env", "override"],
)
async def test_public_value_equals_judgment_value(router_env, env_bytes, override) -> None:
    """공개 조회 값을 넘는 1바이트는 413, 그 값까지는 통과한다."""
    router_env["settings"] = make_settings(
        source_max_upload_bytes=env_bytes
    )
    db = UowDB(override)
    public = await sources.get_source_upload_limits(user=object(), db=db)
    limit = public.max_upload_bytes
    assert limit == (override["max_upload_bytes"] if override else env_bytes)

    with pytest.raises(_Admitted):
        await _upload(FakeUpload("edge.pdf", limit), db)
    over = FakeUpload("over.pdf", limit + 1)
    with pytest.raises(HTTPException) as exc:
        await _upload(over, db)
    assert exc.value.status_code == 413
    assert over.served == limit + 1


def test_public_route_is_reachable_before_source_detail(router_env) -> None:
    """`/sources/upload-limits`가 `/sources/{source_id}`에 가려지지 않는다(인증 사용자)."""
    app = FastAPI()
    app.include_router(sources.router)
    app.dependency_overrides[deps.get_current_user] = lambda: object()
    app.dependency_overrides[get_db] = UowDB
    res = TestClient(app).get("/sources/upload-limits")
    assert res.status_code == 200, res.text
    assert res.json() == {"max_upload_bytes": MAX_UPLOAD_BYTES, "max_sources_per_notebook": 1000}


def test_public_route_requires_authentication() -> None:
    app = FastAPI()
    app.include_router(sources.router)
    app.dependency_overrides[get_db] = UowDB
    assert TestClient(app).get("/sources/upload-limits").status_code == 401
