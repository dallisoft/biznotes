#!/usr/bin/env bash
# 배포 후 자동 스모크 테스트: 로그인 → 노트북 생성 → 텍스트 소스 → ready → 질의 1건
# 사용: API_URL=https://api.example.com ADMIN_EMAIL=... ADMIN_PASSWORD=... ./smoke.sh
set -euo pipefail

API_URL="${API_URL:-http://localhost:28000}"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@biznotes.dev}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin1234!}"
CJ=$(mktemp)
trap 'rm -f "$CJ"' EXIT

fail() { echo "SMOKE FAIL: $1" >&2; exit 1; }

echo "[1/6] readyz"
curl -sf -m 10 "$API_URL/readyz" | grep -q '"status":"ok"' || fail "readyz"

echo "[2/6] login"
curl -sf -m 10 -c "$CJ" -X POST "$API_URL/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" > /dev/null || fail "login"

echo "[3/6] notebook create"
NB_ID=$(curl -sf -m 10 -b "$CJ" -X POST "$API_URL/api/v1/notebooks" \
  -H 'Content-Type: application/json' -d '{"title":"smoke-test"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])") || fail "notebook"

echo "[4/6] text source"
SRC_ID=$(curl -sf -m 10 -b "$CJ" -X POST "$API_URL/api/v1/notebooks/$NB_ID/sources" \
  -H 'Content-Type: application/json' \
  -d '{"type":"text","title":"smoke","text":"스모크 테스트 본문: 에스프레소는 9기압으로 추출한다."}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])") || fail "source"

echo "[5/6] wait ready (max 120s)"
for _ in $(seq 1 60); do
  ST=$(curl -sf -m 10 -b "$CJ" "$API_URL/api/v1/sources/$SRC_ID" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")
  [ "$ST" = "ready" ] && break
  [ "$ST" = "failed" ] && fail "ingestion failed"
  sleep 2
done
[ "$ST" = "ready" ] || fail "ingestion timeout"

echo "[6/6] search query"
HITS=$(curl -sf -m 30 -b "$CJ" -X POST "$API_URL/api/v1/notebooks/$NB_ID/search" \
  -H 'Content-Type: application/json' -d '{"query":"에스프레소 압력","limit":3}' \
  | python3 -c "import json,sys; print(len(json.load(sys.stdin)))")
[ "$HITS" -ge 1 ] || fail "search returned no hits"

# 정리
curl -sf -m 10 -b "$CJ" -X DELETE "$API_URL/api/v1/notebooks/$NB_ID" > /dev/null || true

echo "SMOKE OK"
