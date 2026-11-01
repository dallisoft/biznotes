"""게이트웨이 이미지 생성 경로 (modality: image, #338).

LLM·오디오와 같이 모든 호출이 게이트웨이를 경유한다. mock 프로바이더는 외부 키 없이
파이프라인을 결정적으로 검증하기 위한 경로다.

상한은 모든 환경에 같은 안전 상한(모듈 상수)이다. 산출물 MIME은 프로바이더가 말한 값이
아니라 바이트(매직 넘버)로 정하며 PNG·JPEG만 받는다 — 산출물 다운로드가 인라인으로 보여 주는
이미지 형식이 이 둘뿐이다(`api/v1/artifacts.py`).
"""

import base64
import binascii
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from biznotes_core.core.ssrf import SSRFBlockedError, ssrf_safe_request
from biznotes_core.services.gateway.llm import GatewayCallError, GatewayError, ResolvedModel

logger = structlog.get_logger()

IMAGE_MAX_COUNT = 4  # 비용 가드 — 1회 호출(노드)당 장수 상한
IMAGE_PROMPT_MAX_CHARS = 4000
IMAGE_MAX_BYTES = 20 * 1024 * 1024  # 장당 디코드 뒤 바이트 상한(메모리·저장 가드)
IMAGE_SIZES = ("1024x1024", "1536x1024", "1024x1536")
# 비율 → 요청 크기. 프로바이더가 크기를 비율로 바꿔 받는 경우(Gemini 등)도 litellm이 맞춘다
IMAGE_ASPECT_RATIOS = {
    "1:1": "1024x1024",
    "3:2": "1536x1024",
    "16:9": "1536x1024",
    "2:3": "1024x1536",
    "9:16": "1024x1536",
}
# URL 응답을 받을 때 홉별 시간 상한(초)
_URL_FETCH_TIMEOUT = 30.0

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
# JPEG 프레임 시작(SOFn) 표식 — C4(DHT)·C8(JPG 예약)·CC(DAC)는 프레임이 아니다
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


@dataclass
class GeneratedImage:
    data: bytes
    content_type: str  # "image/png" | "image/jpeg"


@dataclass
class GeneratedImages:
    images: list[GeneratedImage]
    # 프로바이더가 준 토큰 사용량(prompt_tokens·completion_tokens·total_tokens). 없으면 빈 dict
    usage: dict[str, int] = field(default_factory=dict)


def _png_ok(data: bytes) -> bool:
    """청크를 끝까지 걷는다 — 길이·CRC가 맞고 IHDR(13바이트, 가로·세로 > 0)로 시작해 IDAT를 거쳐
    IEND로 정확히 끝나야 한다."""
    pos = len(_PNG_MAGIC)
    kinds: list[bytes] = []
    while pos + 12 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        end = pos + 12 + length
        if end > len(data):
            return False
        kind, payload = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        if struct.unpack(">I", data[end - 4 : end])[0] != zlib.crc32(kind + payload):
            return False
        if not kinds:
            if kind != b"IHDR" or length != 13 or 0 in struct.unpack(">II", payload[:8]):
                return False
        kinds.append(kind)
        pos = end
        if kind == b"IEND":
            return pos == len(data) and b"IDAT" in kinds
    return False


def _jpeg_ok(data: bytes) -> bool:
    """SOI 뒤 세그먼트를 SOS까지 걷는다 — 프레임(SOFn)이 SOS 앞에 있고, SOS 뒤 압축 데이터가
    비어 있지 않으며 EOI로 끝나야 한다."""
    pos, frame = 2, False
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            return False
        marker = data[pos + 1]
        if marker == 0xFF:  # 채움 바이트
            pos += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # 길이 없는 표식
            pos += 2
            continue
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        if length < 2 or pos + 2 + length > len(data):
            return False
        segment = data[pos + 4 : pos + 2 + length]
        if marker in _JPEG_SOF:
            # 정밀도·세로·가로·성분 수 + 성분마다 3바이트 — 가로·성분 수가 0이면 그릴 수 없다
            if len(segment) < 6:
                return False
            width, components = struct.unpack(">H", segment[3:5])[0], segment[5]
            if width == 0 or components == 0 or len(segment) != 6 + 3 * components:
                return False
            frame = True
        if marker == 0xDA:  # SOS — 이후는 압축 데이터
            # 성분 수 + 성분마다 2바이트 + 스펙트럼·근사 3바이트
            if not segment or segment[0] == 0 or len(segment) != 1 + 2 * segment[0] + 3:
                return False
            scan = pos + 2 + length
            return frame and len(data) - scan > 2 and data.endswith(b"\xff\xd9")
        pos += 2 + length
    return False


def image_content_type(data: bytes) -> str | None:
    """바이트로 판정한 MIME — PNG·JPEG가 아니거나 구조가 깨졌으면 None.

    픽셀 디코드는 하지 않고 컨테이너 구조만 확인한다(PNG 청크·CRC, JPEG 세그먼트·프레임·스캔·EOI).
    시그니처만 있거나 잘린 바이트를 성공으로 저장하지 않는다.
    """
    if data.startswith(_PNG_MAGIC):
        return "image/png" if _png_ok(data) else None
    if data.startswith(_JPEG_MAGIC):
        return "image/jpeg" if _jpeg_ok(data) else None
    return None


