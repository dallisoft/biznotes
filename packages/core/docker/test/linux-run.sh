#!/bin/sh
# 코어 작업 트리를 linux 컨테이너에서 비특권 uid로 실행한다 — 테스트·스모크 공용.
# 시험 이미지(docker/test.Dockerfile, biznotes-core-test:py312-node)에 /run.sh로 들어 있다.
#
#   docker run --rm --security-opt no-new-privileges:true -v "<코어 체크아웃>:/src:ro" \
#       biznotes-core-test:py312-node /run.sh -m pytest tests/ -q -p no:cacheprovider
#
# 이미지는 잠금 의존성을 /venv에 둔 것이면 된다(biznotes_core는 설치돼 있지 않아도 된다).
# launcher가 `python -I -m biznotes_core…`로 뜨므로 PYTHONPATH가 아니라 site-packages `.pth`로 사본을 얹는다.
# `.pth`는 root로 쓰고, 실행은 setpriv로 RUN_UID + no-new-privs로 내려간다(PID 1은 python이 된다).
#
# RUN_UID 기본값은 시험 전용 10999다. RLIMIT_NPROC(격리 시험은 nproc=64)는 컨테이너가 아니라
# 커널 전체에서 같은 uid의 스레드를 센다 — 같은 호스트에 uid 1000(제품 appuser)으로 도는 컨테이너가
# 있으면 node가 스레드를 못 만들어 SIGABRT로 죽는다(실측). 다른 uid가 필요하면 `-e RUN_UID=<uid>`.
# TIKTOKEN_CACHE_DIR를 주면 그대로 넘긴다(--network none일 때 캐시 마운트용).
set -e
uid="${RUN_UID:-10999}"
mkdir /tmp/w
cd /src && tar --exclude=./.venv --exclude=./dist --exclude=./.git -cf - . | tar -xf - -C /tmp/w
echo /tmp/w > /venv/lib/python3.12/site-packages/zz_biznotes_core_worktree.pth
chown -R "$uid:$uid" /tmp/w
cd /tmp/w
exec setpriv --reuid="$uid" --regid="$uid" --clear-groups --no-new-privs \
  env HOME=/tmp ${TIKTOKEN_CACHE_DIR:+TIKTOKEN_CACHE_DIR="$TIKTOKEN_CACHE_DIR"} /venv/bin/python "$@"
