"""인제스트 단계 산출물 — locations.json 형식·검증 (#244, 계획 §3.4)."""

import json
from typing import Any

import pytest

from biznotes_core.services.ingestion import stage_artifacts as sa
from biznotes_core.services.ingestion.parsers import Location

MD = "# 제목\n\n## Page 1\n첫 쪽\n\n## 표 1\n표 본문"
DIGEST = sa.parsed_digest(MD.encode())
LOCS = [Location(0, 10, page=1), Location(12, 20, section=0, paragraph=3, table_index=0)]


def _payload(**override: Any) -> bytes:
    body: dict[str, Any] = {
        "v": 1,
        "parsed_sha256": DIGEST,
        "locations": [[0, 10, 1, None, None, None]],
    }
    body.update(override)
    return json.dumps(body).encode()


def test_round_trip_keeps_every_coordinate() -> None:
    payload = sa.encode_locations(DIGEST, LOCS)

    assert sa.decode_locations(payload, markdown=MD, parsed_sha256=DIGEST) == LOCS


def test_encoding_is_deterministic_json_without_offsets_in_keys() -> None:
    a = sa.encode_locations(DIGEST, LOCS)

    assert a == sa.encode_locations(DIGEST, LOCS)
    assert json.loads(a)["locations"][1] == [12, 20, None, 0, 3, 0]


def test_other_generation_yields_no_locations() -> None:
    """다른 parsed.md 세대의 위치는 이 본문의 오프셋이 아니다 — 쓰지 않는다."""
    payload = sa.encode_locations(sa.parsed_digest(b"other"), LOCS)

    assert sa.decode_locations(payload, markdown=MD, parsed_sha256=DIGEST) == []


def test_digest_is_lowercase_sha256_hex() -> None:
    assert sa.is_digest(DIGEST)
    assert not sa.is_digest(DIGEST.upper())
    assert not sa.is_digest(DIGEST[:-1])
    assert not sa.is_digest(None)
    with pytest.raises(ValueError):
        sa.encode_locations("x" * 64, LOCS)


def test_keys_live_next_to_parsed_md() -> None:
    assert sa.locations_key("sid") == "sid/locations.json"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"\xff\xfe", id="not-utf8"),
        pytest.param(b"{not json", id="not-json"),
        pytest.param(b"[]", id="not-object"),
        pytest.param(_payload(v=2), id="version"),
        pytest.param(_payload(parsed_sha256="abc"), id="digest-format"),
        pytest.param(_payload(locations={"0": 1}), id="locations-not-list"),
        pytest.param(_payload(locations=[[0, 10, 1]]), id="short-item"),
        pytest.param(_payload(locations=[{"start": 0}]), id="item-not-list"),
        pytest.param(_payload(locations=[[6, 5, 1, None, None, None]]), id="reversed-span"),
        pytest.param(b"[" * 200_000 + b"]" * 200_000, id="deep-nesting"),
        pytest.param(_payload(locations=[[0, len(MD) + 1, 1, None, None, None]]), id="past-end"),
        pytest.param(_payload(locations=[[-1, 3, 1, None, None, None]]), id="negative-start"),
        pytest.param(
            _payload(locations=[[0, 10, 1, None, None, None], [5, 12, 2, None, None, None]]),
            id="overlap",
        ),
        pytest.param(
            _payload(locations=[[12, 20, 1, None, None, None], [0, 10, 2, None, None, None]]),
            id="unsorted",
        ),
        pytest.param(_payload(locations=[[0, 10, True, None, None, None]]), id="bool-coord"),
        pytest.param(_payload(locations=[[0, 10, -1, None, None, None]]), id="negative-coord"),
        pytest.param(_payload(locations=[[0, 10, 2**60, None, None, None]]), id="huge-coord"),
        pytest.param(_payload(locations=[[0, 10, "1", None, None, None]]), id="str-coord"),
        pytest.param(_payload(locations=[[0.0, 10, 1, None, None, None]]), id="float-offset"),
    ],
)
def test_malformed_artifact_is_rejected(payload: bytes) -> None:
    with pytest.raises(sa.InvalidStageArtifact):
        sa.decode_locations(payload, markdown=MD, parsed_sha256=DIGEST)


def test_size_cap_is_checked_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sa, "LOCATIONS_MAX_BYTES", 10)

    with pytest.raises(sa.InvalidStageArtifact, match="크기 상한"):
        sa.decode_locations(_payload(), markdown=MD, parsed_sha256=DIGEST)


def test_rejection_message_carries_no_artifact_content() -> None:
    """오류 문구가 로그로 나간다 — 비신뢰 산출물의 값을 싣지 않는다."""
    secret = "주민번호-900101"
    with pytest.raises(sa.InvalidStageArtifact) as info:
        sa.decode_locations(
            _payload(locations=[[0, 10, secret, None, None, None]]),
            markdown=MD,
            parsed_sha256=DIGEST,
        )

    assert secret not in str(info.value)


def test_empty_span_is_accepted_like_the_chunker() -> None:
    """빈 구간은 청커가 받는다(`chunker._check_locations`) — 산출물 검증도 같은 규칙이다."""
    payload = sa.encode_locations(DIGEST, [Location(5, 5, page=1)])

    assert sa.decode_locations(payload, markdown=MD, parsed_sha256=DIGEST) == [Location(5, 5, 1)]
