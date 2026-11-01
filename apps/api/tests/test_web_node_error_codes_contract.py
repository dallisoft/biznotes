"""웹 노드 실패 문구 사전 ↔ 코어 노드 실패 코드 정본 대조 (core-v0.20.0-upgrade AC-2.3, D-11).

생성 계약의 `ExecutionLogOut.error_code`는 `string | null`이라(코드 enum 아님) 웹 사전의 누락을
typecheck로 잡을 수 없다. 모르는 코드는 화면에 일반 문구 「노드 실행에 실패했습니다」로만 보인다.
그래서 이 테스트가 웹 `apps/web/src/lib/node-error-labels.ts`의 `MESSAGES` 객체 리터럴 키를
읽어 코어 `biznotes_core.services.agents.node_errors.NODE_ERROR_CODES`(코드는 더하기만 한다)와
같은 집합인지 본다 — 코어 핀 상향으로 코드가 늘면 `make ci`가 여기서 실패한다(경고 사전 계약
`test_web_source_warnings_contract.py`와 같은 방식·같은 파서 — 주석·문자열 속 선언은 읽지 않고,
전개·계산 키·중복 키는 실패한다).
"""

from pathlib import Path

from biznotes_core.services.agents.node_errors import NODE_ERROR_CODES

from tests.test_web_source_warnings_contract import web_message_keys

NODE_ERROR_LABELS_TS = Path(__file__).resolve().parents[2] / "web/src/lib/node-error-labels.ts"


def test_web_node_error_labels_cover_core_node_error_codes() -> None:
    """웹 사전의 키 집합 == 코어 `NODE_ERROR_CODES`.

    v0.29.0: 8종 — #358 컨텍스트 초과, #422 출력 잘림."""
    web = web_message_keys(NODE_ERROR_LABELS_TS.read_text(encoding="utf-8"))
    assert set(web) == set(NODE_ERROR_CODES)
    assert len(web) == len(NODE_ERROR_CODES) == 8


def test_contract_fails_when_web_dictionary_misses_a_core_code() -> None:
    """음성 대조 — 코어 코드 하나가 빠진 사전은 집합이 달라 이 계약이 실패한다."""
    source = NODE_ERROR_LABELS_TS.read_text(encoding="utf-8")
    line = next(ln for ln in source.splitlines() if ln.strip().startswith("ssrf_blocked:"))
    web = web_message_keys(source.replace(line + "\n", ""))
    assert "ssrf_blocked" not in web
    assert set(web) != set(NODE_ERROR_CODES)
