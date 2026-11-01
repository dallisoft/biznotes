"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useRef, useState } from "react";
import { api } from "@/lib/api-client";
import { runBounded } from "@/lib/bulk-run";
import {
  alreadySharedMessage,
  BULK_MESSAGES,
  type ClassifiedError,
  classifyError,
  decideShare,
  type BulkItemResult,
  type BulkStopReason,
  type BulkTarget,
  type ShareDecision,
  type SharePermission,
} from "@/lib/notebook-bulk";
import { notebookPermissionLabel } from "@/lib/labels";
import { qk } from "@/lib/query-keys";
import type { NotebookShare } from "@/lib/types";

/** 동시 실행 상한(D-05). 자동 재시도는 하지 않는다. */
export const BULK_CONCURRENCY = 3;

export interface BulkRunReport {
  /** 입력(표시) 순서와 같은 항목별 결과. */
  results: BulkItemResult[];
  /** 새 항목 시작을 멈춘 이유. 끝까지 실행했으면 null. */
  stopReason: BulkStopReason | null;
}

/**
 * 일괄 삭제 본문(plan §5.6). 기존 단건 `DELETE /notebooks/{id}`를 상한 3으로 반복한다.
 * 401(refresh 실패)·403 `password_change_required`를 받으면 새 항목을 시작하지 않는다(D-06) —
 * 이미 보낸 요청은 응답을 기다려 그 결과로 기록한다. 캐시 처리는 `useBulkDelete`가 한다.
 */
export async function runBulkDelete(
  targets: readonly BulkTarget[],
  opts: { shouldStop: () => boolean; onProgress?: (done: number) => void },
): Promise<BulkRunReport> {
  // 클로저(worker)가 세우는 값이라 객체에 둔다 — 지역 `let`이면 타입 좁히기가 null로 고정된다
  const halt: { reason: BulkStopReason | null } = { reason: null };
  let done = 0;
  const outcomes = await runBounded(
    targets,
    async (target) => {
      try {
        await api.delete<void>(`/api/v1/notebooks/${target.id}`);
      } catch (error) {
        // worker 안에서 세운다 — 이 항목이 끝나 다음 항목을 집기 전에 중단이 보이도록
        const c = classifyError(error);
        if (c.globalStop && halt.reason === null) {
          halt.reason = c.kind === "password_change_required" ? "password_change_required" : "auth_expired";
        }
        throw error;
      }
    },
    {
      concurrency: BULK_CONCURRENCY,
      shouldStop: () => halt.reason !== null || opts.shouldStop(),
      onSettled: () => {
        done += 1;
        opts.onProgress?.(done);
      },
    },
  );

  const results = outcomes.map((outcome, i): BulkItemResult => {
    const { id, title } = targets[i];
    if (outcome.status === "succeeded") {
      return { id, title, status: "succeeded", message: BULK_MESSAGES.deleted, unconfirmed: false };
    }
    if (outcome.status === "not_run") {
      return { id, title, status: "not_run", message: BULK_MESSAGES.notRun, unconfirmed: false };
    }
    const c = classifyError(outcome.error);
    return { id, title, status: "failed", message: c.message, unconfirmed: c.unconfirmed };
  });
  const anyNotRun = results.some((r) => r.status === "not_run");
  return { results, stopReason: halt.reason ?? (anyNotRun && opts.shouldStop() ? "user" : null) };
}

export interface BulkShareRequest {
  targets: readonly BulkTarget[];
  /** 공유 대상 그룹 id — `/me/groups`(요청자 소속)에서 고른 값. */
  groupId: string;
  permission: SharePermission;
}

/** 전역 인증 중단·공유 대상 중단(D-06·D-08)에 해당하면 그 이유. */
function haltReasonOf(c: ClassifiedError): BulkStopReason | null {
  if (c.globalStop) return c.kind === "password_change_required" ? "password_change_required" : "auth_expired";
  if (c.targetStop) return "target_group_missing";
  return null;
}

