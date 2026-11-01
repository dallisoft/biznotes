"""json_validate 노드용 산출물 스키마 검증기 — ctx 무의존 순수 함수(R2-2 추출).

runtime.py에서 기계적으로 이동. 동작 변경 없음 — runtime.py는 이 모듈을 재수출하고
_exec_json_validate가 여기의 검증 함수/딕셔너리를 참조한다(R0-6 특성화 계약).
"""

import re
from collections.abc import Callable
from typing import Any

_MINDMAP_NODE_CAP = 200
# 노드 하나가 댈 수 있는 근거 번호 상한(#303) — 넘는 번호는 버리고 센다
_MINDMAP_CITE_CAP = 8
# 정수로 읽는 근거 번호 문자열 — 길이를 묶는다(`int()`는 4300자리를 넘으면 ValueError다)
_CITE_DIGITS = re.compile(r"[0-9]{1,9}")


def _validate_mindmap_shape(data: Any) -> str | None:
    """마인드맵 트리 스키마(phase-21-design.md §1.2) 검증. 실패 사유 문자열 또는 None(통과).

    스택 기반 반복 순회 — LLM이 병적으로 깊은(넓지 않은) 트리를 생성해도 재귀
    깊이 한도(RN21A-01, codex 리뷰)에 부딪히지 않고 절단 로직까지 도달한다.
    """
    if not isinstance(data, dict):
        return "최상위 값이 객체가 아닙니다"
    root = data.get("root")
    if not isinstance(root, dict):
        return "root 필드가 객체가 아닙니다"

    seen_ids: set[str] = set()
    stack: list[tuple[Any, str]] = [(root, "root")]
    while stack:
        n, path = stack.pop()
        if not isinstance(n, dict):
            return f"{path}가 객체가 아닙니다"
        node_id = n.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            return f"{path}.id가 비어있는 문자열입니다"
        # RN21A-02(codex 리뷰) — MindmapViewer가 id를 react-flow 노드 키로 쓰므로
        # 중복 id는 캔버스 렌더를 깨뜨린다. 스키마 검증 단계에서 명시적으로 거부한다.
        if node_id in seen_ids:
            return f"{path}.id가 트리 내에서 중복됩니다: {node_id}"
        seen_ids.add(node_id)
        if not isinstance(n.get("label"), str) or not n["label"].strip():
            return f"{path}.label이 비어있는 문자열입니다"
        children = n.get("children", [])
        if not isinstance(children, list):
            return f"{path}.children이 배열이 아닙니다"
        for i, child in enumerate(children):
            stack.append((child, f"{path}.children[{i}]"))
    return None


def _count_mindmap_nodes(root: dict[str, Any]) -> int:
    count = 0
    stack: list[dict[str, Any]] = [root]
    while stack:
        n = stack.pop()
        count += 1
        for child in n.get("children", []) or []:
            if isinstance(child, dict):
                stack.append(child)
    return count


def _truncate_mindmap_bfs(root: dict[str, Any], cap: int) -> tuple[dict[str, Any], bool]:
    """BFS 순서로 노드를 살리고 상한 초과분은 절단(D-11 승인값: 단순 BFS, 상위 레벨 우선 보존)."""
    kept = 1
    truncated = False
    queue: list[dict[str, Any]] = [root]
    while queue:
        node = queue.pop(0)
        raw_children = node.get("children", []) or []
        new_children: list[dict[str, Any]] = []
        for child in raw_children:
            if not isinstance(child, dict):
                continue
            if kept >= cap:
                truncated = True
                continue
            kept += 1
            new_children.append(child)
            queue.append(child)
        node["children"] = new_children
    return root, truncated


def _validate_citation_shape(c: Any, path: str) -> str | None:
    """flashcards/quiz 공통 citation 필드(phase-21-design.md §2.2) 검증."""
    if not isinstance(c, dict):
        return f"{path}.citation이 객체가 아닙니다"
    for field_name in ("chunk_id", "quote", "source_id", "source_title"):
        if not isinstance(c.get(field_name), str) or not c[field_name].strip():
            return f"{path}.citation.{field_name}이 비어있는 문자열입니다"
    return None


