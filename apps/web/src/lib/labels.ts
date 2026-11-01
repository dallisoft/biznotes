/**
 * 값 라벨 사전 — 원시값(역할·권한·상태 코드 등)을 화면 라벨로 바꾸는 단일 출처(plan §6.3.2, G-3).
 *
 * 규칙
 * - 알려진 값은 사전의 한국어 라벨로 보인다.
 * - **미지 값은 원시값을 그대로 보인다**(빈 칸·`undefined` 렌더 금지, 콘솔 경고 없음). 서버가 새 값을
 *   먼저 내보내도 화면이 깨지지 않고, 사용자는 적어도 원시값으로 추적할 수 있다.
 * - 값 자체가 없으면(`null`·`undefined`·빈 문자열) 「—」.
 * - 이미 있던 사전(`KIND_LABEL`·`RUN_STATUS_LABEL`)은 옮기지 않고 재수출한다.
 *   호출부를 이 모듈로 옮기는 일은 각 화면의 phase(P3~P5)에서 한다.
 */
import type { StatusTone } from "@/components/ui/status-badge";
import type { Modality } from "@/lib/admin-types";
import { KIND_LABEL, RUN_STATUS_LABEL } from "@/lib/agent-types";
import { EMPTY } from "@/lib/format";
import type { SharePermission } from "@/lib/notebook-bulk";
import type { Notebook, NotebookShare } from "@/lib/types";

export { KIND_LABEL, RUN_STATUS_LABEL };

type Dict = Readonly<Record<string, string>>;

/**
 * 사전 조회 — 알려진 값은 라벨, 미지 값은 원시값, 값이 없으면 「—」.
 * 자체 속성만 본다(`"constructor"`·`"__proto__"` 같은 값이 프로토타입 멤버로 새지 않게).
 */
export function labelFor(dict: Dict, value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return EMPTY;
  return Object.prototype.hasOwnProperty.call(dict, value) ? dict[value] : value;
}

// ---- 역할·권한 ----

/** 사용자 역할 */
export const ROLE_LABEL = {
  admin: "관리자",
  user: "사용자",
} as const satisfies Dict;

export function roleLabel(role: string | null | undefined): string {
  return labelFor(ROLE_LABEL, role);
}

/**
 * 노트북 권한. 공유 다이얼로그(단건·일괄)도 이 문구를 쓴다 — 괄호 영문(「읽기 전용(read)」) 판은
 * core-v0.20.0 P3 뒤 제거했다(#151).
 */
export const NOTEBOOK_PERMISSION_LABEL = {
  owner: "소유자",
  write: "편집 가능",
  read: "읽기 전용",
} as const satisfies Record<Notebook["permission"], string>;

export function notebookPermissionLabel(permission: string | null | undefined): string {
  return labelFor(NOTEBOOK_PERMISSION_LABEL, permission);
}

/**
 * 공유 권한 선택지 — 단건 공유 관리·일괄 공유 대화상자의 선택 control(Base UI Select `items`)이 같은 값·이름을 쓴다.
 * 이름은 `NOTEBOOK_PERMISSION_LABEL`의 「읽기 전용」·「편집 가능」(괄호 영문 없음 — core-v0.20.0 P3 UI 계약, §6.3.2).
 */
export const SHARE_PERMISSION_OPTIONS: readonly { value: SharePermission; label: string }[] = [
  { value: "read", label: NOTEBOOK_PERMISSION_LABEL.read },
  { value: "write", label: NOTEBOOK_PERMISSION_LABEL.write },
];

// ---- 공유 대상(core-v0.20.0 §5.8 ② — 대상 구별) ----

/**
 * 공유 목록 행의 대상 종류(UI 계약 `data-share-kind` 값 집합 — 늘리지 않는다). `group` = 그룹 행 중 소유자가 속한 그룹이거나
 * 소속을 아직 모르는 그룹(`/me/groups` 조회 실패), `foreign-group` = 소유자가 속하지 않은 그룹(다시 공유하면 404라 해제만).
 * 공유는 그룹 대상뿐이다(코어 v0.28.0 #278 3단계 — 옛 개별 사용자 행은 리비전이 지웠다). 권한을 바꿀 수 있는지는 종류가 아니라
 * `editable`이 정한다.
 */
