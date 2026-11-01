"""비밀 필드를 받는 라우터용 라우트 클래스 (#173).

FastAPI 기본 422(`RequestValidationError`)는 오류마다 입력값(`input`)을 싣는다. 필드가 빠지면
요청 본문 **전체**가, 본문이 객체가 아니면 원본이 실린다 — 비밀번호·API 키가 응답으로 돌아가
본문을 기록하는 프록시·APM·클라이언트 로그에 남는다. 필드 제약(`max_length`)이나 pydantic의
`hide_input_in_errors`·`SecretStr`로는 막히지 않는다(`errors()`가 여전히 `input`을 담는다).
"""

from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute

__all__ = ["RedactedValidationRoute"]

# 입력값이 섞일 수 있는 키 — `ctx.error`에는 사용자 정의 validator가 입력을 담은 메시지가 올 수 있다
_REDACTED_KEYS = frozenset({"input", "ctx"})


def _redact(errors: Sequence[Any]) -> list[Any]:
    return [
        {k: v for k, v in e.items() if k not in _REDACTED_KEYS} if isinstance(e, dict) else e
        for e in errors
    ]


class RedactedValidationRoute(APIRoute):
    """요청 검증 오류에서 입력값을 지운다 — 422 본문에 `loc`·`msg`·`type`만 남는다.

    비밀 필드를 받는 라우터에 `APIRouter(route_class=RedactedValidationRoute)`로 건다.
    `include_router`로 다른 라우터·앱에 중첩해도 라우트 클래스가 유지된다. 원래 오류의
    `body`(원본 요청 본문)도 넘기지 않으므로 앱의 예외 핸들러·로깅은 가린 오류만 본다.
    OpenAPI는 바뀌지 않는다.

    한계: `msg`·`loc`는 남긴다. 사용자 정의 validator가 메시지에 입력값을 넣으면 `msg`로
    나가고, dict 필드의 키는 `loc`에 실린다 — 비밀 값을 키나 validator 메시지에 담는
    스키마에는 이 클래스만으로 충분하지 않다.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def redacted_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = _redact(exc.errors())
            # except 블록 밖에서 던진다 — 원래 예외(입력값·본문을 담은)가 `__context__`로
            # 붙어 트레이스백 로깅에 실리지 않게
            raise RequestValidationError(errors)

        return redacted_handler
