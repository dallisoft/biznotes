"""같은 UID 프로세스의 `/proc/<pid>` 열람 차단 — 자격증명을 환경변수로 받는 워커용.

리눅스는 같은 UID의 프로세스끼리 `/proc/<pid>/environ`·`mem`을 읽게 한다. 워커가 비신뢰
네이티브 파서(rhwp 등)에 환경변수를 넘기지 않아도, 파서는 부모 워커의 environ에서
DB·스토리지 비밀을 읽을 수 있다. 프로세스를 non-dumpable로 두면 커널이 `CAP_SYS_PTRACE`
없는 접근을 거부한다.

non-dumpable은 fork로 상속되고 execve에서 풀린다 — 자식 파서 자신에게는 남지 않는다.
근거: `docs/plans/hwp-ingest/reviews/phase-1-security-findings.md` F2.
"""

import ctypes
import functools
import os
import sys

_PR_GET_DUMPABLE = 3
_PR_SET_DUMPABLE = 4


def supported() -> bool:
    return sys.platform.startswith("linux")


@functools.cache
def _libc() -> ctypes.CDLL:
    # 프로세스에 이미 링크된 libc 심볼을 쓴다 — find_library는 ldconfig 서브프로세스를 띄운다
    return ctypes.CDLL(None, use_errno=True)


def set_non_dumpable() -> bool:
    """Linux면 현재 프로세스를 non-dumpable로 바꾸고 True, 그 밖의 플랫폼은 False.

    Linux에서 실패하면 `OSError`를 던진다.
    """
    if not supported():
        return False
    try:
        rc = _libc().prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except AttributeError as exc:  # libc에 prctl 심볼이 없다
        raise OSError(f"prctl을 쓸 수 없습니다: {exc}") from exc
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return True


def is_dumpable() -> bool:
    """현재 프로세스가 dumpable인가(Linux 전용). 판정할 수 없으면 True — 통과시키지 않는다."""
    try:
        return bool(_libc().prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0)
    except (OSError, AttributeError):
        return True