export type ShareTargetKind = "group" | "foreign-group";

/** 이름·이메일을 주는 조회 계약이 없으므로(§5.8) 대상 id 앞 8자로 같은 종류의 행을 서로 구별한다. */
export const SHARE_TARGET_ID_PREFIX = 8;
export const FOREIGN_GROUP_LABEL = "소유자가 속하지 않은 그룹";
/** 그룹 이름을 아직 모를 때(`/me/groups` 조회 실패) — 비구성원이라고 단정하지 않는다. */
export const UNKNOWN_GROUP_LABEL = "그룹";

/**
 * 공유 행의 대상 종류·표기와 권한 변경 가능 여부. `myGroupNames`는 `/me/groups`의 id → 이름이고, 그 조회가 실패해 모르면 `null`이다.
 * - 소속이 확인된 그룹(이름이 있다)만 `editable` — 같은 `POST`로 권한을 바꿀 수 있다.
 * - 소속 미확인(`null`)이면 그룹 행은 중립 표기 「그룹 · <id 앞 8자>」·`group`이고 **`editable`이 아니다**(PR #154 P3-04): 비소속 그룹 행이
 *   조회 실패 동안 권한 변경 대상이 되지 않게 한다 — 소속이 확인되기 전에는 해제만(계획 §5.8 「비소속 그룹 행은 해제만」).
 * - 조회는 됐는데 소속이 아니면 `foreign-group` — 해제만.
 */
export function describeShareTarget(
  row: Pick<NotebookShare, "group_id">,
  myGroupNames: ReadonlyMap<string, string> | null,
): { kind: ShareTargetKind; label: string; editable: boolean } {
  const short = row.group_id.slice(0, SHARE_TARGET_ID_PREFIX);
  if (myGroupNames === null) {
    return { kind: "group", label: `${UNKNOWN_GROUP_LABEL} · ${short}`, editable: false };
  }
  const name = myGroupNames.get(row.group_id);
  if (name !== undefined) return { kind: "group", label: name, editable: true };
  return { kind: "foreign-group", label: `${FOREIGN_GROUP_LABEL} · ${short}`, editable: false };
}

/**
 * 실제 소유자가 아닌데 목록 권한이 owner인 행 — 코어 v0.18.0+ 목록은 관리자에게 남의 노트북도 owner로 준다(admin 승격,
 * core-v0.20.0 §5.4·D-6). 그런 행이 「소유자」로 보이지 않게 쓴다.
 */
export const ADMIN_PERMISSION_LABEL = "관리자 권한";

/** 노트북 목록 행의 권한 라벨 — 승격된 owner(`!is_owner`)는 「관리자 권한」, 그 밖은 `notebookPermissionLabel`. */
export function notebookRowPermissionLabel(nb: { is_owner: boolean; permission: string }): string {
  return !nb.is_owner && nb.permission === "owner" ? ADMIN_PERMISSION_LABEL : notebookPermissionLabel(nb.permission);
}

/**
 * 남의 노트북을 볼 수 있는 까닭 — 공유 기록 없이 모든 사용자에게 공개된 노트북(예: 샘플)은 「공개」, 그 밖은 「공유받음」.
 * 편집 권한은 공유로만 생기므로 `write`면 공개 노트북이라도 「공유받음」이다.
 */
export function notebookAccessLabel(nb: { visibility: string; permission: string }): string {
  return nb.visibility === "public" && nb.permission !== "write" ? "공개" : "공유받음";
}

// ---- 상태 ----

/** 워크플로우 상태 — `enabled` 플래그 */
export function workflowStatusLabel(enabled: boolean): string {
  return enabled ? "게시됨" : "초안";
}

/**
 * 스킬 표시 상태. 키는 화면이 판정한 상태다(초안 / 공개 version 있음 / 검토 요청 대기 / 공개 철회(보류)).
 * 판정 규칙은 스킬 화면 phase(P4·P5)가 정한다.
 */
export const SKILL_STATUS_LABEL = {
  draft: "초안",
  published: "공개 중",
  review_pending: "검토 대기",
  withdrawn: "공개 철회됨",
} as const satisfies Dict;

export type SkillDisplayStatus = keyof typeof SKILL_STATUS_LABEL;

