"""제품 설정 클래스 등록을 코어 모듈 import보다 먼저 보장한다.

`python -m scripts.<x>`는 `app` 패키지를 거치지 않고 시작한다. 하위 모듈이 `biznotes_core`를
먼저 import하면 코어가 기본 설정으로 `get_settings()`를 캐시하고, 그 뒤의 `app` import(직접이든
`scripts.seed` 같은 다른 스크립트를 거쳐서든)가 `set_settings_class`에서 RuntimeError를 낸다.
패키지 `__init__`은 어떤 하위 모듈보다 먼저 실행되므로 여기서 `app`을 import한다(BL-177).
파일 경로로 실행되는 stdio MCP 서버(`demo_mcp_server.py`)는 이 파일을 거치지 않는다.
"""

import app  # noqa: F401
