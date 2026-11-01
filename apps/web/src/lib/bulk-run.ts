/**
 * 동시 실행 수에 상한을 둔 일괄 실행기(plan §5.4). 순수 모듈 — 네트워크·React를 모른다.
 *
 * 보장
 * 1. 동시에 실행 중인 worker는 `concurrency`개를 넘지 않는다.
 * 2. 결과 배열은 입력 순서와 같고, 한 항목이 실패해도 다른 항목은 계속 실행된다.
 * 3. `shouldStop()`이 참이 된 뒤에는 새 항목을 시작하지 않는다. 시작하지 않은 항목은 `not_run`이다.
 *    이미 시작한 worker는 끝까지 기다려 그 결과를 기록한다(D-06 — 요청을 abort하지 않는다).
 * 4. worker 예외는 `failed`로 포착하고, 실행기 자체는 reject하지 않는다(`onSettled` 예외도 삼킨다).
 */

export type BulkOutcome<R> =
  | { status: "succeeded"; value: R }
  | { status: "failed"; error: unknown }
  | { status: "not_run" };

export interface RunBoundedOptions<R> {
  concurrency: number;
  shouldStop: () => boolean;
  /** 항목 하나가 끝날 때마다(성공·실패) 부른다. 시작하지 않은 항목(`not_run`)에는 부르지 않는다. */
  onSettled?: (index: number, outcome: BulkOutcome<R>) => void;
}

export async function runBounded<T, R>(
  items: readonly T[],
  worker: (item: T, index: number) => Promise<R>,
  opts: RunBoundedOptions<R>,
): Promise<BulkOutcome<R>[]> {
  const results: (BulkOutcome<R> | undefined)[] = new Array(items.length).fill(undefined);
  const limit = Math.max(1, Math.floor(opts.concurrency) || 1);
  let next = 0;
  const stopped = () => {
    try {
      return opts.shouldStop();
    } catch {
      return true; // 판정 자체가 실패하면 새 항목을 시작하지 않는 쪽으로 닫는다
    }
  };

  async function lane(): Promise<void> {
    // 항목을 집기 직전에 매번 중단 여부를 본다 — 앞 항목의 결과로 세운 중단이 다음 항목 시작을 막는다
    while (next < items.length && !stopped()) {
      const index = next++;
      let outcome: BulkOutcome<R>;
      try {
        outcome = { status: "succeeded", value: await worker(items[index], index) };
      } catch (error) {
        outcome = { status: "failed", error };
      }
      results[index] = outcome;
      try {
        opts.onSettled?.(index, outcome);
      } catch {
        // 진행 표시 콜백의 오류가 실행기 결과를 깨뜨리지 않는다(보장 4)
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, lane));
  return results.map((r) => r ?? { status: "not_run" });
}
