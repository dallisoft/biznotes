"""로컬 스토리지 서명 URL 토큰 — 객체 하나·메서드 하나·만료를 HMAC으로 묶는다."""

import base64
import hashlib
import hmac
import json
from datetime import timedelta

import pytest

from biznotes_core.config import get_settings
from biznotes_core.services import storage

NOW = 1_800_000_000.0


def _forge(claims: dict) -> str:
    """서명 키를 아는 쪽만 만들 수 있는 토큰 — 검증기가 서명 뒤의 모양도 보는지 확인용."""
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    sig = hmac.new(storage._token_key(), body.encode(), hashlib.sha256).digest()
    return body + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


def test_get_token_round_trip() -> None:
    token = storage.sign_object_token(
        "GET", "artifacts", "run/scripts/c/0", timedelta(minutes=5), now=NOW
    )
    signed = storage.verify_object_token(token, "GET", now=NOW + 299)
    assert signed == storage.SignedObject("GET", "artifacts", "run/scripts/c/0", None)


def test_put_token_carries_body_limit() -> None:
    token = storage.sign_object_token(
        "PUT", "artifacts", "run/0", timedelta(minutes=5), max_bytes=1024, now=NOW
    )
    assert storage.verify_object_token(token, "PUT", now=NOW).max_bytes == 1024


def test_put_token_requires_a_body_limit() -> None:
    with pytest.raises(ValueError):
        storage.sign_object_token("PUT", "artifacts", "k", timedelta(minutes=5))
    with pytest.raises(ValueError):
        storage.sign_object_token("DELETE", "artifacts", "k", timedelta(minutes=5))


def test_unicode_keys_survive_the_token() -> None:
    token = storage.sign_object_token("GET", "skill-packages", "s/v/참고 자료.md", timedelta(1))
    assert storage.verify_object_token(token, "GET").key == "s/v/참고 자료.md"


def test_expired_token_is_rejected() -> None:
    token = storage.sign_object_token("GET", "artifacts", "k", timedelta(seconds=60), now=NOW)
    with pytest.raises(storage.InvalidObjectToken, match="expired"):
        storage.verify_object_token(token, "GET", now=NOW + 61)


def test_token_is_bound_to_its_method() -> None:
    get = storage.sign_object_token("GET", "artifacts", "k", timedelta(minutes=5), now=NOW)
    with pytest.raises(storage.InvalidObjectToken, match="method"):
        storage.verify_object_token(get, "PUT", now=NOW)


@pytest.mark.parametrize("tamper", ["body", "sig", "swap"])
def test_tampered_token_is_rejected(tamper: str) -> None:
    token = storage.sign_object_token("GET", "artifacts", "run/a", timedelta(minutes=5), now=NOW)
    other = storage.sign_object_token("GET", "artifacts", "run/b", timedelta(minutes=5), now=NOW)
    body, sig = token.split(".")
    if tamper == "body":
        claims = json.loads(storage._unb64(body))
        claims["k"] = "other-user/secret"
        token = storage._b64(json.dumps(claims).encode()) + "." + sig
    elif tamper == "sig":
        token = body + "." + sig[:-2] + ("AA" if not sig.endswith("AA") else "BB")
    else:
        token = body + "." + other.split(".")[1]
    with pytest.raises(storage.InvalidObjectToken, match="signature"):
        storage.verify_object_token(token, "GET", now=NOW)


def test_token_signed_with_another_secret_is_rejected(monkeypatch) -> None:
    token = storage.sign_object_token("GET", "artifacts", "k", timedelta(minutes=5), now=NOW)
    monkeypatch.setattr(get_settings(), "jwt_secret", "rotated-secret")
    with pytest.raises(storage.InvalidObjectToken, match="signature"):
        storage.verify_object_token(token, "GET", now=NOW)


@pytest.mark.parametrize(
    "claims",
    [
        {"m": "GET", "b": "artifacts", "k": "../../etc/passwd", "e": int(NOW) + 60},
        {"m": "GET", "b": "../data", "k": "k", "e": int(NOW) + 60},
        {"m": "GET", "b": "artifacts", "k": "/abs", "e": int(NOW) + 60},
    ],
)
def test_validly_signed_but_escaping_key_is_rejected(claims: dict) -> None:
    with pytest.raises(storage.InvalidObjectToken, match="key"):
        storage.verify_object_token(_forge(claims), "GET", now=NOW)


@pytest.mark.parametrize(
    "claims",
    [
        {"m": "GET", "b": "artifacts", "k": "k", "e": "soon"},
        {"m": "GET", "b": "artifacts", "k": "k", "e": True},
        {"m": "GET", "b": 1, "k": "k", "e": int(NOW) + 60},
        {"m": "PUT", "b": "artifacts", "k": "k", "e": int(NOW) + 60},  # 상한 없음
        {"m": "PUT", "b": "artifacts", "k": "k", "e": int(NOW) + 60, "n": -1},
    ],
)
def test_malformed_claims_are_rejected(claims: dict) -> None:
    with pytest.raises(storage.InvalidObjectToken):
        storage.verify_object_token(_forge(claims), claims["m"], now=NOW)


@pytest.mark.parametrize("token", ["", "no-dot", ".sig", "body.", "a.b.c", "x" * 9000 + ".y"])
def test_garbage_tokens_are_rejected(token: str) -> None:
    with pytest.raises(storage.InvalidObjectToken):
        storage.verify_object_token(token, "GET", now=NOW)


def test_local_presigned_url_points_at_the_internal_endpoint(local_storage) -> None:
    url = storage.presigned_url_for(
        "PUT",
        "artifacts",
        "run/scripts/c/0",
        expires=timedelta(minutes=5),
        endpoint="http://api:8000/",
        region="us-east-1",
        max_bytes=10,
    )
    prefix = "http://api:8000/internal/storage/"
    assert url.startswith(prefix)
    signed = storage.verify_object_token(url.removeprefix(prefix), "PUT")
    assert (signed.bucket, signed.key, signed.max_bytes) == ("artifacts", "run/scripts/c/0", 10)


def test_local_presigned_url_refuses_unsafe_keys(local_storage) -> None:
    with pytest.raises(storage.InvalidObjectKey):
        storage.presigned_url_for(
            "GET",
            "artifacts",
            "../x",
            expires=timedelta(minutes=5),
            endpoint="http://api:8000",
            region="us-east-1",
        )
