import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { type RetryFocusStatus, useRetryFocusLanding } from "./use-retry-focus-landing";

// 「다시 시도」 포커스 착지점의 단일 규칙(P6 정리, HCI-P4-4) — P2 관리 개요 카드 칸·P3 연결 카드·P4 화면이 같은 훅을 쓴다.
// 실제 DOM 포커스로 검사한다(포커스 코드는 mock하지 않는다).

type Spot = "fallback" | "region" | "none";

function Harness({
  status,
  spot,
  pendingLink = false,
}: {
  status: RetryFocusStatus;
  spot: Spot;
  pendingLink?: boolean;
}) {
  const { regionRef, markRetry } = useRetryFocusLanding<HTMLElement>(status);
  return (
    <>
      <button type="button">영역 밖</button>
      <section ref={regionRef} aria-label="영역" tabIndex={spot === "region" ? -1 : undefined}>
        {spot === "fallback" && (
          <h2 tabIndex={-1} data-focus-fallback="">
            제목
          </h2>
        )}
        {status === "error" && (
          <button type="button" onClick={() => markRetry()}>
            다시 시도
          </button>
        )}
        {status === "pending" && pendingLink && <a href="/x">카드 링크</a>}
        {status === "success" && <button type="button">결과</button>}
      </section>
    </>
  );
}

/** 「다시 시도」에 포커스를 두고 누른 뒤(markRetry) 로딩으로 바꾼다 — 버튼이 사라져 포커스가 body로 떨어진다. */
function retryToPending(spot: Spot, pendingLink = false) {
  const view = render(<Harness status="error" spot={spot} pendingLink={pendingLink} />);
  const retry = screen.getByRole("button", { name: "다시 시도" });
  act(() => retry.focus());
  act(() => retry.click());
  view.rerender(<Harness status="pending" spot={spot} pendingLink={pendingLink} />);
  return view;
}

describe("useRetryFocusLanding — 다시 시도 포커스의 단일 규칙", () => {
  it("로딩 동안은 대체 제목에 맡기고, 결과가 오면 영역 첫 포커스 가능 요소로 간다", () => {
    const view = retryToPending("fallback");
    expect(screen.getByRole("heading", { name: "제목" })).toHaveFocus();
    view.rerender(<Harness status="success" spot="fallback" />);
    expect(screen.getByRole("button", { name: "결과" })).toHaveFocus();
  });

  it("맡겨 둘 자리가 있으면 로딩 중에 보이는 링크보다 그 자리가 먼저다(결과가 오면 ①부터 다시 고른다)", () => {
    const view = retryToPending("fallback", true);
    expect(screen.getByRole("heading", { name: "제목" })).toHaveFocus();
    view.rerender(<Harness status="success" spot="fallback" />);
    expect(screen.getByRole("button", { name: "결과" })).toHaveFocus();
  });

  it("대체 제목이 없으면 영역 자체(tabIndex=-1)에 맡긴다", () => {
    const view = retryToPending("region");
    expect(screen.getByRole("region", { name: "영역" })).toHaveFocus();
    view.rerender(<Harness status="success" spot="region" />);
    expect(screen.getByRole("button", { name: "결과" })).toHaveFocus();
  });

  it("영역이 포커스를 받지 않고 대체 제목도 없으면 로딩 중에도 영역 안 첫 포커스 가능 요소로 간다(관리 개요 카드 칸·연결 카드)", () => {
    const view = retryToPending("none", true);
    expect(screen.getByRole("link", { name: "카드 링크" })).toHaveFocus();
    view.rerender(<Harness status="success" spot="none" />);
    expect(screen.getByRole("button", { name: "결과" })).toHaveFocus();
  });

  it("다시 실패하면 새 「다시 시도」로 간다(맡겨 둔 자리에서)", () => {
    const view = retryToPending("fallback");
    expect(screen.getByRole("heading", { name: "제목" })).toHaveFocus();
    view.rerender(<Harness status="error" spot="fallback" />);
    expect(screen.getByRole("button", { name: "다시 시도" })).toHaveFocus();
  });

  it("로딩 중 사용자가 다른 곳으로 옮긴 포커스는 결과가 와도 빼앗지 않는다", () => {
    const view = retryToPending("fallback");
    const outside = screen.getByRole("button", { name: "영역 밖" });
    act(() => outside.focus());
    view.rerender(<Harness status="success" spot="fallback" />);
    expect(outside).toHaveFocus();
  });

  it("다시 시도를 누르지 않았으면 상태가 바뀌어도 포커스를 옮기지 않는다", () => {
    const view = render(<Harness status="error" spot="fallback" />);
    view.rerender(<Harness status="pending" spot="fallback" />);
    view.rerender(<Harness status="success" spot="fallback" />);
    expect(document.activeElement).toBe(document.body);
  });
});
