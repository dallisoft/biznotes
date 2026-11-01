"""API 키 등 비밀값 AES-GCM 암호화 (KEK = ENCRYPTION_KEY 환경변수)."""

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from biznotes_core.config import get_settings


def _key() -> bytes:
    # 임의 길이 문자열 → 32바이트 키 유도
    return hashlib.sha256(get_settings().encryption_key.encode()).digest()


def encrypt(plain: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plain.encode(), None)
    return base64.b64encode(nonce + ct).decode()


def decrypt(token: str) -> str:
    raw = base64.b64decode(token)
    nonce, ct = raw[:12], raw[12:]
    return AESGCM(_key()).decrypt(nonce, ct, None).decode()