/**
 * 일괄 공유 본문(plan §5.5·§5.7, core-v0.20.0 §5.8 그룹판). 그룹 하나에 단건 API로 권한을 준다.
 *
 * 1. 제출 시 캡처 — 항목·그룹·권한을 시작할 때 복사해 실행 중 바꾸지 않는다(PR-01).
 * 2. 항목마다(상한 3, 입력 순서로 시작) `GET /shares` → 결정표(`decideShare` — 같은 그룹 행이 같거나 높으면
 *    건너뜀) → 필요할 때만 `POST /share {group_id, permission}`. 대상 id를 미리 알므로 probe가 없다.
 *
 * 공유 행 `DELETE`와 과거 권한을 다시 쓰는 POST는 없다(core-v0.20.0 D-5·K9). 「건너뜀」은 결정 시점 기준이다.
 * POST는 `mode: "upgrade_only"`(코어 v0.21.0 #300)라 `GET`과 POST 사이에 다른 곳에서 올린 그 그룹의 권한을 내리지
 * 않는다 — 서버가 기존 권한과 요청 중 높은 쪽을 남긴다(core-v0.22.0-upgrade D4, PR-16 잔여 해소). 구 코어는 `mode`를
 * 무시해 요청 값으로 바꾼다(롤백 때 이 줄을 먼저 되돌린다).
 *
 * 중단(사용자 「중지」·401·403 `password_change_required`·그룹 404)은 HTTP 요청 사이에서만 본다 —
 * 진행 중인 요청은 끝까지 기다려 기록하고, 그 뒤에는 새 요청을 보내지 않는다(D-06). 사전 확인 `GET`이 실패한
 * 항목에는 쓰기 요청을 보내지 않는다. 응답을 잃은 POST는 성공으로 세지 않고 되돌리지도 않는다(PR-05).
 */
export async function runBulkShare(
  req: BulkShareRequest,
  opts: { shouldStop: () => boolean; onProgress?: (done: number) => void },
): Promise<BulkRunReport> {
  const items: BulkTarget[] = req.targets.map(({ id, title }) => ({ id, title }));
  const groupId = req.groupId;
  const permission = req.permission;

  const halt: { reason: BulkStopReason | null } = { reason: null };
  // 중단 판정 때문에 GET 뒤 POST를 보내지 않은 항목이 있었는지 — 사용자 중지 안내를 남길지 정한다
  const cut = { short: false };
  const userStopped = () => {
    try {
      return opts.shouldStop();
    } catch {
      return true; // 판정 자체가 실패하면 새 요청을 보내지 않는 쪽으로 닫는다(runBounded와 같다)
    }
  };
  const stopped = () => halt.reason !== null || userStopped();
  const raise = (c: ClassifiedError) => {
    halt.reason ??= haltReasonOf(c);
  };

  const sharesPath = (id: string) => `/api/v1/notebooks/${id}/shares`;
  const sharePath = (id: string) => `/api/v1/notebooks/${id}/share`;
  /** 기존 공유 행. 목록이 아닌 응답은 확인 실패로 다룬다(쓰기 요청 없이 끝난다). */
  const fetchShares = async (id: string): Promise<NotebookShare[]> => {
    const rows = await api.get<NotebookShare[]>(sharesPath(id));
    if (!Array.isArray(rows)) throw new TypeError("unexpected shares response");
    return rows;
  };
  const notRun = (t: BulkTarget): BulkItemResult => ({
    ...t,
    status: "not_run",
    message: BULK_MESSAGES.notRun,
    unconfirmed: false,
  });

  let done = 0;
  const outcomes = await runBounded(
    items,
    async (item): Promise<BulkItemResult> => {
      let before: NotebookShare[];
      try {
        before = await fetchShares(item.id);
      } catch (error) {
        // 사전 확인 실패 — 이 항목에는 쓰기 요청을 보내지 않았으므로 응답 손실이어도 「확인 불가」가 아니다
        const c = classifyError(error);
        raise(c);
        return {
          ...item,
          status: "failed",
          message: c.unconfirmed ? BULK_MESSAGES.shareCheckLost : c.message,
          unconfirmed: false,
        };
      }
      const decision: ShareDecision = decideShare(groupId, permission, before);
      if (decision.action === "skip") {
        return {
          ...item,
          status: "skipped",
          message: alreadySharedMessage(notebookPermissionLabel(decision.existing)),
          unconfirmed: false,
        };
      }
      if (stopped()) {
        cut.short = true;
        return notRun(item);
      }
      try {
        // 본문은 정확히 이 세 필드다 — 사용자 대상(사용자 id·이메일, deprecated)을 보내지 않는다(AC-3.4)
        await api.post<NotebookShare>(sharePath(item.id), { group_id: groupId, permission, mode: "upgrade_only" });
      } catch (error) {
        // worker 안에서 세운다 — 이 항목이 끝나 다음 항목을 집기 전에 중단이 보이도록(그룹 404·인증 만료)
        raise(classifyError(error));
        throw error;
      }
      return {
        ...item,
        status: "succeeded",
        message: decision.result === "shared" ? BULK_MESSAGES.shared : BULK_MESSAGES.addedWrite,
        unconfirmed: false,
      };
    },
    {
      concurrency: BULK_CONCURRENCY,
      shouldStop: stopped,
      onSettled: () => {
        done += 1;
        opts.onProgress?.(done);
      },
    },
  );

  const results = outcomes.map((outcome, i): BulkItemResult => {
    if (outcome.status === "succeeded") return outcome.value;
    if (outcome.status === "not_run") return notRun(items[i]);
    // 쓰기 요청(POST) 실패 — 응답 손실이면 「확인 불가」(처리됐을 수 있음)
    const c = classifyError(outcome.error);
    return { ...items[i], status: "failed", message: c.message, unconfirmed: c.unconfirmed };
  });
  const anyNotRun = results.some((r) => r.status === "not_run");
  return {
    results,
    stopReason: halt.reason ?? ((anyNotRun || cut.short) && userStopped() ? "user" : null),
  };
}

