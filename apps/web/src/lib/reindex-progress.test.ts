import { describe, expect, it } from "vitest";
import type { ReindexRun } from "@/lib/admin-types";
import {
  formatElapsed,
  historyPollIntervalFor,
  listState,
  pollIntervalFor,
  scopeLabel,
  selectFocusedRun,
  stallNotice,
  summarizeRun,
} from "./reindex-progress";

const CREATED = "2026-10-05T00:00:00.000Z";

function run(overrides: Partial<ReindexRun> = {}): ReindexRun {
  return {
    id: "run-1",
    notebook_id: null,
    failed_only: false,
    refetch: false,
    requested: 10,
    queued: 4,
    done: 5,
    failed: 1,
    created_at: CREATED,
    finished_at: null,
    removed: 0,
    retrying: 0,
    max_attempts: 3,
    stale_only: false,
    ...overrides,
  };
}

describe("reindex progress 순수 함수", () => {
  it("queued>0이면 status=active, pct=floor(completed/total*100)", () => {
    const result = summarizeRun(run(), Date.parse(CREATED) + 5_000);
    expect(result).toMatchObject({ status: "active", completed: 6, total: 10, pct: 60 });
  });

  it("queued==0·missing==0이면 done/done_with_failures, pct=100", () => {
    expect(
      summarizeRun(run({ queued: 0, done: 10, failed: 0, finished_at: CREATED }), 0),
    ).toMatchObject({ status: "done", pct: 100 });
    expect(
      summarizeRun(run({ queued: 0, done: 9, failed: 1, finished_at: CREATED }), 0),
    ).toMatchObject({ status: "done_with_failures", pct: 100 });
  });

  it("requested 3·항목 0이면 closed_incomplete, 0/3·0%, finishedAtKind=unknown", () => {
    const result = summarizeRun(
      run({ requested: 3, queued: 0, done: 0, failed: 0, finished_at: CREATED }),
      0,
    );
    expect(result).toMatchObject({
      status: "closed_incomplete",
      completed: 0,
      total: 3,
      pct: 0,
      missing: 3,
      finishedAtKind: "unknown",
      elapsedMs: null,
    });
  });

  it("requested 20·failed 15면 closed_incomplete, 15/20·75%, finishedAtKind=last_item", () => {
    const result = summarizeRun(
      run({ requested: 20, queued: 0, done: 0, failed: 15, finished_at: CREATED }),
      0,
    );
    expect(result).toMatchObject({
      status: "closed_incomplete",
      completed: 15,
      total: 20,
      pct: 75,
      missing: 5,
      finishedAtKind: "last_item",
    });
  });

  it("created_at이 now보다 미래여도 elapsedMs는 0", () => {
    expect(summarizeRun(run(), Date.parse(CREATED) - 10_000).elapsedMs).toBe(0);
  });

  it("종결 run의 elapsedMs는 finished_at-created_at", () => {
    const finished = "2026-10-05T00:02:03.000Z";
    expect(
      summarizeRun(run({ queued: 0, done: 9, finished_at: finished }), Date.parse(finished) + 99_000)
        .elapsedMs,
    ).toBe(123_000);
  });

  it("knownScope=all→전체 / notebook 제목 매핑 / 제목 없으면 id 8자 / null+복원→전체 또는 삭제된 노트북 / failed_only·refetch 접미", () => {
    const titles = new Map([["notebook-123", "제품 문서"]]);
    expect(scopeLabel(run(), titles, "all")).toBe("전체");
    expect(scopeLabel(run({ notebook_id: "notebook-123" }), titles)).toBe("노트북 제품 문서");
    expect(scopeLabel(run({ notebook_id: "abcdefgh-more" }), titles)).toBe("노트북 abcdefgh");
    expect(scopeLabel(run(), titles)).toBe("전체 또는 삭제된 노트북");
    expect(scopeLabel(run({ failed_only: true, refetch: true }), titles, "all")).toBe(
      "전체 · 실패만 · 원본 재수집",
    );
  });

  it("queued>0→1000, 종결→false, 404/401/403→false, 429→30000, 5xx·네트워크→5s·10s·20s·40s·60s 상한, 데이터·오류 없음(첫 응답 전)→1000", () => {
    expect(pollIntervalFor({ data: run() })).toBe(1_000);
    expect(pollIntervalFor({ data: run({ queued: 0 }) })).toBe(false);
    for (const status of [404, 401, 403]) {
      expect(pollIntervalFor({ error: { status } })).toBe(false);
    }
    expect(pollIntervalFor({ error: { status: 429 } })).toBe(30_000);
    expect(
      [1, 2, 3, 4, 5, 6].map((fetchFailureCount) =>
        pollIntervalFor({ error: { status: 500 }, fetchFailureCount }),
      ),
    ).toEqual([5_000, 10_000, 20_000, 40_000, 60_000, 60_000]);
    expect(pollIntervalFor({})).toBe(1_000);
    expect(pollIntervalFor(listState({ data: [run()] }))).toBe(2_000);
    expect(pollIntervalFor(listState({ data: [run({ queued: 0 })] }))).toBe(30_000);
  });

  it("startedRun 활성 > 최신 활성 > 종결된 startedRun > null, dismissed 제외", () => {
    const older = run({ id: "older", created_at: "2026-10-05T00:00:00Z" });
    const newer = run({ id: "newer", created_at: "2026-10-05T00:01:00Z" });
    const started = run({ id: "started", created_at: "2026-10-05T00:00:30Z" });
    expect(selectFocusedRun([older, newer, started], "started", new Set())?.id).toBe("started");
    expect(selectFocusedRun([older, newer], null, new Set())?.id).toBe("newer");
    expect(
      selectFocusedRun([run({ id: "closed", queued: 0, done: 10 })], "closed", new Set())?.id,
    ).toBe("closed");
    expect(selectFocusedRun([started], "started", new Set(["started"]))).toBeNull();
  });

  it("mm:ss와 1시간 이상 h:mm:ss", () => {
    expect(formatElapsed(62_000)).toBe("01:02");
    expect(formatElapsed(3_662_000)).toBe("1:01:02");
  });

  it("튜플 무변화 3분 미만은 null, 이상이면 reaper 15/60분·3회 문구", () => {
    const samples = [{ queued: 3, done: 1, failed: 0, at: 0 }];
    expect(stallNotice(samples, 179_999)).toBeNull();
    const notice = stallNotice(samples, 180_000);
    expect(notice).toContain("15분");
    expect(notice).toContain("60분");
    expect(notice).toContain("3회");
  });

  it("lastStart가 포커스 run이면 후보=enqueued+skipped, 아니면 null", () => {
    expect(
      summarizeRun(run(), 0, {
        lastStart: { run_id: "run-1", enqueued: 2, skipped: 3 },
      }).candidate,
    ).toEqual({ total: 5, enqueued: 2, skipped: 3 });
    expect(
      summarizeRun(run(), 0, {
        lastStart: { run_id: "other", enqueued: 2, skipped: 3 },
      }).candidate,
    ).toBeNull();
  });
});