def _cite_numbers(raw: Any) -> tuple[list[int], int]:
    """`cite` 값 → (중복을 뺀 근거 번호, 버린 항목 수). 상한을 넘는 번호도 버린 수다."""
    if raw is None:
        return [], 0
    if not isinstance(raw, list):
        return [], 1
    numbers: list[int] = []
    malformed = 0
    for item in raw:
        if isinstance(item, int) and not isinstance(item, bool):
            numbers.append(item)
        elif isinstance(item, str) and _CITE_DIGITS.fullmatch(item.strip()):
            numbers.append(int(item.strip()))
        else:
            malformed += 1
    numbers = list(dict.fromkeys(numbers))
    if len(numbers) > _MINDMAP_CITE_CAP:
        malformed += len(numbers) - _MINDMAP_CITE_CAP
        numbers = numbers[:_MINDMAP_CITE_CAP]
    return numbers, malformed


def _resolve_mindmap_refs(root: dict[str, Any], evidence: list[Any]) -> tuple[int, int]:
    """노드 근거를 run 근거표의 서버 쌍으로 맞춘다(#303). 노드를 제자리에서 고치고
    `(해석한 노드 수, 버린 참조 수)`를 돌려준다.

    근거표는 `ctx.tool_evidence`의 `kind == "source"` 항목 중 `citation.n`(정수)이 있는 것이다
    (`rag_search`가 쌓는다, `_record_run_citations`). 번호는 run 안에서 유일하다.

    - `cite`: 선택 보조 정보라 형식 오류로 run을 실패시키지 않는다(사용자 결정). 정수와 정수로
      읽히는 문자열(`"2"`)을 받고, 그 밖의 항목·배열이 아닌 값·`_MINDMAP_CITE_CAP` 초과분은 버린
      수로 센다. 표에 있는 번호 중 첫 번호의 서버 `(source_id, chunk_id)`로 `source_ref`를
      덮어쓰고, 표에 없는 번호도 버린 수로 센다. `cite` 키는 저장 문서에서 지운다 — 번호는 run
      밖에서 해석할 수 없다.
    - 직접 쓴 `source_ref`(해석한 `cite`가 없을 때): 쌍 전체가 표의 한 항목과 같을 때만 그 두
      키로 남긴다. 노트북 소속만으로는 남기지 않는다 — 소속과 run 근거성은 다른 보장이다.
      `null`은 「없음」이라 키만 지우고 세지 않는다.
    - 표는 run 트리 전체다(하위 실행도 공유한다). 이미 번호가 붙은 청크를 하위 실행이 다시
      검색하면 그 번호를 재사용하고 표에 새 항목을 넣지 않으므로(`_record_run_citations`), 범위를
      하위 실행이 더한 항목으로 좁히면 하위 실행 LLM이 본 번호가 해석되지 않는다.
    - 절단 뒤에 부른다. 잘린 노드는 세지 않는다. 반복 순회라 깊은 트리도 재귀 한도에 걸리지 않는다.
    """
    by_number: dict[int, dict[str, str]] = {}
    pairs: set[tuple[str, str]] = set()
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") != "source":
            continue
        citation = item.get("citation")
        if not isinstance(citation, dict):
            continue
        number = citation.get("n")
        source_id, chunk_id = citation.get("source_id"), citation.get("chunk_id")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        if source_id is None or chunk_id is None:
            continue
        pair = (str(source_id), str(chunk_id))
        by_number.setdefault(number, {"source_id": pair[0], "chunk_id": pair[1]})
        pairs.add(pair)

    resolved = dropped = 0
    stack: list[dict[str, Any]] = [root]
    while stack:
        node = stack.pop()
        for child in node.get("children") or []:
            if isinstance(child, dict):
                stack.append(child)
        numbers, malformed = _cite_numbers(node.pop("cite", None))
        known = [k for k in numbers if k in by_number]
        dropped += malformed + len(numbers) - len(known)
        if known:
            node["source_ref"] = dict(by_number[known[0]])
            resolved += 1
            continue
        if node.get("source_ref", None) is None:
            node.pop("source_ref", None)  # 없음 — 세지 않는다
            continue
        ref = node["source_ref"]
        ref_source = ref.get("source_id") if isinstance(ref, dict) else None
        ref_chunk = ref.get("chunk_id") if isinstance(ref, dict) else None
        if (
            isinstance(ref_source, str)
            and isinstance(ref_chunk, str)
            and (ref_source, ref_chunk) in pairs
        ):
            node["source_ref"] = {"source_id": ref_source, "chunk_id": ref_chunk}
        else:
            del node["source_ref"]
            dropped += 1
    return resolved, dropped


