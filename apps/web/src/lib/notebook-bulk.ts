import { ApiError, PASSWORD_CHANGE_REQUIRED } from "@/lib/api-client";
import type { NotebookShare } from "@/lib/types";

/**
 * 노트북 일괄 작업의 순수 규칙(plan §5.4) — 오류 분류와 항목별 결과 표현.
 * 서버 `detail` 원문은 화면에 내보내지 않고 분류된 고정 문구로 바꾼다(plan §7.2 민감 데이터).
 */

export type BulkErrorKind =
  | "forbidden"
  | "not_found"
  | "target_group_missing"
  | "invalid"
  | "server"
  | "auth_expired"
  | "password_change_required"
  | "unconfirmed"
  | "other";

export interface ClassifiedError {
  kind: BulkErrorKind;
  message: string;
  /** 전역 중단(401·403 `password_change_required`) — 새 항목을 시작하지 않는다(D-06). */
  globalStop: boolean;
  /** 대상 중단(D-08, 공유 전용) — 고른 그룹에 공유할 수 없으므로 새 항목을 시작하지 않는다. */
  targetStop: boolean;
  /** 요청은 보냈으나 응답을 받지 못함 — 처리 여부 미확인(PR-05). 성공으로 세지 않는다. */
  unconfirmed: boolean;
}

export const BULK_MESSAGES = {
  forbidden: "권한 없음 — 소유자만 할 수 있습니다",
  notFound: "찾을 수 없음 — 이미 삭제되었거나 접근할 수 없습니다",
  // 코어 404 "Target group not found" — 노트북 실제 소유자가 속하지 않은(또는 없는) 그룹(core-v0.20.0 §5.8)
  targetGroupMissing: "이 그룹에는 공유할 수 없습니다 — 소유자가 속한 그룹이 아닙니다",
  invalid: "요청 형식 오류",
  server: "서버 오류",
  authExpired: "로그인이 만료되었습니다",
  passwordChangeRequired: "비밀번호 변경이 필요합니다",
  unconfirmed: "처리 여부를 확인할 수 없습니다 — 목록을 새로 고친 뒤 확인하세요",
  notRun: "실행하지 않음",
  deleted: "삭제됨",
  shared: "공유됨",
  addedWrite: "편집 가능 권한으로 변경됨",
  // 공유 전 `GET /shares`가 응답 없이 끝났다 — 이 항목에는 쓰기 요청을 보내지 않았으므로 「확인 불가」가 아니다
  shareCheckLost: "기존 공유를 확인하지 못했습니다 — 변경하지 않았습니다",
  // 요청자가 속한 그룹이 0개(`GET /me/groups` → []) — 그룹은 관리자만 만든다(core-v0.20.0 §5.8). 단건·일괄 공유가 같이 쓴다
  noGroups: "공유할 그룹이 없습니다 — 관리자에게 그룹을 요청하세요",
} as const;

// 코어 `biznotes_core/api/v1/notebooks.py`(v0.20.0 `share`)의 그룹 대상 404 문구(D-08). 비구성원 그룹도 없는 그룹과
// 같은 문구다(#278). 서버 원문은 화면에 보이지 않고 이 분류로만 쓴다.
const TARGET_GROUP_MISSING_DETAIL = "Target group not found";

function classified(
  kind: BulkErrorKind,
  message: string,
  flags: Partial<Pick<ClassifiedError, "globalStop" | "targetStop" | "unconfirmed">> = {},
): ClassifiedError {
  return { kind, message, globalStop: false, targetStop: false, unconfirmed: false, ...flags };
}

