"""통합 테스트 설정 — 로컬/CI의 실제 PostgreSQL 사용.

컨테이너 postgres가 호스트 25433으로 노출되어 있다고 가정 (compose.dev.yml).
CI에서는 services로 동일 포트 매핑.
"""

import fcntl
import json
import os
import resource
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# ---------------------------------------------------------------------------
# BL-01 워커별 테스트 자원 파생 (D-15). app import 전에 실행되어야 하므로
# 순수 헬퍼를 먼저 정의하고 즉시 호출한다 (테스트는 tests/test_conftest_isolation.py).
# ---------------------------------------------------------------------------

TEST_DB_PREFIX = "biznotes_test"
#: 비-xdist 실행이 쓰는 공유 테스트 DB — 세션 종료 시 DROP 대상이 아니다.
SHARED_TEST_DB = TEST_DB_PREFIX
DEFAULT_REDIS_URL = "redis://localhost:26379/15"
#: 브로커 URL 환경 변수 — 설정은 `VALKEY_URL`을 `REDIS_URL`보다 먼저 읽으므로 테스트도 이
#: 이름에 쓴다. 셸에 남은 `VALKEY_URL`이 워커별 DB 인덱스를 덮지 않게 한다.
BROKER_URL_ENV = "VALKEY_URL"
#: xdist 워커가 쓰는 Redis DB 인덱스 상한. 워커 n은 base - n 을 쓴다.
DEFAULT_REDIS_DB_BASE = "15"


def derive_test_db_name(run_uid: str, worker: str) -> str:
    """xdist 워커별 PostgreSQL DB 이름 — run-uid를 포함해 controller 간에도 겹치지 않는다."""
    return f"{TEST_DB_PREFIX}_{run_uid}_{worker}"


def derive_redis_db_index(worker: str, redis_db_base: str) -> int:
    """워커 번호를 Redis DB 인덱스로 환산. 인덱스가 1 미만이면 실행을 거부한다."""
    index = int(redis_db_base) - int(worker[2:])
    if index < 1:
        raise ValueError(
            f"Redis DB 인덱스가 범위를 벗어났습니다 (worker={worker}, "
            f"base={redis_db_base}, index={index}) — 워커 수는 14개 이하여야 합니다"
        )
    return index