def _validate_flashcards_shape(data: Any) -> str | None:
    """플래시카드 산출물 스키마(phase-21-design.md §2.2) 검증: 배열, 각 항목 필수 필드."""
    if not isinstance(data, list) or not data:
        return "최상위 값이 비어있지 않은 배열이 아닙니다"
    seen_ids: set[str] = set()
    for i, item in enumerate(data):
        path = f"[{i}]"
        if not isinstance(item, dict):
            return f"{path}가 객체가 아닙니다"
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            return f"{path}.id가 비어있는 문자열입니다"
        if item_id in seen_ids:
            return f"{path}.id가 중복됩니다: {item_id}"
        seen_ids.add(item_id)
        for field_name in ("question", "answer"):
            if not isinstance(item.get(field_name), str) or not item[field_name].strip():
                return f"{path}.{field_name}이 비어있는 문자열입니다"
        err = _validate_citation_shape(item.get("citation"), path)
        if err:
            return err
    return None


def _validate_quiz_shape(data: Any) -> str | None:
    """퀴즈 산출물 스키마(phase-21-design.md §2.2) 검증: 배열, 각 항목 필수 필드."""
    if not isinstance(data, list) or not data:
        return "최상위 값이 비어있지 않은 배열이 아닙니다"
    seen_ids: set[str] = set()
    for i, item in enumerate(data):
        path = f"[{i}]"
        if not isinstance(item, dict):
            return f"{path}가 객체가 아닙니다"
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            return f"{path}.id가 비어있는 문자열입니다"
        if item_id in seen_ids:
            return f"{path}.id가 중복됩니다: {item_id}"
        seen_ids.add(item_id)
        if not isinstance(item.get("question"), str) or not item["question"].strip():
            return f"{path}.question이 비어있는 문자열입니다"
        choices = item.get("choices")
        if not isinstance(choices, list) or len(choices) != 4:
            return f"{path}.choices가 4개 항목의 배열이 아닙니다"
        if not all(isinstance(c, str) and c.strip() for c in choices):
            return f"{path}.choices에 비어있는 항목이 있습니다"
        answer_index = item.get("answer_index")
        if not isinstance(answer_index, int) or isinstance(answer_index, bool):
            return f"{path}.answer_index가 정수가 아닙니다"
        if not (0 <= answer_index < 4):
            return f"{path}.answer_index가 0~3 범위를 벗어납니다"
        err = _validate_citation_shape(item.get("citation"), path)
        if err:
            return err
    return None


def _validate_slides_document(data: Any, slide_key: str) -> str | None:
    """슬라이드 문서 공통 최소 형태(#337): {title, slides: [{<slide_key>, ...}, ...]}.

    웹 뷰어는 장마다 제목만 있으면 그리고 모르는 layout·틀린 선택 필드는 버리거나 bullets로
    대체한다. 그래서 여기서는 최소 형태만 거부하고 format·layout·선택 필드·정의되지 않은 키는
    타입과 무관하게 통과시킨다. 정규화·절단은 하지 않는다.
    """
    if not isinstance(data, dict):
        return "최상위 값이 객체가 아닙니다"
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return "title이 비어있는 문자열입니다"
    slides = data.get("slides")
    if not isinstance(slides, list) or not slides:
        return "slides가 비어있지 않은 배열이 아닙니다"
    for i, slide in enumerate(slides):
        path = f"slides[{i}]"
        if not isinstance(slide, dict):
            return f"{path}가 객체가 아닙니다"
        value = slide.get(slide_key)
        if not isinstance(value, str) or not value.strip():
            return f"{path}.{slide_key}이 비어있는 문자열입니다"
    return None


