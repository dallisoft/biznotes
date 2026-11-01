"""인제스트 단계 사이의 산출물 — 파싱 태스크가 쓰고 색인 태스크가 읽는다 (#244).

파싱(HWP는 egress가 막힌 전용 워커)과 청킹·임베딩·색인(일반 워커)이 다른 태스크라서, 파싱
결과 중 parsed.md 밖에 있는 것을 저장해 넘긴다. 지금은 `locations`(쪽·표 위치) 하나다.

- **세대 = parsed.md 바이트의 SHA-256**(`parsed_digest`). 파싱 태스크가 핸드오프 메시지에 싣고,
  색인 태스크가 지금 parsed.md와 대조한다. locations.json에도 같은 해시를 적는다 — 해시가 다른
  위치는 다른 세대의 것이라 쓰지 않는다.
- 일반 워커가 **비신뢰 파서의 산출물**을 읽는다. JSON만 쓰고(역직렬화 없음) 크기 상한과
  스키마를 검사한다. 어긋나면 위치 없이 진행한다 — 위치는 부가 메타라 가용성을 지킨다.

계획: `docs/plans/ingest-stage-tasks/plan.md` §3.4. DB·모델 무의존 순수 모듈이다.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from typing import Any

from biznotes_core.services.ingestion.parsers import Location

LOCATIONS_ARTIFACT = "locations.json"
LOCATIONS_SCHEMA_VERSION = 1
# locations.json 바이트 상한 — HWP 조립의 블록 수(쪽·표)보다 훨씬 크다
LOCATIONS_MAX_BYTES = 8 * 1024 * 1024
# 색인 태스크가 읽는 parsed.md 바이트 상한 — 업로드 상한(50 MiB)·HWP 본문·부록 상한보다 크다
PARSED_MAX_BYTES = 64 * 1024 * 1024

_DIGEST_RE = re.compile(r"[0-9a-f]{64}")


class InvalidStageArtifact(ValueError):
    """산출물이 형식·상한을 벗어났다 — 메시지에 산출물 내용을 넣지 않는다."""


def locations_key(source_id: Any) -> str:
    return f"{source_id}/{LOCATIONS_ARTIFACT}"


def parsed_digest(data: bytes) -> str:
    """parsed.md 바이트의 세대 식별자(소문자 hex 64자)."""
    return hashlib.sha256(data).hexdigest()


def is_digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def encode_locations(parsed_sha256: str, locations: Sequence[Location]) -> bytes:
    """locations.json 본문 — `[start, end, page, section, paragraph, table_index]` 배열 목록."""
    if not is_digest(parsed_sha256):
        raise ValueError("parsed_sha256은 소문자 hex 64자여야 한다")
    body = {
        "v": LOCATIONS_SCHEMA_VERSION,
        "parsed_sha256": parsed_sha256,
        "locations": [
            [loc.start, loc.end, loc.page, loc.section, loc.paragraph, loc.table_index]
            for loc in locations
        ],
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


# 좌표 상한 — 청크 meta는 JSONB·검색 결과 payload·JSON 응답으로 나간다(JS 안전 정수)
_MAX_COORD = 2**53 - 1


def _int(value: Any) -> bool:
    return type(value) is int and 0 <= value <= _MAX_COORD


def _coord(value: Any) -> bool:
    return value is None or _int(value)


def decode_locations(payload: bytes, *, markdown: str, parsed_sha256: str) -> list[Location]:
    """locations.json을 검증해 읽는다.

    반환: 위치 목록. **세대가 다르면**(`parsed_sha256` 불일치) 빈 목록이다 — 다른 세대의
    위치는 이 parsed.md의 오프셋이 아니다. 형식·상한 위반은 `InvalidStageArtifact`.
    """
    if len(payload) > LOCATIONS_MAX_BYTES:
        raise InvalidStageArtifact("locations.json이 크기 상한을 넘는다")
    try:
        body = json.loads(payload.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        # UnicodeDecodeError·JSONDecodeError·4300자리 넘는 정수는 ValueError,
        # 깊은 중첩은 RecursionError다
        raise InvalidStageArtifact("locations.json이 JSON이 아니다") from e
    if not isinstance(body, dict) or body.get("v") != LOCATIONS_SCHEMA_VERSION:
        raise InvalidStageArtifact("locations.json 스키마 버전이 다르다")
    digest = body.get("parsed_sha256")
    if not is_digest(digest):
        raise InvalidStageArtifact("locations.json의 parsed_sha256 형식이 틀렸다")
    if digest != parsed_sha256:
        return []
    items = body.get("locations")
    if not isinstance(items, list):
        raise InvalidStageArtifact("locations.json의 locations가 목록이 아니다")
    limit = len(markdown)
    out: list[Location] = []
    prev_end = 0
    for item in items:
        if not isinstance(item, list) or len(item) != 6:
            raise InvalidStageArtifact("위치 항목은 길이 6인 배열이어야 한다")
        start, end, page, section, paragraph, table_index = item
        # 빈 구간(start == end)은 청커(`_check_locations`)가 받는다 — 같은 규칙을 쓴다
        if not (_int(start) and _int(end) and start <= end <= limit):
            raise InvalidStageArtifact("위치 구간이 parsed.md 범위를 벗어난다")
        if start < prev_end:
            # 조립 불변식: start 오름차순·비중첩(계획 A §3.3)
            raise InvalidStageArtifact("위치 구간이 정렬되지 않았거나 겹친다")
        if not all(_coord(v) for v in (page, section, paragraph, table_index)):
            raise InvalidStageArtifact("위치 좌표는 None 또는 0 이상 정수여야 한다")
        out.append(Location(start, end, page, section, paragraph, table_index))
        prev_end = end
    return out