export function skillStatusLabel(status: string | null | undefined): string {
  return labelFor(SKILL_STATUS_LABEL, status);
}

/**
 * 스킬 표시 상태의 배지 톤 — 같은 상태는 어느 화면에서나 같은 색이다(§6.5 StatusBadge, 진단 P-07 ③).
 * 사용자 스킬 목록·상세와 관리 스킬 공개 화면이 이 사전 하나를 쓴다(P6 — 사용자 화면 「공개 중」이 info였던 불일치 해소).
 */
export const SKILL_STATUS_TONE = {
  draft: "neutral",
  published: "success",
  review_pending: "info",
  withdrawn: "warning",
} as const satisfies Readonly<Record<SkillDisplayStatus, StatusTone>>;

/**
 * 스킬 공개 범위(`visibility`) — `system`은 「기본 제공」(§6.3.1, 「빌트인」 대체). 사용자 스킬 화면(P4)과 관리 스킬
 * 화면(P5 — `lib/skill-types`의 `VISIBILITY_LABEL`은 이 사전의 별칭)이 함께 쓴다.
 */
export const SKILL_VISIBILITY_LABEL = {
  private: "비공개",
  org: "조직 공유",
  system: "기본 제공",
} as const satisfies Dict;

export function skillVisibilityLabel(visibility: string | null | undefined): string {
  return labelFor(SKILL_VISIBILITY_LABEL, visibility);
}

/** 활성 여부 — `enabled`·`is_active` */
export function activeLabel(active: boolean): string {
  return active ? "활성" : "비활성";
}

/** 헬스 — `true` 정상, `false` 오류, `null`(아직 확인 안 함) 미확인 */
export function healthLabel(healthy: boolean | null | undefined): string {
  if (healthy === true) return "정상";
  if (healthy === false) return "오류";
  return "미확인";
}

/** 에이전트 실행 상태 — 기존 `RUN_STATUS_LABEL` + 미지 값 폴백 */
export function runStatusLabel(status: string | null | undefined): string {
  return labelFor(RUN_STATUS_LABEL, status);
}

/** 에이전트 유형 — 기존 `KIND_LABEL` + 미지 값 폴백 */
export function kindLabel(kind: string | null | undefined): string {
  return labelFor(KIND_LABEL, kind);
}

// 프로바이더 유형(`anthropic`·`google`·`mock` 등)은 식별자이므로 번역하지 않는다 — 사전 없음(§6.3.2).

/**
 * 에이전트 범위 — 코어 enum `system`·`user`를 번역하고, 그 밖(미지 값)은 원시값.
 * `user`는 P5 통합에서 추가(ops E-1, Orchestrator 승인 2026-10-15 — 정식 enum 값이 영문으로 보이던 문제).
 */
export const AGENT_SCOPE_LABEL = {
  system: "시스템",
  user: "사용자",
} as const satisfies Dict;

export function agentScopeLabel(scope: string | null | undefined): string {
  return labelFor(AGENT_SCOPE_LABEL, scope);
}

/**
 * 모델 능력(modality) — 키 순서는 `MODALITIES`(코어 정본)와 같다. 사전에 없는 능력은 원시값.
 * `image`는 코어 v0.23.2(#347)부터 모델 가져오기 목록에도 나온다(#206).
 */
export const CAPABILITY_LABEL = {
  llm: "텍스트 생성(LLM)",
  vlm: "이미지 이해(VLM)",
  embedding: "임베딩",
  reranker: "리랭커",
  image: "이미지 생성",
} as const satisfies Record<Modality, string>;

export function capabilityLabel(capability: string | null | undefined): string {
  return labelFor(CAPABILITY_LABEL, capability);
}

/**
 * 감사 로그 액션 코드 → 라벨. 전체 코드 목록은 P5에서 감사 로그·코어 상수에서 수집해 채운다.
 * 미지 코드는 원시 코드를 그대로 돌려준다(화면은 라벨 아래 원시 코드를 함께 보여 추적성을 지킨다 — P5).
 */