def _validate_slide_outline_shape(data: Any) -> str | None:
    """슬라이드 자료 개요(slide-outline.json) 검증: 각 장 {title, summary?}."""
    return _validate_slides_document(data, "title")


def _validate_slide_deck_shape(data: Any) -> str | None:
    """슬라이드 자료(slide-deck.json) 검증: 각 장 {heading, layout?, ...선택 필드}."""
    return _validate_slides_document(data, "heading")


def _prune_slide_deck_images(data: dict[str, Any], prompt_max_chars: int) -> list[str]:
    """slide_deck 선택 필드 `images`(장별 삽화 요청)에서 잘못된 항목을 버린다(#345).

    `_validate_slide_deck_shape`를 통과한 문서에만 부른다. 삽화는 선택 기능이라 위반이 있어도
    덱을 실패시키지 않고 그 항목만 뺀다. 항목은 `{slide, prompt, ...}` 객체이고
    `slide`는 1~`len(slides)`의 정수(bool·실수 제외)이며 앞 항목과 겹치지 않는다. `prompt`는
    비어 있지 않은 `prompt_max_chars`자 이하 문자열이다. 겹치면 앞 항목을 남긴다.

    `images`가 없거나 null이면 손대지 않는다. 배열이 아니면 빈 배열로 바꾼다(loop가 객체·
    문자열을 항목 1개로 감싸지 않게). 정상 항목은 고치지 않고, 버릴 항목이 없으면 문서를
    그대로 둔다. 반환: 버린 항목마다 위치가 담긴 사유(값은 담지 않는다).
    """
    images = data.get("images")
    if images is None:
        return []
    if not isinstance(images, list):
        data["images"] = []
        return ["images가 배열이 아닙니다"]
    slide_count = len(data["slides"])
    kept: list[Any] = []
    seen: set[int] = set()
    reasons: list[str] = []
    for i, item in enumerate(images):
        path = f"images[{i}]"
        if not isinstance(item, dict):
            reasons.append(f"{path}가 객체가 아닙니다")
            continue
        slide = item.get("slide")
        if not isinstance(slide, int) or isinstance(slide, bool) or not 1 <= slide <= slide_count:
            reasons.append(f"{path}.slide가 1~{slide_count} 범위의 정수가 아닙니다")
            continue
        if slide in seen:
            reasons.append(f"{path}.slide가 앞 항목과 겹칩니다: {slide}")
            continue
        prompt = item.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            reasons.append(f"{path}.prompt가 비어있는 문자열입니다")
            continue
        if len(prompt) > prompt_max_chars:
            reasons.append(f"{path}.prompt가 {prompt_max_chars}자를 넘습니다")
            continue
        seen.add(slide)
        kept.append(item)
    if reasons:
        data["images"] = kept
    return reasons


# 스키마 이름 → 검증 함수. 21b의 flashcards/quiz 스키마는 이
# 딕셔너리에 추가되어 json_validate 노드를 공유한다(phase-21-design.md §1.3).
_JSON_VALIDATE_SCHEMAS: dict[str, Callable[[Any], str | None]] = {
    "mindmap": _validate_mindmap_shape,
    "flashcards": _validate_flashcards_shape,
    "quiz": _validate_quiz_shape,
    "slide_outline": _validate_slide_outline_shape,
    "slide_deck": _validate_slide_deck_shape,
}

# truncated 메타 필드는 트리 구조(dict)인 mindmap에만 적용 — flashcards/quiz는 배열이라
# 최상위에 필드를 얹을 수 없으므로 이 집합에 속한 스키마만 절단/메타 처리 대상이다.
_JSON_VALIDATE_DICT_SCHEMAS = {"mindmap"}
