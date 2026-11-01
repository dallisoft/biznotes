import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useDialogSlot, useOpenSession } from "./use-dialog-slot";

// P3 리뷰 P3-02 — 닫기 콜백은 그 콜백이 만들어진 렌더에서 열려 있던 창에만 작용한다(늦게 끝난 요청이 다른 창을 닫지 않음).

describe("useDialogSlot", () => {
  it("현재 창의 닫기 콜백은 닫고, true(열기 요청)는 무시한다", () => {
    const { result } = renderHook(() => useDialogSlot<string>());
    act(() => result.current.open("a"));
    expect(result.current.value).toBe("a");
    act(() => result.current.onOpenChange(true));
    expect(result.current.value).toBe("a");
    act(() => result.current.onOpenChange(false));
    expect(result.current.value).toBeNull();
  });

  it("다른 창을 연 뒤 이전 창의 닫기 콜백이 늦게 불려도 지금 창은 그대로다", () => {
    const { result } = renderHook(() => useDialogSlot<string>());
    act(() => result.current.open("share"));
    const staleClose = result.current.onOpenChange;
    act(() => result.current.onOpenChange(false)); // 사용자가 닫음(Esc)
    act(() => result.current.open("rename"));
    act(() => staleClose(false)); // 공유 요청이 늦게 성공
    expect(result.current.value).toBe("rename");
  });

  it("같은 값을 다시 연 새 창도 이전 세대의 닫기 콜백으로는 닫히지 않는다", () => {
    const { result } = renderHook(() => useDialogSlot<string>());
    act(() => result.current.open("rename"));
    const staleClose = result.current.onOpenChangeFor("rename");
    act(() => result.current.onOpenChange(false));
    act(() => result.current.open("rename"));
    act(() => staleClose(false));
    expect(result.current.value).toBe("rename");
    act(() => result.current.onOpenChangeFor("rename")(false));
    expect(result.current.value).toBeNull();
  });

  it("onOpenChangeFor는 지금 창의 값이 다르면 닫지 않는다", () => {
    const { result } = renderHook(() => useDialogSlot<string>());
    act(() => result.current.open("tags"));
    act(() => result.current.onOpenChangeFor("export")(false));
    expect(result.current.value).toBe("tags");
    act(() => result.current.onOpenChangeFor("tags")(false));
    expect(result.current.value).toBeNull();
  });

  it("닫힌 상태에서 만든 닫기 콜백은 아무것도 닫지 않는다", () => {
    const { result } = renderHook(() => useDialogSlot<string>());
    const closedClose = result.current.onOpenChange;
    act(() => result.current.open("a"));
    act(() => closedClose(false));
    expect(result.current.value).toBe("a");
  });
});

// P3 재리뷰 P3-R01 — 다이얼로그 안쪽의 열린 회차
describe("useOpenSession", () => {
  it("열릴 때와 열린 채 대상이 바뀔 때만 회차가 오르고, 닫기만으로는 오르지 않는다(닫히는 동안 폼 유지)", () => {
    const { result, rerender } = renderHook(({ open, target }) => useOpenSession(open, target), {
      initialProps: { open: false, target: "a" as string | null },
    });
    expect(result.current.session).toBe(0);
    rerender({ open: true, target: "a" });
    expect(result.current.session).toBe(1);
    rerender({ open: true, target: "a" });
    expect(result.current.session).toBe(1);
    rerender({ open: false, target: "a" });
    expect(result.current.session).toBe(1);
    rerender({ open: true, target: "a" });
    expect(result.current.session).toBe(2);
    rerender({ open: true, target: "b" });
    expect(result.current.session).toBe(3);
    rerender({ open: false, target: null });
    expect(result.current.session).toBe(3);
  });

  it("isCurrent는 제출 시점의 회차가 지금 회차일 때만 참이다(닫았다 다시 열면 거짓)", () => {
    const { result, rerender } = renderHook(({ open }) => useOpenSession(open, "a"), {
      initialProps: { open: true },
    });
    const submitted = result.current.session;
    expect(result.current.isCurrent(submitted)).toBe(true);
    rerender({ open: false });
    expect(result.current.isCurrent(submitted)).toBe(true); // 닫기만 했으면 그 창의 요청(닫힌 창 — 후처리는 무해)
    rerender({ open: true });
    expect(result.current.isCurrent(submitted)).toBe(false);
  });
});
