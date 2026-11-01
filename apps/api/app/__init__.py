"""설정 클래스 등록을 코어 모듈 import보다 먼저 보장한다.

코어 모듈 다수가 import 시점에 `get_settings()`를 호출한다. 이 패키지의
어떤 하위 모듈을 import하든 `app/__init__.py`가 먼저 실행되므로, 여기서
`app.core.config`를 import해 두면 등록이 항상 앞선다. 등록이 늦으면
`set_settings_class`가 예외를 던진다.
"""

from app.core import config as _config  # noqa: F401
