"""로컬 스토리지 내부 엔드포인트(`/internal/storage/<토큰>`) — 샌드박스가 파일을 받고 올리는 길.

토큰 검증(서명·만료·메서드)·버킷 허용 목록·본문 상한·백엔드 조건을 실제 라우터와 로컬
백엔드(tmp_path)로 본다. 마지막 테스트는 샌드박스 작업 형식 그대로(출처 검사 포함) 내려받기·
올리기가 이 엔드포인트로 끝나는지 확인한다.
"""

from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from biznotes_core.api import internal_storage
from biznotes_core.config import get_settings
from biznotes_core.services import storage

BASE = "http://api:8000"


@pytest.fixture
def client(local_storage) -> TestClient:
    app = FastAPI()
    app.include_router(internal_storage.router)
    return TestClient(app, base_url=BASE)


def _url(method: str, bucket: str, key: str, **kw: Any) -> str:
    return storage.presigned_url_for(
        method,
        bucket,
        key,
        expires=kw.pop("expires", timedelta(minutes=5)),
        endpoint=BASE,
        region="us-east-1",
        **kw,
    )


def _artifacts() -> str:
    return get_settings().storage_bucket_artifacts


def test_get_streams_the_signed_object(client: TestClient) -> None:
    storage.put_object(_artifacts(), "run/in.csv", b"a,b\n1,2\n")

    res = client.get(_url("GET", _artifacts(), "run/in.csv"))

    assert res.status_code == 200
    assert res.content == b"a,b\n1,2\n"
    assert res.headers["content-type"] == "application/octet-stream"
    assert res.headers["content-length"] == "8"


def test_get_skill_package_file(client: TestClient) -> None:
    bucket = get_settings().storage_bucket_skill_packages
    storage.put_object(bucket, "skills/s/versions/v/scripts/now.py", b"print(1)")

    res = client.get(_url("GET", bucket, "skills/s/versions/v/scripts/now.py"))

    assert res.status_code == 200 and res.content == b"print(1)"


def test_get_missing_object_is_404(client: TestClient) -> None:
    assert client.get(_url("GET", _artifacts(), "run/none")).status_code == 404


def test_put_writes_the_signed_slot(client: TestClient) -> None:
    res = client.put(_url("PUT", _artifacts(), "run/scripts/c/0", max_bytes=100), content=b"out")

    assert res.status_code == 200
    assert storage.get_object(_artifacts(), "run/scripts/c/0") == b"out"


def test_put_over_the_limit_is_413_and_writes_nothing(client: TestClient) -> None:
    url = _url("PUT", _artifacts(), "run/scripts/c/0", max_bytes=4)

    assert client.put(url, content=b"12345").status_code == 413

    def chunks():
        yield b"12"
        yield b"345"

    # Content-Length 없이 흘려보내도 받은 만큼 세어 자른다
    assert client.put(url, content=chunks()).status_code == 413
    with pytest.raises(storage.ObjectNotFound):
        storage.get_object(_artifacts(), "run/scripts/c/0")


def test_expired_token_is_403(client: TestClient) -> None:
    url = _url("GET", _artifacts(), "run/in.csv", expires=timedelta(seconds=-1))
    assert client.get(url).status_code == 403


def test_method_mismatch_is_403(client: TestClient) -> None:
    storage.put_object(_artifacts(), "run/in.csv", b"x")
    get_url = _url("GET", _artifacts(), "run/in.csv")
    put_url = _url("PUT", _artifacts(), "run/in.csv", max_bytes=10)

    # 읽기 토큰으로 덮어쓰지 못하고, 쓰기 토큰으로 읽지 못한다
    assert client.put(get_url, content=b"overwrite").status_code == 403
    assert client.get(put_url).status_code == 403
    assert storage.get_object(_artifacts(), "run/in.csv") == b"x"


def test_forged_token_is_403(client: TestClient) -> None:
    url = _url("GET", _artifacts(), "run/in.csv")
    assert client.get(url[:-3] + "AAA").status_code == 403
    assert client.get(BASE + storage.INTERNAL_STORAGE_PATH + "garbage").status_code == 403


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_buckets_outside_the_allowlist_are_403(client: TestClient, method: str) -> None:
    s = get_settings()
    storage.put_object(s.storage_bucket_sources, "src/raw.pdf", b"user upload")
    url = _url(method, s.storage_bucket_sources, "src/raw.pdf", max_bytes=100)

    res = client.get(url) if method == "GET" else client.put(url, content=b"replaced")

    assert res.status_code == 403
    assert storage.get_object(s.storage_bucket_sources, "src/raw.pdf") == b"user upload"