/** 항목 하나의 실패를 분류한다(plan §5.4 오류 분류표). */
export function classifyError(error: unknown): ClassifiedError {
  // ApiError가 아니면 응답을 받지 못한 것이다(네트워크 오류·연결 끊김). 서버는 커밋 뒤 응답하므로
  // 실제로는 처리됐을 수 있다 — 실패로 두되 「확인 불가」로 표시한다(PR-05).
  if (!(error instanceof ApiError)) {
    return classified("unconfirmed", BULK_MESSAGES.unconfirmed, { unconfirmed: true });
  }
  const { status, detail, code } = error;
  if (status === 401) {
    return classified("auth_expired", BULK_MESSAGES.authExpired, { globalStop: true });
  }
  if (status === 403) {
    return code === PASSWORD_CHANGE_REQUIRED
      ? classified("password_change_required", BULK_MESSAGES.passwordChangeRequired, {
          globalStop: true,
        })
      : classified("forbidden", BULK_MESSAGES.forbidden);
  }
  if (status === 404) {
    if (detail === TARGET_GROUP_MISSING_DETAIL) {
      return classified("target_group_missing", BULK_MESSAGES.targetGroupMissing, { targetStop: true });
    }
    return classified("not_found", BULK_MESSAGES.notFound);
  }
  if (status === 422) return classified("invalid", BULK_MESSAGES.invalid);
  if (status >= 500) return classified("server", BULK_MESSAGES.server);
  return classified("other", `요청 실패 (HTTP ${status})`);
}

/** 결과 목록 한 줄. 삭제(Phase 2)와 공유(Phase 3)가 함께 쓴다. */
export type BulkItemStatus = "succeeded" | "skipped" | "failed" | "not_run";

export interface BulkTarget {
  id: string;
  title: string;
}

export interface BulkItemResult extends BulkTarget {
  status: BulkItemStatus;
  message: string;
  /** 응답 손실 — `failed`이지만 서버에서 처리됐을 수 있다. */
  unconfirmed: boolean;
}

/**
 * 실행이 끝나기 전에 새 항목 시작을 멈춘 이유. 사용자 「중지」, 전역 인증 중단(D-06), 공유 대상 중단(D-08 —
 * 고른 그룹에 공유할 수 없음: 코어 404 "Target group not found").
 */
export type BulkStopReason = "user" | "auth_expired" | "password_change_required" | "target_group_missing";

export const STOP_NOTICES: Record<BulkStopReason, string> = {
  user: "중지했습니다 — 시작하지 않은 항목은 실행하지 않았습니다",
  auth_expired: "로그인이 만료되어 중단했습니다 — 다시 로그인한 뒤 남은 항목을 실행하세요",
  password_change_required: "비밀번호 변경이 필요해 중단했습니다 — 변경한 뒤 남은 항목을 실행하세요",
  target_group_missing:
    "이 그룹에는 공유할 수 없어 중단했습니다 — 소유자가 속한 그룹인지 확인한 뒤 남은 항목을 다시 실행하세요",
};

export interface BulkSummary {
  total: number;
  succeeded: number;
  skipped: number;
  /** 확인 불가를 뺀 실패 수 — `succeeded + skipped + failed + unconfirmed + notRun === total`. */
  failed: number;
  unconfirmed: number;
  notRun: number;
}

export function summarizeResults(results: readonly BulkItemResult[]): BulkSummary {
  const summary: BulkSummary = {
    total: results.length,
    succeeded: 0,
    skipped: 0,
    failed: 0,
    unconfirmed: 0,
    notRun: 0,
  };
  for (const r of results) {
    if (r.status === "succeeded") summary.succeeded += 1;
    else if (r.status === "skipped") summary.skipped += 1;
    else if (r.status === "not_run") summary.notRun += 1;
    else if (r.unconfirmed) summary.unconfirmed += 1;
    else summary.failed += 1;
  }
  return summary;
}

/** 선택에서 뺄 항목(S-7) — 성공·건너뜀. 실패(확인 불가 포함)·미실행은 선택을 유지한다. */
export function settledIds(results: readonly BulkItemResult[]): string[] {
  return results.filter((r) => r.status === "succeeded" || r.status === "skipped").map((r) => r.id);
}

