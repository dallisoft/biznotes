"""GitHub 저장소 URL 스킬 설치 스키마(#259, docs/plans/skill-install-github.md §4).

- `SkillInstallOfferOut`: 채팅 SSE `skill_install_offer` 이벤트의 `offer`. HTTP 응답이 아니라서
  `app.core.openapi_sse`가 OpenAPI 컴포넌트로 내보낸다(웹은 생성 타입을 쓴다).
- `SkillInstallGithubRequest`·`SkillInstallGithubOut`: 카드 버튼이 부르는
  `POST /skill-installs/github`.

출력 모델의 nullable 필드는 기본값을 두지 않는다 — 생성 타입이 `x?:`가 아니라 `x: T | null`이 된다.
"""

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# 사용자 URL·요청 값의 허용 모양(설계 노트 §4). `/`가 든 브랜치명은 받지 않는다.
OWNER_PATTERN = r"^[A-Za-z0-9-]{1,39}$"
REPO_PATTERN = r"^[A-Za-z0-9._-]{1,100}$"
SHA_PATTERN = r"^[0-9a-f]{40}$"
SHA256_PATTERN = r"^[0-9a-f]{64}$"
# codeload archive의 최상위 폴더(`<repo>-<sha>`) — archive 안 경로의 접두사일 뿐이다
ARCHIVE_ROOT_PATTERN = r"^[A-Za-z0-9._-]{1,200}$"
# 설치 1회의 경로 상한(§4 「한도」)
MAX_INSTALL_PATHS = 10
# 저장소 안 스킬 폴더 경로 상한 — `source_ref`(512B) 안에 owner·repo·SHA와 함께 들어간다
MAX_SKILL_PATH_CHARS = 300
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]{1,255}")


def is_skill_dir_path(path: str) -> bool:
    """저장소 안 스킬 폴더 경로 — 빈 문자열(저장소 루트) 또는 `a/b/c`. `.`·`..` 조각은 없다."""
    if path == "":
        return True
    if len(path) > MAX_SKILL_PATH_CHARS:
        return False
    return all(
        _SEGMENT_RE.fullmatch(segment) and segment not in (".", "..")
        for segment in path.split("/")
    )


def _not_dot(value: str) -> str:
    if value in (".", ".."):
        raise ValueError("'.'·'..'은 쓸 수 없습니다")
    return value


class SkillInstallWarningOut(BaseModel):
    """설치 전 경고 한 건 — 코어 보고서 층(standard·portability·policy·capability) 또는 제품."""

    layer: Literal["standard", "portability", "policy", "capability", "product"]
    code: str
    message: str


class SkillInstallReinstallOut(BaseModel):
    """같은 출처(저장소 + 경로)로 이미 설치한 요청자 본인의 스킬 — 설치하면 새 version이 쌓인다."""

    skill_id: uuid.UUID
    name: str
    latest_version: int | None


class SkillInstallCandidateOut(BaseModel):
    path: str = Field(description="저장소 안 스킬 폴더 경로. 저장소 루트의 SKILL.md는 빈 문자열")
    name: str | None
    description: str | None
    file_count: int
    ok: bool = Field(description="사전 점검 통과 — 거짓이면 설치할 수 없다(`errors`에 사유 코드)")
    errors: list[str]
    warnings: list[SkillInstallWarningOut]
    runtime_profile: str | None
    has_scripts: bool
    copy_of: str | None = Field(
        description="같은 name의 대표 후보 경로. 값이 있으면 사본이라 카드에서 접고 체크하지 않는다"
    )
    reinstall: SkillInstallReinstallOut | None


class SkillInstallLicenseOut(BaseModel):
    spdx_id: str | None
    name: str | None


class SkillInstallOfferOut(BaseModel):
    """확인 카드 — 저장하지 않는다(새로고침하면 사라진다). 설치 요청은 이 값을 그대로 돌려보낸다."""

    owner: str
    repo: str
    sha: str
    ref: str
    archive_sha256: str
    archive_root: str
    subpath: str | None = Field(
        description="URL의 `/tree/<ref>/<path>` 경로. 이 아래 후보만 보인다"
    )
    license: SkillInstallLicenseOut | None = Field(description="저장소 라이선스. 없으면 null")
    candidates: list[SkillInstallCandidateOut]
    truncated: int
    max_paths: int


class SkillInstallOfferEvent(BaseModel):
    """채팅 SSE 이벤트 `skill_install_offer`(새 이벤트, 추가형)."""

    type: Literal["skill_install_offer"]
    offer: SkillInstallOfferOut


class SkillInstallGithubRequest(BaseModel):
    owner: str = Field(pattern=OWNER_PATTERN)
    repo: str = Field(pattern=REPO_PATTERN)
    sha: str = Field(pattern=SHA_PATTERN)
    archive_sha256: str = Field(pattern=SHA256_PATTERN)
    archive_root: str = Field(pattern=ARCHIVE_ROOT_PATTERN)
    paths: list[str] = Field(min_length=1, max_length=MAX_INSTALL_PATHS)

    @field_validator("repo", "archive_root")
    @classmethod
    def _no_dot_segment(cls, value: str) -> str:
        return _not_dot(value)

    @field_validator("paths")
    @classmethod
    def _valid_paths(cls, paths: list[str]) -> list[str]:
        if any(not is_skill_dir_path(path) for path in paths):
            raise ValueError("경로는 저장소 안 스킬 폴더 경로여야 합니다")
        # 같은 경로를 두 번 설치하지 않는다(순서는 지킨다)
        return list(dict.fromkeys(paths))


class SkillInstallItemOut(BaseModel):
    path: str
    status: Literal["installed", "reinstalled", "failed"]
    skill_id: uuid.UUID | None
    name: str | None
    version: int | None
    has_scripts: bool
    reason: str | None = Field(description="실패 사유 코드(코어 검증 사유 그대로)")
    message: str | None = Field(description="실패 고정 문구")


class SkillInstallGithubOut(BaseModel):
    owner: str
    repo: str
    sha: str
    items: list[SkillInstallItemOut]
