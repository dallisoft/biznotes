import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PageHeader } from "./page-header";

describe("PageHeader", () => {
  it("제목은 h1 하나다", () => {
    render(<PageHeader title="LLM 게이트웨이" description="프로바이더와 모델을 관리합니다" />);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("LLM 게이트웨이");
    expect(screen.getByText("프로바이더와 모델을 관리합니다")).toBeInTheDocument();
    expect(screen.queryByRole("navigation")).toBeNull();
  });

  it("브레드크럼은 nav[aria-label=위치] 안의 링크 + 현재 위치(aria-current)이고 heading이 아니다", () => {
    render(
      <PageHeader
        title="LLM 게이트웨이"
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "LLM 게이트웨이" }]}
      />,
    );
    const nav = screen.getByRole("navigation", { name: "위치" });
    expect(within(nav).getByRole("link", { name: "관리" })).toHaveAttribute("href", "/admin");
    expect(within(nav).getByText("LLM 게이트웨이")).toHaveAttribute("aria-current", "page");
    expect(within(nav).queryByRole("heading")).toBeNull();
    expect(screen.getAllByRole("heading")).toHaveLength(1);
  });

  it("기존 children은 actions 별칭으로 그대로 렌더된다(하위 호환)", () => {
    render(
      <PageHeader title="사용자 관리">
        <button>사용자 추가</button>
      </PageHeader>,
    );
    const actions = screen.getByRole("button", { name: "사용자 추가" }).parentElement!;
    expect(actions).toHaveAttribute("data-slot", "page-header-actions");
  });

  it("actions는 보조 → 주 순서로 두고, children을 함께 주면 뒤에 붙는다", () => {
    render(
      <PageHeader
        title="스킬"
        actions={
          <>
            <button>가져오기</button>
            <button>새 스킬</button>
          </>
        }
      >
        <button>도움말</button>
      </PageHeader>,
    );
    const actions = screen.getByRole("button", { name: "가져오기" }).parentElement!;
    expect(within(actions).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "가져오기",
      "새 스킬",
      "도움말",
    ]);
  });

  it("액션이 없으면 액션 영역을 그리지 않는다", () => {
    const { container } = render(<PageHeader title="실행 기록" />);
    expect(container.querySelector("[data-slot='page-header-actions']")).toBeNull();
  });
});
