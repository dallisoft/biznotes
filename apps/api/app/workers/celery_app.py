"""Celery 앱 — 정본은 `biznotes_core.workers.celery_app`.

이 shim을 남기는 이유는 두 가지다.

1. **워커 진입점이 `biznotes_core.workers.celery_app`이다.** 이 모듈을 거치면 `app`
   패키지의 `__init__`이 먼저 실행되어 설정 클래스 등록이 보장된다. 코어의
   celery_app은 import 시점에 `Celery(...)`를 만들며 설정을 읽으므로, 등록이
   늦으면 브로커 URL이 코어 기본값으로 굳는다.
2. 태스크 모듈(`app.workers.tasks.*`)이 아직 이 프로젝트에 있고, 그 이름이
   브로커 계약이라 코어가 그대로 참조한다.

앱 이름은 `Settings.celery_app_name`(= "biznotes")으로 주입된다.
"""

from biznotes_core.workers.celery_app import celery_app, ping

__all__ = ["celery_app", "ping"]