/** 삭제 요약 문구(AC-2.5) — 「삭제 a · 실패 b · 확인 불가 u · 미실행 c」. */
export function formatDeleteSummary(summary: BulkSummary): string {
  return `삭제 ${summary.succeeded} · 실패 ${summary.failed} · 확인 불가 ${summary.unconfirmed} · 미실행 ${summary.notRun}`;
}

/** 공유 요약 문구 — 「공유 a · 건너뜀 s · 실패 b · 확인 불가 u · 미실행 c」(공유 = shared + added_write). */
export function formatShareSummary(summary: BulkSummary): string {
  return `공유 ${summary.succeeded} · 건너뜀 ${summary.skipped} · 실패 ${summary.failed} · 확인 불가 ${summary.unconfirmed} · 미실행 ${summary.notRun}`;
}

/** 확인 다이얼로그에 보일 제목 — 최대 `limit`개와 나머지 개수(D-12). */
export function previewTitles(
  targets: readonly BulkTarget[],
  limit = 10,
): { shown: string[]; rest: number } {
  return {
    shown: targets.slice(0, limit).map((t) => t.title),
    rest: Math.max(0, targets.length - limit),
  };
}

// ---- 일괄 공유(plan §5.5) ----

export type SharePermission = "read" | "write";

type ExistingPermission = NotebookShare["permission"];

const PERMISSION_RANK: Record<ExistingPermission, number> = { read: 1, write: 2 };

export type ShareDecision =
  | { action: "post"; result: "shared" | "added_write" }
  | { action: "skip"; existing: ExistingPermission };

/**
 * 공유 결정표(§5.5, core-v0.20.0 §5.8 그룹판). 대상 그룹 G의 기존 행 권한 집합 E와 요청 권한 p로 정한다(read < write).
 * - E = ∅ → POST(`shared`)
 * - max(E) ≥ p → 호출하지 않음(`skip`)
 * - max(E) = read, p = write → POST(`added_write`) — 코어 v0.19.0부터 같은 대상 POST는 가장 먼저 만든
 *   행의 권한을 바꾼다(#222). 그래서 행이 늘지 않고 그 read 행이 write로 올라간다
 * 다른 그룹 행은 보지 않는다. DELETE를 내는 동작은 결정표에 없다(NG-3·K9).
 *
 * 「건너뜀」은 **결정 시점 기준**이다(core-v0.20.0 §5.3·PR-16) — 결정용 `GET /shares`와 쓰기 POST는 별도
 * 요청이라, 그 사이 다른 곳에서 G에게 더 높은 권한을 주면 이 작업의 POST가 그 행을 p로 덮을 수 있다.
 */
export function decideShare(
  targetGroupId: string,
  permission: SharePermission,
  rows: readonly Pick<NotebookShare, "group_id" | "permission">[],
): ShareDecision {
  let best: ExistingPermission | null = null;
  for (const row of rows) {
    if (!row || row.group_id !== targetGroupId) continue;
    const rank = PERMISSION_RANK[row.permission] ?? 0;
    if (rank > 0 && (best === null || rank > PERMISSION_RANK[best])) best = row.permission;
  }
  if (best === null) return { action: "post", result: "shared" };
  if (PERMISSION_RANK[best] >= PERMISSION_RANK[permission]) return { action: "skip", existing: best };
  return { action: "post", result: "added_write" };
}

/**
 * 건너뜀 문구 — 「이미 {권한 라벨} 권한으로 공유됨」(plan §6.3.2 — 원시값 `read`/`write`를 보이지 않는다).
 * 라벨은 호출부가 `lib/labels`의 `notebookPermissionLabel`로 바꿔 넘긴다 — `lib/labels`가 이 모듈의
 * `SharePermission`을 가져가므로(의존 방향 labels → notebook-bulk) 여기서는 `lib/labels`를 import하지 않는다.
 */
export function alreadySharedMessage(permissionLabel: string): string {
  return `이미 ${permissionLabel} 권한으로 공유됨`;
}
