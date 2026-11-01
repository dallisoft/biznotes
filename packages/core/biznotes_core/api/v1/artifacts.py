"""에이전트 산출물 다운로드 응답 — 안전한 서빙 규칙의 단일 출처 (표준 계획 D9, 위협 모델 T8).

다운로드 엔드포인트는 두 제품이 소유한다. 산출물은 스킬 지시문이 만든 내용이라 같은 origin에서
능동 콘텐츠로 열리면 저장형 XSS가 된다. 두 제품이 이 함수 하나로 응답을 만들어 규칙이 갈리지
않게 한다.

- inline 허용: `text/plain`·`text/markdown`·`image/png`·`image/jpeg`·`application/pdf`
- 그 밖은 `attachment`. html·svg·알 수 없는 타입은 `application/octet-stream`으로 강등한다.
- 모든 응답에 `X-Content-Type-Options: nosniff`.
"""

from fastapi.responses import Response

from biznotes_core.api.v1.skills import content_disposition_attachment

INLINE_ARTIFACT_TYPES: frozenset[str] = frozenset(
    {"text/plain", "text/markdown", "image/png", "image/jpeg", "application/pdf"}
)
# attachment로 내리되 타입은 유지하는 수동(passive) 타입 — 오디오 재생·내려받은 파일 연결에 쓴다
_PASSIVE_TYPES: frozenset[str] = frozenset(
    {"application/json", "text/csv", "application/zip", "application/octet-stream"}
)
_PASSIVE_PREFIXES: tuple[str, ...] = (
    "audio/",
    "video/",
    "application/vnd.openxmlformats-officedocument.",
)


def artifact_media_type(content_type: str | None) -> tuple[str, bool]:
    """(응답 media type, inline 여부). 매개변수(`; charset=…`)는 떼고 판정한다."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media in INLINE_ARTIFACT_TYPES:
        if media.startswith("text/"):
            return f"{media}; charset=utf-8", True
        return media, True
    if media in _PASSIVE_TYPES or media.startswith(_PASSIVE_PREFIXES):
        return media, False
    return "application/octet-stream", False


def artifact_response(data: bytes, name: str, content_type: str | None) -> Response:
    """산출물 바이트를 D9 규칙의 응답으로 만든다. 바이트는 바꾸지 않는다."""
    media_type, inline = artifact_media_type(content_type)
    headers = {"X-Content-Type-Options": "nosniff"}
    if not inline:
        headers["Content-Disposition"] = content_disposition_attachment(name)
    return Response(content=data, media_type=media_type, headers=headers)
