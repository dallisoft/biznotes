"""태스크를 이름으로 발행한다 — 서비스·라우터의 공통 발행 경계 (#321).

서비스·라우터는 태스크 모듈을 import하지 않는다(`ARCHITECTURE.md` §2). 그렇다고 `send_task`만 쓰면
태스크 객체의 `apply_async`가 붙이던 태스크 단위 발행 옵션(`task_annotations`의 큐·만료·우선순위,
`ignore_result` 등)과 `task_always_eager`가 빠진다. 그래서 Celery 서명(signature)으로 발행한다.

- 이 프로세스에 태스크가 등록돼 있으면(워커, 또는 제품이 태스크 모듈을 import한 프로세스) 그
  태스크의 `apply_async`를 거친다 — 위 옵션과 eager 실행이 태스크를 직접 부를 때와 같다.
- 등록돼 있지 않으면 `celery_app.send_task`로 이름 발행한다. 큐는 같은 `task_routes`로 정해진다.
  이때 태스크 단위 annotation·eager는 적용되지 않는다 — 필요한 제품은 그 프로세스에서 태스크
  모듈을 import해 등록한다.
"""

from typing import Any

from biznotes_core.workers.celery_app import celery_app


def publish_task(name: str, *, args: list[Any], **options: Any) -> None:
    """`name` 태스크를 `args`로 발행한다. `options`는 `apply_async` 옵션(`countdown` 등)이다."""
    celery_app.signature(name, args=args, options=options).apply_async()
