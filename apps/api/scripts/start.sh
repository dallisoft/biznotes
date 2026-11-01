#!/bin/sh
# api 컨테이너 진입점 — 마이그레이션을 끝까지 적용한 뒤 받은 명령(uvicorn)으로 바뀐다.
#
# BIZNOTES_AUTO_MIGRATE(기본 true)가 켜져 있으면 `alembic upgrade head`를 먼저 돈다. 실패하면
# api를 띄우지 않는다 — 스키마가 모자란 채로 요청을 받지 않게 한다. api 복제본을 여럿 띄우는
# 배포에서는 끄고(false) 마이그레이션을 따로 한 번 돌린다.
set -eu

case "${BIZNOTES_AUTO_MIGRATE:-true}" in
  true | 1 | yes) alembic upgrade head ;;
  false | 0 | no) ;;
  *)
    echo "BIZNOTES_AUTO_MIGRATE는 true 또는 false여야 합니다: ${BIZNOTES_AUTO_MIGRATE}" >&2
    exit 64
    ;;
esac

exec "$@"