export const AUDIT_ACTION_LABEL = {
  "llm.provider_create": "LLM 프로바이더 생성",
  "llm.provider_update": "LLM 프로바이더 수정",
  "llm.provider_delete": "LLM 프로바이더 삭제",
  "llm.model_create": "모델 생성",
  "llm.model_update": "모델 수정",
  "llm.model_delete": "모델 삭제",
  "rag.settings_update": "RAG 설정 변경",
  "rag.reindex": "재인덱싱 요청",
  "limits.settings_update": "사용 제한 변경",
  // 코어 v0.26.0 「사용 제한」(#386·#387·#388) — 관리 › 사용 제한의 코어 항목
  "rate_limits.update": "분당 레이트 리밋 변경",
  "usage_defaults.update": "일일 한도 기본값 변경",
  "source_upload.update": "업로드 최대 크기 변경",
  // ---- P5 추가(트랙 ops, 2026-10-15) — 코드 목록 출처(읽기 전용 수집):
  //   ① 코어 `biznotes-core` v0.15.0(`biznotes_core/**`)과 `apps/api/app/**`의 감사 기록 호출(`audit.record`·`_audit_isolated`·
  //      `_transition_requests(action=…)`) 인자 70종 ② dev DB `audit_logs`의 실제 코드 31종(모두 ①에 포함).
  //   위 8종은 P1에 있던 항목이고 아래 62종을 더했다. 그 밖의(새로 생긴) 코드는 원시 코드 그대로 보인다.
  // 사용자·그룹·사용 한도
  "user.create": "사용자 생성",
  "user.update": "사용자 수정",
  "user.delete": "사용자 삭제",
  "user.password_reset": "비밀번호 초기화",
  "user.password_change": "비밀번호 변경",
  "usage_limits.update": "사용 한도 변경",
  "group.create": "그룹 생성",
  "group.delete": "그룹 삭제",
  "group.member_add": "그룹 구성원 추가",
  "group.member_remove": "그룹 구성원 제거",
  // LLM 게이트웨이
  "llm.circuit_reset": "LLM 프로바이더 차단 해제",
  "llm.model_import": "모델 가져오기",
  "llm.model_set_default": "기본 모델 지정",
  // 에이전트
  "agent.create": "에이전트 생성",
  "agent.update": "에이전트 수정",
  "agent.delete": "에이전트 삭제",
  // MCP
  "mcp.server_create": "MCP 서버 생성",
  "mcp.server_update": "MCP 서버 수정",
  "mcp.server_delete": "MCP 서버 삭제",
  "mcp.tools_approve": "MCP 도구 승인",
  "mcp.exposure_change": "MCP 서버 공개 범위 변경",
  "mcp.call": "MCP 도구 호출",
  "mcp.call_result": "MCP 도구 호출 결과",
  // 스킬
  "skill.create": "스킬 생성",
  "skill.update": "스킬 수정",
  "skill.delete": "스킬 삭제",
  "skill.clone": "스킬 복제",
  "skill.import": "스킬 가져오기",
  "skill.export": "스킬 내보내기",
  "skill.frontmatter.update": "스킬 초안 정보 수정",
  "skill.freeze": "스킬 버전 고정",
  "skill.publish": "스킬 버전 고정·공개",
  "skill.expose": "스킬 공개",
  "skill.retract": "스킬 공개 철회",
  "skill.unexpose": "스킬 비공개 전환",
  "skill.demote": "스킬 공개 범위 축소",
  "skill.enable": "스킬 활성화",
  "skill.disable": "스킬 비활성화",
  "skill.review.request": "스킬 검토 요청",
  "skill.review.approve": "스킬 검토 승인",
  "skill.review.reject": "스킬 검토 거절",
  "skill.review.cancel": "스킬 검토 요청 취소",
  "skill.review.withdraw": "스킬 승인 철회",
  "skill.review.read": "스킬 검토 파일 열람",
  // 워크플로우
  "workflow.create": "워크플로우 생성",
  "workflow.update": "워크플로우 수정",
  "workflow.delete": "워크플로우 삭제",
  "workflow.duplicate": "워크플로우 복제",
  "workflow.publish": "워크플로우 게시",
  "workflow.unpublish": "워크플로우 게시 취소",
} as const satisfies Dict;

export function auditActionLabel(code: string | null | undefined): string {
  return labelFor(AUDIT_ACTION_LABEL, code);
}