export type BulkRunPhase = "idle" | "running" | "done";

/**
 * 일괄 실행 상태(진행 k/N·중지·결과) — 삭제와 공유가 같이 쓴다. `run`은 실행 본문, `finish`는 끝난 뒤
 * 캐시 처리다. 확인 버튼을 연타해도 두 번 실행하지 않는다.
 */
function useBulkJob<A>(
  run: (args: A, ctl: { shouldStop: () => boolean; onProgress: (done: number) => void }) => Promise<BulkRunReport>,
  sizeOf: (args: A) => number,
  finish: (report: BulkRunReport) => void,
) {
  const stopRef = useRef(false);
  const runningRef = useRef(false);
  const [phase, setPhase] = useState<BulkRunPhase>("idle");
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [stopping, setStopping] = useState(false);
  const [report, setReport] = useState<BulkRunReport | null>(null);

  const start = useCallback(
    async (args: A): Promise<BulkRunReport | null> => {
      if (runningRef.current) return null; // 확인 버튼 연타로 두 번 실행하지 않는다
      runningRef.current = true;
      stopRef.current = false;
      const total = sizeOf(args);
      setStopping(false);
      setReport(null);
      setProgress({ done: 0, total });
      setPhase("running");
      try {
        const result = await run(args, {
          shouldStop: () => stopRef.current,
          onProgress: (done) => setProgress({ done, total }),
        });
        finish(result);
        setReport(result);
        setPhase("done");
        return result;
      } finally {
        runningRef.current = false;
      }
    },
    [run, sizeOf, finish],
  );

  const stop = useCallback(() => {
    if (!runningRef.current) return;
    stopRef.current = true;
    setStopping(true);
  }, []);

  const reset = useCallback(() => {
    if (runningRef.current) return;
    setPhase("idle");
    setReport(null);
    setStopping(false);
    setProgress({ done: 0, total: 0 });
  }, []);

  return { phase, progress, stopping, report, start, stop, reset };
}

const targetCount = (targets: readonly BulkTarget[]) => targets.length;
const shareCount = (req: BulkShareRequest) => req.targets.length;

/**
 * 일괄 삭제 실행 상태. 실행이 끝나면 목록 쿼리를 **1회** 무효화하고(D-13), 삭제에 성공한 노트북의
 * 상세 캐시(`["notebook", id]`)를 지운다. 단건 카드용 `useDeleteNotebook`은 항목마다 무효화하므로
 * 여기서는 쓰지 않는다.
 */
export function useBulkDelete() {
  const queryClient = useQueryClient();
  const finish = useCallback(
    (result: BulkRunReport) => {
      for (const r of result.results) {
        if (r.status === "succeeded") {
          queryClient.removeQueries({ queryKey: ["notebook", r.id], exact: true });
        }
      }
      void queryClient.invalidateQueries({ queryKey: ["notebooks"] });
    },
    [queryClient],
  );
  return useBulkJob(runBulkDelete, targetCount, finish);
}

/**
 * 일괄 공유 실행 상태. 끝나면 노트북 목록 쿼리를 **1회** 무효화하고(D-13), 열어 둔 적 있는 공유 목록 캐시
 * (`qk.notebookShares.all` — 단건 공유 관리 대화상자)도 한 번에 무효화한다.
 */
export function useBulkShare() {
  const queryClient = useQueryClient();
  const finish = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["notebooks"] });
    void queryClient.invalidateQueries({ queryKey: qk.notebookShares.all });
  }, [queryClient]);
  return useBulkJob(runBulkShare, shareCount, finish);
}
