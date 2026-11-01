"""services.authz 순수 함수 단위 테스트 (R1-5).

DB 없이 권한 판정 규칙과 "실제 소유자 vs admin 승격" 분리를 고정한다.
"""

import uuid

from biznotes_core.services.authz import is_actual_owner, resolve_permission

OWNER = uuid.uuid4()
OTHER = uuid.uuid4()
ADMIN = uuid.uuid4()


def _resolve(user_id, user_role="user", share_perms=None, owner_id=OWNER, visibility="private"):
    return resolve_permission(
        owner_id=owner_id,
        visibility=visibility,
        user_id=user_id,
        user_role=user_role,
        share_perms=share_perms or set(),
    )


def test_actual_owner_gets_owner():
    assert _resolve(OWNER) == "owner"
    assert is_actual_owner(OWNER, OWNER) is True


def test_admin_escalated_to_owner_but_not_actual_owner():
    # admin은 owner 권한으로 승격되지만 "실제 소유자"는 아니다 (phase-22c HCI-2).
    assert _resolve(ADMIN, user_role="admin") == "owner"
    assert is_actual_owner(OWNER, ADMIN) is False


def test_share_write_and_read():
    assert _resolve(OTHER, share_perms={"write"}) == "write"
    assert _resolve(OTHER, share_perms={"read"}) == "read"
    # write가 read보다 우선.
    assert _resolve(OTHER, share_perms={"read", "write"}) == "write"


def test_public_visibility_grants_read():
    assert _resolve(OTHER, visibility="public") == "read"


def test_no_access_returns_none():
    assert _resolve(OTHER) is None
    assert _resolve(OTHER, visibility="private", share_perms=set()) is None


def test_owner_precedes_admin_and_share():
    # 실제 소유자면 role/share와 무관하게 owner.
    assert _resolve(OWNER, user_role="admin", share_perms={"read"}) == "owner"
