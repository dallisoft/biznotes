import { describe, expect, it, vi } from "vitest";
import { runBounded } from "./bulk-run";

// AC-2.2 — 실제 runBounded를 계측 worker로 실행한다(대체 없음).

/** 손으로 끝낼 수 있는 worker. 동시 실행 수와 시작 순서를 기록한다. */
function controlledWorker() {
  let active = 0;
  let maxActive = 0;
  const started: number[] = [];
  const pending = new Map<number, { resolve: (v: string) => void; reject: (e: unknown) => void }>();
  const worker = (item: number) =>
    new Promise<string>((resolve, reject) => {
      active += 1;
      maxActive = Math.max(maxActive, active);
      started.push(item);
      pending.set(item, {
        resolve: (v) => {
          active -= 1;
          resolve(v);
        },
        reject: (e) => {
          active -= 1;
          reject(e);
        },
      });
    });
  return {
    worker,
    started,
    get active() {
      return active;
    },
    get maxActive() {
      return maxActive;
    },
    finish(item: number, ok = true) {
      const p = pending.get(item);
      if (!p) throw new Error(`item ${item} 은 시작하지 않았다`);
      pending.delete(item);
      if (ok) p.resolve(`ok-${item}`);
      else p.reject(new Error(`fail-${item}`));
    },
  };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("runBounded", () => {
  it("concurrency=3, 7개, 2개 실패 — 동시 최대 3, 입력 순서 결과, 실패 격리", async () => {
    const w = controlledWorker();
    const settled: number[] = [];
    const run = runBounded([0, 1, 2, 3, 4, 5, 6], w.worker, {
      concurrency: 3,
      shouldStop: () => false,
      onSettled: (i) => settled.push(i),
    });
    await flush();
    expect(w.started).toEqual([0, 1, 2]);
    expect(w.active).toBe(3);

    // 완료 순서를 뒤섞는다 — 결과 배열은 그래도 입력 순서여야 한다
    w.finish(2);
    await flush();
    w.finish(0, false);
    await flush();
    w.finish(4);
    await flush();
    w.finish(1);
    await flush();
    w.finish(3, false);
    await flush();
    w.finish(6);
    await flush();
    w.finish(5);
    const results = await run;

    expect(w.maxActive).toBe(3);
    expect(w.started).toEqual([0, 1, 2, 3, 4, 5, 6]);
    expect(settled).toEqual([2, 0, 4, 1, 3, 6, 5]);
    expect(results.map((r) => r.status)).toEqual([
      "failed",
      "succeeded",
      "succeeded",
      "failed",
      "succeeded",
      "succeeded",
      "succeeded",
    ]);
    expect(results[1]).toEqual({ status: "succeeded", value: "ok-1" });
    expect(results[0]).toMatchObject({ status: "failed", error: new Error("fail-0") });
  });

  it("shouldStop 이후 새 항목을 시작하지 않고 미시작 항목은 not_run, 진행 중 항목은 결과대로 기록한다", async () => {
    const w = controlledWorker();
    let stop = false;
    const run = runBounded([0, 1, 2, 3, 4, 5, 6], w.worker, {
      concurrency: 3,
      shouldStop: () => stop,
    });
    await flush();
    w.finish(0);
    await flush(); // 0이 끝나 3이 시작됐다
    expect(w.started).toEqual([0, 1, 2, 3]);

    stop = true; // 1·2·3은 진행 중
    w.finish(1);
    await flush();
    w.finish(2, false);
    await flush();
    expect(w.started).toEqual([0, 1, 2, 3]); // 새 항목 0개
    w.finish(3);
    const results = await run;

    expect(results.map((r) => r.status)).toEqual([
      "succeeded",
      "succeeded",
      "failed",
      "succeeded",
      "not_run",
      "not_run",
      "not_run",
    ]);
  });

  it("처음부터 중단 상태면 아무것도 시작하지 않는다", async () => {
    const worker = vi.fn(async () => "x");
    const results = await runBounded([1, 2], worker, { concurrency: 3, shouldStop: () => true });
    expect(worker).not.toHaveBeenCalled();
    expect(results).toEqual([{ status: "not_run" }, { status: "not_run" }]);
  });

  it("실행기 자체는 reject하지 않는다 — 동기 throw·onSettled 예외·shouldStop 예외도 포착한다", async () => {
    const results = await runBounded(
      ["a", "b"],
      (item) => {
        if (item === "a") throw new Error("sync");
        return Promise.resolve(item);
      },
      {
        concurrency: 3,
        shouldStop: () => false,
        onSettled: () => {
          throw new Error("progress");
        },
      },
    );
    expect(results).toEqual([
      { status: "failed", error: new Error("sync") },
      { status: "succeeded", value: "b" },
    ]);

    const stopped = await runBounded([1], async () => 1, {
      concurrency: 1,
      shouldStop: () => {
        throw new Error("판정 실패");
      },
    });
    expect(stopped).toEqual([{ status: "not_run" }]);
  });

  it("concurrency가 항목 수보다 크거나 1 미만이어도 안전하다", async () => {
    const w = controlledWorker();
    const run = runBounded([0, 1], w.worker, { concurrency: 10, shouldStop: () => false });
    await flush();
    expect(w.started).toEqual([0, 1]);
    w.finish(0);
    w.finish(1);
    expect((await run).map((r) => r.status)).toEqual(["succeeded", "succeeded"]);

    const s = controlledWorker();
    const serial = runBounded([0, 1], s.worker, { concurrency: 0, shouldStop: () => false });
    await flush();
    expect(s.started).toEqual([0]); // 1 미만은 1로 본다
    s.finish(0);
    await flush();
    s.finish(1);
    await serial;
    expect(s.maxActive).toBe(1);

    expect(await runBounded([], s.worker, { concurrency: 3, shouldStop: () => false })).toEqual([]);
  });
});
