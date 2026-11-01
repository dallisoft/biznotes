import { afterEach, describe, expect, it } from "vitest";
import { FOCUSABLE_SELECTOR, focusFirstWithin, focusIsLost } from "./focus";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("focus 도우미(P2 리뷰 P2-01)", () => {
  it("focusIsLost — body·DOM에서 빠진 요소면 true, 연결된 요소에 포커스가 있으면 false", () => {
    document.body.innerHTML = '<button id="a">A</button>';
    const a = document.getElementById("a") as HTMLButtonElement;
    (document.activeElement as HTMLElement | null)?.blur();
    expect(focusIsLost()).toBe(true);
    a.focus();
    expect(focusIsLost()).toBe(false);
    a.remove();
    expect(focusIsLost()).toBe(true);
  });

  it("focusFirstWithin — 비활성·tabindex=-1을 건너뛰고 첫 포커스 가능 요소로 옮긴다, 없으면 false", () => {
    document.body.innerHTML = `
      <section id="r">
        <button disabled>비활성</button>
        <div tabindex="-1">프로그램 전용</div>
        <a>href 없음</a>
        <a href="/admin/mcp" id="link">MCP 서버에서 보기</a>
        <button id="later">나중</button>
      </section>
      <section id="empty"><p>글자만</p></section>`;
    expect(focusFirstWithin(document.getElementById("r"))).toBe(true);
    expect(document.activeElement?.id).toBe("link");
    expect(focusFirstWithin(document.getElementById("empty"))).toBe(false);
    expect(focusFirstWithin(null)).toBe(false);
    expect(document.activeElement?.id).toBe("link");
  });

  it("선택자는 링크·버튼·폼 컨트롤·tabindex 요소를 포함한다", () => {
    for (const part of ["a[href]", "button:not([disabled])", "input:not([disabled])", '[tabindex]:not([tabindex="-1"])']) {
      expect(FOCUSABLE_SELECTOR).toContain(part);
    }
  });
});
