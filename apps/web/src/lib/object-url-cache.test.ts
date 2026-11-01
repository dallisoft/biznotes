/** 산출물 object URL 해제(PR-2 리뷰 M3) — 실제 QueryClient·QueryObserver로 캐시 제거를 일으킨다. */
import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { installObjectUrlCleanup } from "@/lib/object-url-cache";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function client() {
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  const qc = new QueryClient({ defaultOptions: { queries: { gcTime: 1_000, retry: false } } });
  const uninstall = installObjectUrlCleanup(qc);
  return { qc, revoke, uninstall };
}

describe("installObjectUrlCleanup", () => {
  it("관찰자가 모두 떠나고 gcTime이 지나 캐시가 버리면 해제한다 — 쓰는 동안은 두 화면이 공유해도 남긴다", async () => {
    vi.useFakeTimers();
    const { qc, revoke } = client();
    const key = ["artifact-image", "run-1", "slide-image-1.png"];
    const options = { queryKey: key, queryFn: async () => "blob:img-1", staleTime: Infinity };
    const viewer = new QueryObserver(qc, options);
    const detail = new QueryObserver(qc, options);
    const offViewer = viewer.subscribe(() => undefined);
    const offDetail = detail.subscribe(() => undefined);
    await vi.waitFor(() => expect(qc.getQueryData(key)).toBe("blob:img-1"));

    offViewer();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(revoke).not.toHaveBeenCalled(); // 다른 화면이 아직 쓴다
    offDetail();
    await vi.advanceTimersByTimeAsync(999);
    expect(revoke).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2);
    expect(revoke).toHaveBeenCalledWith("blob:img-1");
    expect(qc.getQueryData(key)).toBeUndefined();
  });

  it("clear()(로그아웃 등)는 오디오·삽화 URL을 모두 해제하고, 다른 키·blob이 아닌 값은 두지 않는다", () => {
    const { qc, revoke } = client();
    qc.setQueryData(["artifact-image", "r", "a.png"], "blob:a");
    qc.setQueryData(["artifact-audio", "r", "a.mp3"], "blob:b");
    qc.setQueryData(["artifact", "r", "deck.json"], "blob:not-a-url-owner");
    qc.setQueryData(["artifact-image", "r", "failed.png"], undefined);
    qc.clear();
    expect(revoke.mock.calls.map(([u]) => u).sort()).toEqual(["blob:a", "blob:b"]);
  });

  it("구독을 풀면 더 해제하지 않는다", () => {
    const { qc, revoke, uninstall } = client();
    qc.setQueryData(["artifact-image", "r", "a.png"], "blob:a");
    uninstall();
    qc.clear();
    expect(revoke).not.toHaveBeenCalled();
  });
});
