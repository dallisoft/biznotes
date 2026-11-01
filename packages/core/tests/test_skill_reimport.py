"""#402 §4 — 기존 가져온 스킬 다시 가져오기(`target_skill_id`)와 출처 키(`source_ref`).

권한·잠금·draft 교체는 PostgreSQL이 필요해 `tests/test_skill_reimport_pg.py`가 본다. 여기서는
DB 없이 입력 경계(`bad_source_ref`, 입력값을 되돌려주지 않는다), `origin_detail` 모양, 재가져오기
분기를 고정한다. 원격 fetch만 대역으로 바꾼다.
"""

import hashlib
import io
import uuid
import zipfile

import httpx
import pytest
from fastapi import FastAPI, HTTPException, UploadFile

from biznotes_core.api import deps
from biznotes_core.api.v1 import skills as skills_api
from biznotes_core.core.db import get_db
from biznotes_core.schemas.skill import SkillImportUrlRequest
from biznotes_core.services.skills import package as pkg
from biznotes_core.services.skills import service as svc
from biznotes_core.services.skills.validator import SkillValidationError

SKILL_MD = b"---\nname: tool\ndescription: d\n---\n# t\n"
URL = "https://example.test/repo/zip/0123abc"


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("tool/SKILL.md", SKILL_MD)
    return buf.getvalue()


GOOD_REFS = [
    "github:owner/repo@0123abc:skills/tool",
    "a:",
    "git+ssh.v-2:owner/repo",
    "x" * 32 + ":v",  # scheme 32자
    "github:" + "p" * (512 - len("github:")),  # 512B
    "github:소유자/저장소@0123abc:경로",
]
BAD_REFS = [
    "https://example.test/repo.zip?token=secret-value",
    "github:owner/repo?ref=main",
    "github:owner/repo#main",
    "github:owner repo",
    "github:owner" + chr(9) + "repo",
    "github:a" + chr(0x3000) + "b",
    "github:" + "p" * (513 - len("github:")),  # 513B
    "github:" + "가" * 171,  # 520B — 바이트로 센다
    "Github:owner/repo",
    "1git:owner/repo",
    "x" * 33 + ":v",  # scheme 33자
    "owner/repo",
    "",
    "github:" + chr(0),
    "github:" + chr(0x2028),
    "github:" + chr(0xD800),  # 짝 없는 surrogate — 인코딩 오류가 500으로 새지 않는다
]


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    calls: dict[str, object] = {}

    async def create(_db, _owner, unpacked, *, origin_detail):
        calls["create"] = origin_detail
        return object()

    async def reimport(_db, _owner, target_skill_id, unpacked, *, origin_detail):
        calls["reimport"] = (target_skill_id, origin_detail, set(unpacked.files))
        return object()

    monkeypatch.setattr(svc, "_import_unpacked", create)
    monkeypatch.setattr(svc, "_reimport_unpacked", reimport)
    return calls


def no_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_url: str) -> bytes:
        raise AssertionError("입력 검증에 실패하면 원격 archive를 받지 않는다")

    monkeypatch.setattr(svc, "_fetch_import_archive", fail)