def test_skill_packages_are_read_only(client: TestClient) -> None:
    bucket = get_settings().storage_bucket_skill_packages
    url = _url("PUT", bucket, "skills/s/versions/v/scripts/now.py", max_bytes=100)
    assert client.put(url, content=b"tampered").status_code == 403


def test_endpoint_is_404_unless_the_backend_is_local(client: TestClient, monkeypatch) -> None:
    storage.put_object(_artifacts(), "run/in.csv", b"x")
    url = _url("GET", _artifacts(), "run/in.csv")
    monkeypatch.setattr(get_settings(), "storage_backend", "s3")

    assert client.get(url).status_code == 404


def test_signer_endpoint_defaults_follow_the_backend(monkeypatch) -> None:
    from biznotes_core.services.skills import sandbox

    s = get_settings()
    monkeypatch.setattr(s, "skill_script_storage_endpoint", "")
    monkeypatch.setattr(s, "api_base_url", "http://localhost:28000")
    monkeypatch.setattr(s, "s3_endpoint", "rustfs:9000")
    monkeypatch.setattr(s, "storage_backend", "local")
    assert sandbox._signer_endpoint() == "http://localhost:28000"
    monkeypatch.setattr(s, "storage_backend", "s3")
    assert sandbox._signer_endpoint() == "rustfs:9000"
    monkeypatch.setattr(s, "skill_script_storage_endpoint", "http://api:8000")
    assert sandbox._signer_endpoint() == "http://api:8000"


def test_sandbox_job_round_trip_through_the_endpoint(client: TestClient, monkeypatch) -> None:
    """에이전트 워커가 만든 URL로 샌드박스가 출처 검사를 통과해 받고 올린다."""
    import hashlib

    from biznotes_core.services.skills import sandbox
    from biznotes_core.workers.skill_sandbox import job as sandbox_job

    s = get_settings()
    monkeypatch.setattr(s, "skill_script_storage_endpoint", BASE)
    script = b"print('hi')"
    pkg = s.storage_bucket_skill_packages
    storage.put_object(pkg, "skills/s/versions/v/scripts/now.py", script)
    ttl = timedelta(minutes=5)
    raw = {
        "call_id": "00000000-0000-0000-0000-000000000001",
        "start_by": 9e9,
        "script": "scripts/now.py",
        "args": [],
        "files": [
            {
                "path": "scripts/now.py",
                "url": sandbox._presign("GET", pkg, "skills/s/versions/v/scripts/now.py", ttl),
                "size": len(script),
                "sha256": hashlib.sha256(script).hexdigest(),
            }
        ],
        "inputs": [],
        "output_slots": [
            sandbox._presign(
                "PUT", s.storage_bucket_artifacts, "run/scripts/c/0", ttl, max_bytes=64
            )
        ],
        "limits": {
            "wall_sec": 5,
            "cpu_sec": 5,
            "as_bytes": 64 << 20,
            "fsize_bytes": 1 << 20,
            "outputs_max_bytes": 64,
        },
    }
    origins = sandbox_job.origins_from_env({"SKILL_SANDBOX_STORAGE_ORIGINS": BASE})
    parsed = sandbox_job.parse_job(raw, origins)

    def get(url: str, limit: int) -> bytes:
        res = client.get(url)
        res.raise_for_status()
        return res.content[: limit + 1]

    def put(url: str, data: bytes) -> None:
        client.put(url, content=data).raise_for_status()

    assert get(parsed.files[0].url, parsed.files[0].size) == script
    put(parsed.output_slots[0], b"result")
    assert storage.get_object(s.storage_bucket_artifacts, "run/scripts/c/0") == b"result"
    # 다른 출처(예: S3 백엔드 서버)로 서명된 URL은 샌드박스가 받지 않는다
    with pytest.raises(sandbox_job.JobRejected):
        s3_only = {"SKILL_SANDBOX_STORAGE_ORIGINS": "http://rustfs:9000"}
        sandbox_job.parse_job(raw, sandbox_job.origins_from_env(s3_only))
