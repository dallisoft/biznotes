"""FastAPI OpenAPI 스키마를 서버 기동 없이 JSON 파일로 내보낸다.

사용: uv run python -m scripts.export_openapi <output_path>
"""

import json
import sys
from pathlib import Path

from app.main import app


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("openapi.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2))
    print(f"OpenAPI schema written to {out}")


if __name__ == "__main__":
    main()
