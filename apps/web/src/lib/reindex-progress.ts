/**
 * 재인덱싱 진행률 순수 함수 (reindex-progress PLAN D-2·D-3·D-4·D-7).
 *
 * 진행률 수식·종료 판정·폴링 주기·포커스 run 선택을 한곳에 모아, 패널(시각 막대·텍스트·ARIA)과
 * 훅(refetchInterval·refetchOnWindowFocus·refetchOnReconnect)이 같은 결과를 쓰게 한다.
 */
import type { ReindexRun, ReindexStartOut } from "@/lib/admin-types";

export type ReindexStatus = "active" | "done" | "done_with_failures" | "closed_incomplete";

/** 내가 방금 시작한 run의 요청 범위 — `{}`(전체) 또는 노트북 지정. */
export type KnownReindexScope = "all" | { notebook_id: string };

export type FinishedAtKind = "none" | "last_item" | "unknown";

export interface ReindexSummary {
  status: ReindexStatus;
  sum: number;
  completed: number;
  total: number;
  /** 처리 전에 소스가 지워져 대상에서 빠진 항목(코어 v0.12.0 `removed`, 구 API는 0). */
  removed: number;
  /** `requested`와 집계 합의 차 — 코어 v0.12.0 이전 run에서만 생긴다. */
  missing: number;
  pct: number;
  /** `finishedAtKind === "unknown"`이면 null(경과 "알 수 없음"). */
  elapsedMs: number | null;
  finishedAtKind: FinishedAtKind;
  /** POST 직후 후보 줄 — `lastStart.run_id`가 이 run일 때만. */
  candidate: { total: number; enqueued: number; skipped: number } | null;
}

/** React Query `query.state`에서 폴링 정책이 읽는 부분. */
export interface PollState<T> {
  data?: T;
  error?: unknown;
  /** 연속 실패 횟수(성공 시 0). */
  fetchFailureCount?: number;
}

export type RunPollState = PollState<ReindexRun>;
export type ListPollState = PollState<readonly ReindexRun[]> & { list: true };

export interface ProgressSample {
  queued: number;
  done: number;
  failed: number;
  at: number;
}

export const STALL_NOTICE_MS = 180_000;

export const STALL_NOTICE_TEXT =
  "3분 이상 변화가 없습니다. 대기 항목은 워커가 실제로 처리 중인지 이 화면만으로는 알 수 없습니다 — 정체 시 안전망이 15분(대기열이 흐르면 최대 60분) 뒤 재발행하고, 3회 재발행 후에도 처리되지 않으면 실패로 닫습니다.";

/** `ApiError.status` 등 오류 객체의 HTTP 상태(없으면 undefined — 네트워크 오류). */
export function errorStatus(error: unknown): number | undefined {
  if (typeof error !== "object" || error === null || !("status" in error)) return undefined;
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" ? status : undefined;
}

/** 404/401/403 — 재시도하지 않는 종결 오류(D-2). */
export function isTerminalReindexError(error: unknown): boolean {
  const status = errorStatus(error);
  return status === 401 || status === 403 || status === 404;
}

export function isAuthError(error: unknown): boolean {
  const status = errorStatus(error);
  return status === 401 || status === 403;
}

/**
 * 코어 v0.12.0이 더한 응답 필드. 생성 타입은 필수지만 구 API(롤백 창)는 보내지 않는다 —
 * 없으면 종전 동작으로 돌아간다.
 */
type RunExtras = Partial<
  Pick<
    ReindexRun,
    "removed" | "retrying" | "max_attempts" | "last_update" | "skipped" | "candidates" | "scope"
  >
>;

function extras(run: ReindexRun): RunExtras {
  return run;
}

/** D-3: 종료 판정은 `queued == 0`만, 진척률은 `max(requested, 합계)` 분모 — 100% 강제 없음. */
export function summarizeRun(
  run: ReindexRun,
  now: number,
  opts: { lastStart?: ReindexStartOut | null } = {},
): ReindexSummary {
  const extra = extras(run);
  const removed = extra.removed ?? 0;
  const sum = run.queued + run.done + run.failed + removed;
  const completed = run.done + run.failed + removed;
  const total = Math.max(run.requested, sum);
  const missing = Math.max(run.requested - sum, 0);
  const terminal = run.queued === 0;
  const status: ReindexStatus = !terminal
    ? "active"
    : missing > 0
      ? "closed_incomplete"
      : run.failed > 0
        ? "done_with_failures"
        : "done";
  // `last_update`가 있고 null이면 항목이 없어 `finished_at`이 합성값이다. 필드가 없으면(구 API) 종전 판정
  const finishedAtKind: FinishedAtKind = !terminal
    ? "none"
    : extra.last_update === null
      ? "unknown"
      : extra.last_update !== undefined || sum > 0
        ? "last_item"
        : "unknown";
  const createdAt = Date.parse(run.created_at);
  const parsedFinished = run.finished_at ? Date.parse(run.finished_at) : Number.NaN;
  const end = terminal && Number.isFinite(parsedFinished) ? parsedFinished : now;
  const elapsedMs = finishedAtKind === "unknown" ? null : Math.max(0, end - createdAt);
  const lastStart = opts.lastStart;
  const candidate =
    extra.candidates != null && extra.skipped != null
      ? { total: extra.candidates, enqueued: run.requested, skipped: extra.skipped }
      : lastStart && lastStart.run_id === run.id
        ? {
            total: lastStart.enqueued + lastStart.skipped,
            enqueued: lastStart.enqueued,
            skipped: lastStart.skipped,
          }
        : null;

  return {
    status,
    sum,
    completed,
    total,
    removed,
    missing,
    pct: total > 0 ? Math.floor((completed / total) * 100) : 0,
    elapsedMs,
    finishedAtKind,
    candidate,
  };
}