/** 코어 v0.12.0 이전 응답 — 새 필드가 아예 없다(롤백 창의 새 web × 구 API). */
function legacy(overrides: Partial<ReindexRun> = {}): ReindexRun {
  const full = run(overrides) as Partial<ReindexRun>;
  for (const key of ["removed", "retrying", "max_attempts", "stale_only", "last_update"] as const) {
    delete full[key];
  }
  return full as ReindexRun;
}

describe("코어 v0.12.0 run 계약 (core-v0.12.0-upgrade AC-2.1~2.4)", () => {
  it("removed는 완료로 세고 requested = 합계면 집계 제외 0·완료", () => {
    const summary = summarizeRun(
      run({ requested: 3, queued: 0, done: 2, failed: 0, removed: 1, finished_at: CREATED }),
      0,
    );
    expect(summary).toMatchObject({ status: "done", removed: 1, missing: 0, completed: 3, pct: 100 });
  });

  it("구 API run(필드 부재)은 removed 0·집계 제외로 종전처럼 판정", () => {
    const summary = summarizeRun(legacy({ requested: 3, queued: 0, done: 2, failed: 0 }), 0);
    expect(summary).toMatchObject({ status: "closed_incomplete", removed: 0, missing: 1 });
    expect(summary.finishedAtKind).toBe("last_item");
  });

  it("last_update가 null이면(항목 없음) 종료 시각은 합성값이라 알 수 없음, 값이 있으면 마지막 항목 갱신", () => {
    const none = run({ requested: 0, queued: 0, done: 0, failed: 0, last_update: null });
    expect(summarizeRun(none, 0).finishedAtKind).toBe("unknown");
    const some = run({ queued: 0, done: 10, failed: 0, last_update: CREATED, finished_at: CREATED });
    expect(summarizeRun(some, 0).finishedAtKind).toBe("last_item");
  });

  it("구 API run에서 last_update 부재는 합계 휴리스틱을 유지한다", () => {
    expect(summarizeRun(legacy({ queued: 0, done: 0, failed: 0, requested: 0 }), 0).finishedAtKind).toBe(
      "unknown",
    );
  });

  it("candidates·skipped가 있으면 lastStart 없이도 후보 줄을 만든다", () => {
    const summary = summarizeRun(run({ requested: 2, skipped: 3, candidates: 5 }), 0);
    expect(summary.candidate).toEqual({ total: 5, enqueued: 2, skipped: 3 });
    expect(summarizeRun(run({ skipped: null, candidates: null }), 0).candidate).toBeNull();
  });

  it("scope 스냅샷: all은 전체, notebook은 노트북이 지워져도 notebook_title", () => {
    const titles = new Map<string, string>();
    expect(scopeLabel(run({ scope: "all" }), titles)).toBe("전체");
    expect(
      scopeLabel(run({ scope: "notebook", notebook_id: null, notebook_title: "지운 노트북" }), titles),
    ).toBe("노트북 지운 노트북");
    expect(scopeLabel(run({ scope: null, notebook_id: null }), titles)).toBe("전체 또는 삭제된 노트북");
  });

  it("이력 목록 폴링은 활성 run이 있어도 30초, 오류 백오프는 목록 정책과 같다", () => {
    expect(historyPollIntervalFor({ data: [run()] })).toBe(30_000);
    expect(historyPollIntervalFor({ data: [run(), run()], error: { status: 500 }, fetchFailureCount: 1 })).toBe(
      5_000,
    );
  });
});