@pytest.mark.parametrize("ref", GOOD_REFS)
async def test_good_source_ref_is_stored_with_the_import(
    ref: str, stored: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_zip()
    monkeypatch.setattr(svc, "_fetch_import_archive", lambda _url: data)
    await svc.import_url_detailed(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        URL,
        source_ref=ref,
    )
    assert stored["create"] == {
        "source": "url",
        "archive_sha256": hashlib.sha256(data).hexdigest(),
        "source_ref": ref,
    }


@pytest.mark.parametrize("ref", BAD_REFS)
async def test_bad_source_ref_is_rejected_before_fetch(
    ref: str, stored: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    no_fetch(monkeypatch)
    with pytest.raises(SkillValidationError) as exc:
        await svc.import_url_detailed(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            URL,
            source_ref=ref,
        )
    assert exc.value.reason == "bad_source_ref"
    if ref:
        assert ref not in str(exc.value)
    assert stored == {}


async def test_zip_import_stores_source_ref_only_when_given(stored: dict[str, object]) -> None:
    await svc.import_zip_detailed(object(), object(), make_zip())  # type: ignore[arg-type]
    assert stored["create"] == {"source": "zip"}  # 기존 모양 그대로
    await svc.import_zip_detailed(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        make_zip(),
        source_ref="github:o/r@1:tool",
    )
    assert stored["create"] == {"source": "zip", "source_ref": "github:o/r@1:tool"}
    with pytest.raises(SkillValidationError) as exc:
        await svc.import_zip_detailed(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            make_zip(),
            source_ref="github:o r",
        )
    assert exc.value.reason == "bad_source_ref"


async def test_target_skill_id_routes_to_the_reimport_path(
    stored: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = make_zip()
    target = uuid.uuid4()
    monkeypatch.setattr(svc, "_fetch_import_archive", lambda _url: data)
    await svc.import_url_detailed(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        URL,
        target_skill_id=target,
        source_ref="github:o/r@1:tool",
    )
    assert "create" not in stored
    assert stored["reimport"] == (
        target,
        {
            "source": "url",
            "archive_sha256": hashlib.sha256(data).hexdigest(),
            "source_ref": "github:o/r@1:tool",
        },
        {"SKILL.md"},
    )
    stored.clear()
    await svc.import_zip_detailed(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        data,
        target_skill_id=target,
    )
    assert stored == {"reimport": (target, {"source": "zip"}, {"SKILL.md"})}


async def test_reimport_validates_the_package_before_touching_the_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """검증 실패는 스킬 행을 잠그기 전에 난다 — DB에 닿지 않는다."""

    class _NoDb:
        def __getattr__(self, name: str):
            raise AssertionError(f"검증 전에는 DB를 쓰지 않는다: {name}")

    bad = pkg.UnpackedPackage(files={"SKILL.md": b"---\ndescription: d\n---\n"}, root_name=None)
    with pytest.raises(svc.SkillParseError) as exc:
        await svc._reimport_unpacked(
            _NoDb(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            uuid.uuid4(),
            bad,
            origin_detail={"source": "zip"},
        )
    assert exc.value.reason == "missing_name"


async def test_url_route_rejects_bad_source_ref_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_fetch(monkeypatch)
    secret = "https://user:tok-123@example.test/repo.zip?sig=abc"
    with pytest.raises(HTTPException) as exc:
        await skills_api.import_skill_url(
            body=SkillImportUrlRequest(url=URL, source_ref=secret),
            user=object(),  # type: ignore[arg-type]
            db=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 422
    assert str(exc.value.detail).startswith("[bad_source_ref]")
    assert "tok-123" not in str(exc.value.detail)


async def test_zip_route_rejects_bad_source_ref_without_echoing_it() -> None:
    with pytest.raises(HTTPException) as exc:
        await skills_api.import_skill_zip(
            file=UploadFile(file=io.BytesIO(make_zip()), filename="tool.zip"),
            selected_skill_path=None,
            source_ref="github:secret value",
            user=object(),  # type: ignore[arg-type]
            db=object(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 422
    assert str(exc.value.detail).startswith("[bad_source_ref]")
    assert "secret value" not in str(exc.value.detail)


def test_url_request_schema_adds_optional_fields_without_constraints() -> None:
    """`source_ref` 제약은 서비스가 건다 — 스키마 422가 입력값을 되돌려주지 않게."""
    body = SkillImportUrlRequest(url=URL)
    assert (body.target_skill_id, body.source_ref) == (None, None)
    assert SkillImportUrlRequest(url=URL, source_ref="x" * 5000).source_ref == "x" * 5000


async def test_bad_source_ref_response_over_http_does_not_echo_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASGI 실경로 — 핸들러 422(`bad_source_ref`) 본문에도 입력값이 없다(리뷰 M)."""
    no_fetch(monkeypatch)
    app = FastAPI()
    app.include_router(skills_api.router)
    app.dependency_overrides[deps.get_current_user] = lambda: object()
    app.dependency_overrides[get_db] = lambda: None
    secret = "https://example.test/repo.zip?sig=tok-7f3a"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        res = await client.post("/skills/import-url", json={"url": URL, "source_ref": secret})
    assert res.status_code == 422
    assert res.json()["detail"].startswith("[bad_source_ref]")
    assert "tok-7f3a" not in res.text