/** `mm:ss`, 1시간 이상은 `h:mm:ss`. 음수는 0으로. */
export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1_000));
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const rest = seconds % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(rest).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

/**
 * D-3 범위 라벨. 코어 v0.12.0 run은 `scope`·`notebook_title` 스냅샷을 쓴다(노트북이 지워져도 유지).
 * 스냅샷이 없는 run은 `notebook_id == null`이고 범위를 모르면 "전체 또는 삭제된 노트북"(FK SET NULL).
 */
export function scopeLabel(
  run: ReindexRun,
  notebookTitles: ReadonlyMap<string, string>,
  knownScope?: KnownReindexScope | null,
): string {
  let label: string;
  const scope = extras(run).scope;
  const notebookId =
    run.notebook_id ?? (knownScope && knownScope !== "all" ? knownScope.notebook_id : null);
  if (scope === "all") {
    label = "전체";
  } else if (scope === "notebook") {
    const title =
      run.notebook_title ??
      (notebookId ? (notebookTitles.get(notebookId) ?? notebookId.slice(0, 8)) : "");
    label = `노트북 ${title}`;
  } else if (knownScope === "all" && run.notebook_id == null) {
    label = "전체";
  } else if (notebookId) {
    label = `노트북 ${notebookTitles.get(notebookId) ?? notebookId.slice(0, 8)}`;
  } else {
    label = "전체 또는 삭제된 노트북";
  }
  if (run.failed_only) label += " · 실패만";
  if (run.refetch) label += " · 원본 재수집";
  return label;
}

/**
 * 최근 run 이력 목록의 폴링 — 활성 run 탐색은 `active_only` 질의가 맡으므로 항상 유휴 30초,
 * 오류 백오프는 목록 정책과 같다(core-v0.12.0-upgrade AC-2.5 ②).
 */
export function historyPollIntervalFor(state: PollState<readonly ReindexRun[]>): number | false {
  return pollIntervalFor(listState({ ...state, data: [] }));
}

/** 목록 쿼리 상태 표지 — 같은 `pollIntervalFor`가 활성 2초 / 유휴 30초를 돌려준다. */
export function listState(state: PollState<readonly ReindexRun[]>): ListPollState {
  return { ...state, list: true };
}

function isListState(state: RunPollState | ListPollState): state is ListPollState {
  return "list" in state && state.list === true;
}

/**
 * D-2 폴링 주기 — 상세: 활성 1초·종결 false, 목록: 활성 2초·유휴 30초,
 * 공통 오류: 404/401/403 false, 429 30초, 5xx·네트워크 5→10→20→40→60초(상한).
 * 첫 응답 전(데이터·오류 없음)은 상세 1초 / 목록 2초.
 */
export function pollIntervalFor(state: RunPollState | ListPollState): number | false {
  if (state.error) {
    const status = errorStatus(state.error);
    if (status === 401 || status === 403 || status === 404) return false;
    if (status === 429) return 30_000;
    const failures = Math.max(1, state.fetchFailureCount ?? 1);
    return Math.min(5_000 * 2 ** (failures - 1), 60_000);
  }
  if (isListState(state)) {
    if (state.data === undefined) return 2_000;
    return state.data.some((run) => run.queued > 0) ? 2_000 : 30_000;
  }
  if (state.data === undefined) return 1_000;
  return state.data.queued > 0 ? 1_000 : false;
}

/**
 * D-3 포커스 run: ① startedRunId가 활성이면 그것 ② 활성 run 중 최신 ③ 종결된 startedRun
 * ④ null. dismissed는 전 단계에서 제외.
 */
export function selectFocusedRun(
  runs: readonly ReindexRun[],
  startedRunId: string | null,
  dismissedRunIds: ReadonlySet<string>,
): ReindexRun | null {
  const available = runs.filter((run) => !dismissedRunIds.has(run.id));
  const started = startedRunId ? available.find((run) => run.id === startedRunId) : undefined;
  if (started && started.queued > 0) return started;
  const active = available
    .filter((run) => run.queued > 0)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
  return active[0] ?? started ?? null;
}

/** D-7: `(queued, done, failed)`가 마지막으로 바뀐 뒤 3분 이상이면 reaper 안내 문구. */
export function stallNotice(samples: readonly ProgressSample[], now: number): string | null {
  const current = samples.at(-1);
  if (!current) return null;
  let unchangedSince = current.at;
  for (let i = samples.length - 2; i >= 0; i -= 1) {
    const sample = samples[i];
    if (
      sample.queued !== current.queued ||
      sample.done !== current.done ||
      sample.failed !== current.failed
    ) {
      break;
    }
    unchangedSince = sample.at;
  }
  return now - unchangedSince >= STALL_NOTICE_MS ? STALL_NOTICE_TEXT : null;
}