def replace_redis_db_index(redis_url: str, index: int) -> str:
    """브로커 URL의 DB 인덱스만 교체 (호스트·포트·스킴은 보존)."""
    parts = urlsplit(redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{index}", parts.query, parts.fragment))


def worker_env_overrides(env: dict[str, str]) -> dict[str, str]:
    """xdist 실행이면 워커별 `POSTGRES_DB`·`VALKEY_URL` **명시 대입** 값을 돌려준다.

    비-xdist면 빈 dict — 호출자가 기존 `setdefault` 기본값을 유지한다.
    """
    worker = env.get("PYTEST_XDIST_WORKER")
    if not worker:
        return {}
    run_uid = env.get("PYTEST_XDIST_TESTRUNUID", "")[:8]
    index = derive_redis_db_index(worker, env.get("PYTEST_REDIS_DB_BASE", DEFAULT_REDIS_DB_BASE))
    return {
        "POSTGRES_DB": derive_test_db_name(run_uid, worker),
        BROKER_URL_ENV: replace_redis_db_index(env.get(BROKER_URL_ENV, DEFAULT_REDIS_URL), index),
    }


def db_guard_error(db_name: str) -> str | None:
    """`drop_all` 대상이 테스트 DB가 아니면 사유 문자열, 안전하면 None.

    conftest는 세션 시작 시 전 테이블 `drop_all`을 수행하므로, 이름이
    `biznotes_test`로 시작하지 않는 DB(개발 DB `biznotes` 등)를 가리키고 있으면
    스키마를 파괴한다. 그런 실행은 테이블을 건드리기 전에 중단한다.
    """
    if not db_name.startswith(TEST_DB_PREFIX):
        return (
            f"POSTGRES_DB={db_name!r}는 테스트 DB가 아닙니다 "
            f"({TEST_DB_PREFIX!r} 접두 필요) — drop_all 전에 중단합니다"
        )
    return None


def run_once_per_run(lock_path: Path, run_key: str, action: Callable[[], None]) -> bool:
    """`action`을 run당 정확히 1회 실행 (xdist 워커·스레드 간 `fcntl.flock` 직렬화).

    잠금 파일의 내용이 곧 "이 run은 이미 실행했다"는 표식이다 (별도 마커 파일 없음).
    실행했으면 True, 이미 실행된 run이라 건너뛰었으면 False.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            if handle.read().strip() == run_key:
                return False
            action()
            handle.seek(0)
            handle.truncate()
            handle.write(run_key)
            handle.flush()
            return True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def ensure_once_per_run(lock_path: Path, run_uid: str | None, action: Callable[[], None]) -> bool:
    """run당 1회 `action` 실행 — xdist면 `run_uid` 잠금으로 직렬화, 비-xdist면 즉시 실행.

    비-xdist에서 잠금을 쓰지 않는 이유: 워커가 없으면 프로세스가 하나뿐이라 경합
    자체가 없고, 잠금 파일이 놓이는 `getbasetemp().parent`는 비-xdist에서 run 간
    **영속하는** `pytest-of-<user>/`다. 그 경로에 `pid-<pid>`를 run 키로 남기면 OS가
    PID를 재사용한 이후의 실행이 "이미 실행한 run"으로 오인돼 `action`을 조용히
    영구 skip한다 — D-4가 마커 파일 방식을 버린 것과 같은 실패 모드다.
    """
    if not run_uid:
        action()
        return True
    return run_once_per_run(lock_path, run_uid, action)


# app 모듈 import 전에 환경 고정 (get_settings가 lru_cache이므로)
os.environ.setdefault("POSTGRES_PORT", "25433")
os.environ.setdefault("POSTGRES_DB", SHARED_TEST_DB)
os.environ.setdefault(BROKER_URL_ENV, DEFAULT_REDIS_URL)
# xdist 워커는 setdefault로 덮을 수 없다 (controller가 값을 물려주므로) — 명시 대입.
os.environ.update(worker_env_overrides(dict(os.environ)))
os.environ.setdefault("JWT_SECRET", "test-secret")
# 로컬 스토리지 백엔드가 저장소 안(`apps/api/data`)에 파일을 만들지 않게 실행마다 임시 경로를 준다
os.environ.setdefault("BIZNOTES_DATA_DIR", tempfile.mkdtemp(prefix="api-test-data-"))
os.environ.setdefault("APP_ENV", "test")  # 레이트 리밋 비활성화

import uuid  # noqa: E402

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from biznotes_core.core.security import hash_password  # noqa: E402
from biznotes_core.models import Base, Group, NotebookShare, User, UserGroup  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

settings = get_settings()

#: 코어가 메타데이터에서 뺀 옛 테이블(v0.14.0 #168 `ingestion_jobs`). `drop_all`은 메타데이터에
#: 없는 테이블을 모르므로, 공유 `biznotes_test`(직렬 실행)에 남은 옛 테이블의 FK가 부모
#: (`sources`) 삭제를 막는다. xdist 워커 DB는 run마다 새로 만들어 해당이 없다(HCI-13).
RETIRED_TABLES = ("ingestion_jobs",)


async def drop_retired_tables(conn: AsyncConnection, db_name: str) -> None:
    """`RETIRED_TABLES`를 `DROP TABLE IF EXISTS … CASCADE` — 테스트 DB가 아니면 거부한다."""
    guard = db_guard_error(db_name)
    if guard is not None:
        raise RuntimeError(guard)
    for table in RETIRED_TABLES:
        await conn.execute(text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))


async def _connect_maintenance_db() -> asyncpg.Connection:
    """CREATE/DROP DATABASE를 실행할 수 있는 유지보수 DB(`postgres`) 커넥션."""
    return await asyncpg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=settings.postgres_password,
        database="postgres",
    )


async def _ensure_database() -> None:
    conn = await _connect_maintenance_db()
    try:
        await conn.execute(f'CREATE DATABASE "{settings.postgres_db}"')
    except asyncpg.DuplicateDatabaseError:
        pass
    finally:
        await conn.close()


async def _drop_worker_database() -> None:
    """xdist 워커가 만든 run-uid DB를 삭제한다. 공용 `biznotes_test`는 유지.

    AC-18 (c): 실행 후 `datname LIKE 'biznotes_test_%'`가 0행이어야 한다.
    """
    name = settings.postgres_db
    if name == SHARED_TEST_DB or not name.startswith(f"{TEST_DB_PREFIX}_"):
        return
    conn = await _connect_maintenance_db()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
async def _setup_db():
    # BL-01 destructive guard: drop_all 진입 전에 대상이 테스트 DB인지 확인한다.
    guard = db_guard_error(settings.postgres_db)
    if guard is not None:
        pytest.exit(guard, returncode=1)

    try:
        await _ensure_database()
    except OSError:
        # R0-2: skip → fail. 조용한 전체 skip은 "테스트 0개 통과"를 green으로 위장한다.
        # 통합 테스트는 PG가 전제 조건 — 없으면 즉시 비정상 종료(make up 후 재실행).
        pytest.exit(
            "PostgreSQL not reachable — 통합 테스트 전제 미충족 (make up 후 재실행)", returncode=1
        )

    from biznotes_core.core.db import engine

    async with engine.begin() as conn:
        # 청크 벡터 열(source_chunks.embedding·sparse)의 타입 — 마이그레이션 0001과 같다
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await drop_retired_tables(conn, settings.postgres_db)
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()
    await _drop_worker_database()


# R0-2: 테이블명은 모델(진실 공급원)에서 산출 — 신규 테이블 자동 포함.
# FK 역순 DELETE — `sorted_tables`는 의존성 오름차순이므로 역순이 자식→부모 삭제 순서.
# (TRUNCATE ... CASCADE 대비 실측 ~5배 빠름: 빈 테이블에서는 TRUNCATE의 파일 절단·
#  ACCESS EXCLUSIVE 락·identity 재시작 오버헤드가 지배적 — 73ms/test vs DELETE 15ms/test,
#  505 테스트 기준 스위트 +37s vs +7.5s. design/r0-2-test-isolation.md §3 실측 반영.)
_DELETE_ORDER = tuple(t.name for t in reversed(Base.metadata.sorted_tables))


@pytest.fixture(autouse=True)
async def _db_isolation():
    """각 테스트 종료 후 전 테이블 DELETE — 테스트 간 데이터 격리 (R0-2, 대안 C).

    설계 근거: `design/r0-2-test-isolation.md` §2 (SAVEPOINT/팩토리패치 탈락, 테이블 초기화 채택).
    앱 코드는 일절 건드리지 않으므로 실제 commit·다중 커넥션·워커 별도 이벤트 루프·
    병렬 노드 세션이 전부 그대로 동작한다. `pytest-randomly` 셔플에서도 매 테스트가
    빈 DB에서 시작해 순서 의존을 원천 제거한다.

    DELETE는 identity 시퀀스를 리셋하지 않으나(ID가 테스트 간 증가), 테스트는 UUID·
    반환 ID로 조회하지 하드코딩 id=1에 의존하지 않으므로 무해하며 오히려 더 현실적이다.
    """
    yield
    from biznotes_core.core.db import engine

    async with engine.begin() as conn:
        for table in _DELETE_ORDER:
            await conn.execute(text(f'DELETE FROM "{table}"'))


@pytest.fixture
async def db():
    from biznotes_core.core.db import async_session_factory

    async with async_session_factory() as session:
        yield session


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


_PASSWORD = "pw-test-1234"
# bcrypt는 느리므로 해시 1회 재사용
_PASSWORD_HASH = hash_password(_PASSWORD)


@pytest.fixture
def test_password() -> str:
    return _PASSWORD


async def _create_user(db, prefix: str, role: str = "user") -> User:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@test.dev"
    user = User(email=email, password_hash=_PASSWORD_HASH, name=prefix, role=role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def share_group(db, *members: User) -> Group:
    """`members`가 구성원인 그룹을 새로 만든다(커밋). API로 그룹 공유할 때 쓴다 — 코어는 노트북 실제
    소유자가 속한 그룹만 공유 대상으로 받으므로(#278 1단계) 소유자도 `members`에 넣는다."""
    group = Group(name=f"share-{uuid.uuid4().hex[:12]}")
    db.add(group)
    await db.flush()
    db.add_all(UserGroup(user_id=member.id, group_id=group.id) for member in members)
    await db.commit()
    return group


async def share_with(db, notebook_id: uuid.UUID, user: User, permission: str) -> NotebookShare:
    """`user`에게 노트북 권한을 그룹 공유 행으로 준다(flush만, 커밋은 호출측).

    코어 v0.28.0부터 공유 대상은 그룹뿐이다(#278 3단계). 사용자마다 전용 그룹(`share-<user id>`)을
    하나 두고 그 그룹에 공유한다. 같은 사용자·노트북으로 다시 부르면 그 행의 권한만 바꾼다 — 옛
    사용자 대상 공유 한 행과 같은 모양이다. 해제는 공유 행을 지우면 된다(그룹 소속은 남는다).
    """
    name = f"share-{user.id}"
    group = await db.scalar(select(Group).where(Group.name == name))
    if group is None:
        group = Group(name=name)
        db.add(group)
        await db.flush()
        db.add(UserGroup(user_id=user.id, group_id=group.id))
    share = await db.scalar(
        select(NotebookShare).where(
            NotebookShare.notebook_id == notebook_id, NotebookShare.group_id == group.id
        )
    )
    if share is None:
        share = NotebookShare(notebook_id=notebook_id, group_id=group.id, permission=permission)
        db.add(share)
    else:
        share.permission = permission
    await db.flush()
    return share


@pytest.fixture
async def user_a(db) -> User:
    return await _create_user(db, "user-a")


@pytest.fixture
async def user_b(db) -> User:
    return await _create_user(db, "user-b")


@pytest.fixture
async def admin_user(db) -> User:
    return await _create_user(db, "admin", role="admin")


@pytest.fixture
async def mock_llm(db):
    """기본(is_default) mock LLM 프로바이더+모델 — 8개 테스트 파일 중복 정의를 공용화 (R0-2).

    반환값 없음: "is_default mock 모델이 존재한다"는 부수효과만 제공한다.
    프로바이더 type="mock"이 게이트웨이의 mock 응답 경로를 결정하며 model_name은 임의값.
    """
    from biznotes_core.models import LLMModel, LLMProvider

    provider = LLMProvider(name=f"mock-{uuid.uuid4().hex[:6]}", type="mock", enabled=True)
    db.add(provider)
    await db.flush()
    db.add(
        LLMModel(
            provider_id=provider.id,
            model_name="mock-rag",
            display_name="Mock",
            capabilities=["llm"],
            is_default=True,
            enabled=True,
        )
    )
    await db.commit()


@pytest.fixture(autouse=True)
def deleted_objects(monkeypatch) -> list[tuple[str, list[str]]]:
    """코어 v0.12.0 삭제 경로의 커밋 뒤 오브젝트 정리(`storage.delete_objects`)를 막는다.

    호출은 `(bucket, keys)`로 기록한다.

    막지 않으면 노트북·소스·사용자 삭제 테스트가 실제 스토리지에 닿는다
    (core-v0.12.0-upgrade AC-1.5).
    """
    calls: list[tuple[str, list[str]]] = []

    def delete_objects(bucket, keys):
        calls.append((bucket, list(keys)))
        return []

    monkeypatch.setattr("biznotes_core.services.storage.delete_objects", delete_objects)
    return calls


@pytest.fixture
def mem_storage(monkeypatch):
    """skills package.py가 쓰는 storage 모듈 함수를 인메모리 dict로 대체."""
    store: dict[tuple[str, str], bytes] = {}

    def put_object(bucket, key, data, content_type="application/octet-stream"):
        store[(bucket, key)] = data

    def get_object(bucket, key):
        if (bucket, key) not in store:
            raise FileNotFoundError(key)
        return store[(bucket, key)]

    def list_objects(bucket, prefix):
        return [k for (b, k) in store if b == bucket and k.startswith(prefix)]

    def copy_object(bucket, src, dst):
        store[(bucket, dst)] = store[(bucket, src)]

    def delete_prefix(bucket, prefix):
        for k in [k for (b, k) in list(store) if b == bucket and k.startswith(prefix)]:
            store.pop((bucket, k), None)

    def delete_object(bucket, key):
        store.pop((bucket, key), None)

    def delete_objects(bucket, keys):
        for key in keys:
            store.pop((bucket, key), None)
        return []

    import biznotes_core.services.storage as st

    monkeypatch.setattr(st, "ensure_buckets", lambda: None)
    monkeypatch.setattr(st, "put_object", put_object)
    monkeypatch.setattr(st, "get_object", get_object)
    monkeypatch.setattr(st, "list_objects", list_objects)
    monkeypatch.setattr(st, "copy_object", copy_object)
    monkeypatch.setattr(st, "delete_prefix", delete_prefix)
    monkeypatch.setattr(st, "delete_object", delete_object)
    monkeypatch.setattr(st, "delete_objects", delete_objects)
    return store


@pytest.fixture
def _mock_runtime_deps(monkeypatch):
    """에이전트 런타임 외부 의존(RAG 검색·아티팩트 저장) 모킹 — 중복 정의 공용화 (R0-2).

    retrieve는 "결과 없음"을 반환한다. citation이 필요한 테스트(test_agents.py)는
    같은 이름의 로컬 픽스처로 오버라이드한다. 반환값은 put_object로 저장된
    아티팩트 dict(key → bytes).
    """
    artifacts: dict[str, bytes] = {}

    async def fake_retrieve(db, notebook_id, query, source_ids, top_k=6):
        from biznotes_core.services.rag_chat import RetrievalResult

        return RetrievalResult(citations=[], insufficient_context=True)

    monkeypatch.setattr("biznotes_core.services.rag_chat.retrieve_context", fake_retrieve)

    def fake_put(bucket, key, data, content_type="text/plain"):
        artifacts[key] = data

    monkeypatch.setattr("biznotes_core.services.storage.ensure_buckets", lambda: None)
    monkeypatch.setattr("biznotes_core.services.storage.put_object", fake_put)
    return artifacts


async def login(client: httpx.AsyncClient, email: str, password: str = _PASSWORD) -> str:
    """로그인 후 access 토큰 반환 (Bearer 헤더용).

    **쿠키 위생(리뷰 P1-05)**: 코어 `biznotes_core/api/deps.py::_extract_token`은 httpOnly
    쿠키를 Bearer 헤더보다 **우선**한다. 로그인 응답의 Set-Cookie가 클라이언트 쿠키 jar에
    누적되면, 이후 다른 사용자의 Bearer 헤더로 보낸 요청이 실제로는 **마지막 로그인
    사용자**로 인증되어 권한 테스트가 잘못된 주체로 조용히 통과한다(false green).
    이 헬퍼는 Bearer 용도이므로 반환 전에 jar를 비워 그 함정을 구조적으로 제거한다.
    쿠키 인증 자체를 검증하는 테스트는 이 헬퍼를 쓰지 않고 `/auth/login`을 직접 호출한다.
    """
    res = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    client.cookies.clear()
    return res.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def set_source_limit(monkeypatch, value: int) -> None:
    """노트북당 소스 상한(env `MAX_SOURCES_PER_NOTEBOOK`, 코어 v0.30.0 #445)을 이 테스트에서만
    바꾼다.

    코어 `enqueue.MAX_SOURCES_PER_NOTEBOOK` monkeypatch는 폐기 예정(v0.31.0 제거)이라 설정 값을
    바꾼다. 관리자 재정의가 없을 때 이 값이 상한이다. 코어 설정 검증은 1 이상만 받지만 테스트는
    0도 넣어 「첫 소스부터 거부」를 만든다(setattr은 검증을 거치지 않는다).
    """
    from biznotes_core.config import get_settings as core_settings

    monkeypatch.setattr(core_settings(), "max_sources_per_notebook", value)


# ---------------------------------------------------------------------------
# BL-35 RSS 계측 훅 — `PYTEST_RSS_REPORT=1`일 때만 활성 (기본 비활성).
#
# 계측 장치가 계측 대상을 바꾸지 않도록 opt-in으로 둔다. 비활성이면 훅 본문이
# 첫 줄에서 즉시 반환하고 `resource.getrusage` 호출조차 하지 않는다.
# `ru_maxrss`는 프로세스 생애의 단조 최대치이므로 모듈별 증가분은 **후보 선별용**이며
# 모듈에 귀속된 순수 사용량이 아니다 (AC-20 (b)).
# ---------------------------------------------------------------------------

RSS_REPORT_ENABLED = os.environ.get("PYTEST_RSS_REPORT") == "1"
#: macOS `ru_maxrss`는 bytes, Linux는 KiB (AC-20 단위 정규화 근거).
RSS_UNIT = "bytes" if sys.platform == "darwin" else "KiB"

_rss_module_delta: dict[str, int] = {}
_rss_current_module: list[str | None] = [None]
_rss_module_base: list[int] = [0]


def _ru_maxrss() -> int:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def _rss_close_module() -> None:
    module = _rss_current_module[0]
    if module is None:
        return
    delta = _ru_maxrss() - _rss_module_base[0]
    _rss_module_delta[module] = _rss_module_delta.get(module, 0) + max(0, delta)
    _rss_current_module[0] = None


def pytest_runtest_logstart(nodeid: str, location: object) -> None:
    if not RSS_REPORT_ENABLED:
        return
    module = nodeid.split("::", 1)[0]
    if module != _rss_current_module[0]:
        _rss_close_module()
        _rss_current_module[0] = module
        _rss_module_base[0] = _ru_maxrss()


def pytest_sessionfinish(session: object, exitstatus: object) -> None:
    if not RSS_REPORT_ENABLED:
        return
    _rss_close_module()
    peak = _ru_maxrss()
    worker = os.environ.get("PYTEST_XDIST_WORKER") or "main"
    top = sorted(_rss_module_delta.items(), key=lambda kv: kv[1], reverse=True)[:10]
    print(f"\n[rss] worker={worker} peak_ru_maxrss={peak} {RSS_UNIT}")
    for module, delta in top:
        print(f"[rss] +{delta} {RSS_UNIT} {module}")

    report_dir = os.environ.get("PYTEST_RSS_REPORT_DIR")
    if not report_dir:
        return
    # 디렉터리 정리는 하지 않는다 — 호출자가 fresh `mktemp -d`를 주고
    # 파일 수·`worker`·`testrunuid`를 단언한다 (AC-20 (c), PR-14 잔여).
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{worker}.json").write_text(
        json.dumps(
            {
                "worker": worker,
                "testrunuid": os.environ.get("PYTEST_XDIST_TESTRUNUID", ""),
                "ru_maxrss": peak,
                "unit": RSS_UNIT,
            }
        ),
        encoding="utf-8",
    )