def check_image_request(prompt: str, n: int, size: str) -> None:
    """호출 전 상한 검사 — 위반은 작성자에게 보이는 `GatewayError`다."""
    if not prompt.strip():
        raise GatewayError("이미지 프롬프트가 비어 있습니다")
    if len(prompt) > IMAGE_PROMPT_MAX_CHARS:
        raise GatewayError(
            f"이미지 프롬프트가 상한({IMAGE_PROMPT_MAX_CHARS}자)을 초과합니다: {len(prompt)}자"
        )
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= IMAGE_MAX_COUNT:
        raise GatewayError(f"이미지 장수는 1~{IMAGE_MAX_COUNT} 정수여야 합니다: {n!r}")
    if size not in IMAGE_SIZES:
        raise GatewayError(f"이미지 크기는 {', '.join(IMAGE_SIZES)} 중 하나여야 합니다: {size!r}")


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _mock_png(index: int, side: int = 8) -> bytes:
    """단색 RGB PNG — mock 이미지 산출물(열 수 있는 유효 파일). 장마다 색이 다르다."""
    color = bytes(((index * 80) % 256, 128, (255 - index * 80) % 256))
    raw = b"".join(b"\x00" + color * side for _ in range(side))  # 행마다 필터 0
    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (
        _PNG_MAGIC
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


async def _fetch_url(url: str) -> bytes:
    """URL 응답 이미지를 받는다 — 공인 IP만, 장당 바이트 상한(SSRF 관문)."""
    try:
        res = await ssrf_safe_request(
            "GET", url, allow_private=False, timeout=_URL_FETCH_TIMEOUT, max_bytes=IMAGE_MAX_BYTES
        )
    except SSRFBlockedError as e:
        raise GatewayCallError(f"이미지 URL을 받을 수 없습니다: {e}") from e
    except httpx.HTTPError as e:
        raise GatewayCallError(f"이미지 URL 요청 실패: {e}") from e
    if res.status_code != 200:
        raise GatewayCallError(f"이미지 URL 응답이 실패했습니다: HTTP {res.status_code}")
    return res.content


async def _decode_item(item: Any) -> bytes:
    b64 = getattr(item, "b64_json", None)
    if b64:
        # 디코드 전에 크기를 묶는다 — base64는 3바이트를 4글자로 쓴다
        if len(b64) > (IMAGE_MAX_BYTES // 3 + 1) * 4:
            raise GatewayCallError(f"생성된 이미지가 상한({IMAGE_MAX_BYTES} bytes)을 넘습니다")
        try:
            return base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise GatewayCallError(f"생성된 이미지를 해석할 수 없습니다: {e}") from e
    url = getattr(item, "url", None)
    if url:
        return await _fetch_url(str(url))
    raise GatewayCallError("이미지 응답에 데이터가 없습니다")


def _usage(res: Any) -> dict[str, int]:
    usage = getattr(res, "usage", None)
    if usage is None:
        return {}
    pairs = (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
    )
    out: dict[str, int] = {}
    for key, attr in pairs:
        value = getattr(usage, attr, None)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            out[key] = value
    return out


async def generate_images(
    resolved: ResolvedModel, prompt: str, *, n: int = 1, size: str = "1024x1024"
) -> GeneratedImages:
    """프롬프트 → 이미지 `n`장. 받은 장수가 `n`보다 적으면 받은 만큼 돌려준다.

    응답은 base64를 요청하고, 프로바이더가 URL만 주면 SSRF 관문으로 받는다.
    PNG·JPEG가 아니거나 장당 상한을 넘거나 요청보다 많이 오면 `GatewayCallError`다.
    """
    check_image_request(prompt, n, size)
    if resolved.provider.type == "mock":
        return GeneratedImages(images=[GeneratedImage(_mock_png(i), "image/png") for i in range(n)])

    import litellm

    try:
        res = await litellm.aimage_generation(
            prompt=prompt,
            n=n,
            size=size,
            response_format="b64_json",
            # 프로바이더가 받지 않는 파라미터(response_format 등)는 litellm이 뺀다(#224 관례)
            drop_params=True,
            **resolved.litellm_params,
        )
    except Exception as e:
        logger.error("image generation failed", model=resolved.model.model_name, error=str(e))
        raise GatewayCallError(f"이미지 생성 호출 실패: {e}") from e

    items = list(getattr(res, "data", None) or [])
    if not items:
        raise GatewayCallError("이미지 생성 응답에 이미지가 없습니다")
    if len(items) > n:
        raise GatewayCallError(f"요청({n}장)보다 많은 이미지가 왔습니다: {len(items)}장")

    images: list[GeneratedImage] = []
    for item in items:
        data = await _decode_item(item)
        if len(data) > IMAGE_MAX_BYTES:
            raise GatewayCallError(f"생성된 이미지가 상한({IMAGE_MAX_BYTES} bytes)을 넘습니다")
        content_type = image_content_type(data)
        if content_type is None:
            raise GatewayCallError("생성된 이미지가 PNG·JPEG가 아니거나 잘렸습니다")
        images.append(GeneratedImage(data, content_type))

    return GeneratedImages(images=images, usage=_usage(res))
