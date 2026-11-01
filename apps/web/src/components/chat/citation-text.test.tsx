import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { CitationChip } from "@/components/chat/citation-text";
import { MarkdownContent } from "@/components/chat/markdown-content";
import type { Citation } from "@/lib/chat-types";
import { useSourceFocus } from "@/stores/source-focus";

// mermaid는 ESM/캔버스 의존이 무거워 단위 테스트에서 목 처리
vi.mock("@/components/chat/mermaid-diagram", () => ({
  MermaidDiagram: ({ chart }: { chart: string }) => <div data-testid="mermaid">{chart}</div>,
}));

const verified: Citation = {
  n: 1,
  source_id: "s1",
  chunk_id: "c1",
  quote: "에스프레소는 9기압으로 추출한다",
  source_title: "커피 추출 가이드",
  verified: true,
};

const unverified: Citation = { ...verified, n: 2, verified: false };

describe("CitationChip", () => {
  it("검증된 인용 — 기본 칩, 경고 없음", () => {
    render(<CitationChip n={1} citation={verified} />);
    const chip = screen.getByRole("button", { name: "1" });
    expect(chip).not.toHaveAttribute("title");
    expect(chip.className).not.toContain("warning");
  });

  it("미검증 인용 — 호박색 점선 칩 + 경고 타이틀", () => {
    render(<CitationChip n={2} citation={unverified} />);
    const chip = screen.getByRole("button", { name: "2" });
    expect(chip).toHaveAttribute("title", "출처 미확인 인용");
    expect(chip.className).toContain("border-dashed");
    // 경고 의미색 토큰(plan §6.4.1 — 하드코딩 amber 팔레트 대체)
    expect(chip.className).toContain("bg-warning/10");
    expect(chip.className).toContain("text-warning-text");
  });

  it("[Phase 18c] 클릭 시 소스 포커스를 요청한다", async () => {
    useSourceFocus.setState({ requestedSourceId: null });
    const user = userEvent.setup();
    render(<CitationChip n={1} citation={verified} />);
    await user.click(screen.getByRole("button", { name: "1" }));
    expect(useSourceFocus.getState().requestedSourceId).toBe("s1");
  });
});

describe("MarkdownContent — [n] 인용 마커", () => {
  it("[n] 마커를 인용 칩으로 치환한다", () => {
    render(
      <MarkdownContent content="추출 압력은 9기압이다 [1]." citations={[verified]} />,
    );
    expect(screen.getByRole("button", { name: "1" })).toBeInTheDocument();
    expect(screen.getByText(/추출 압력은 9기압이다/)).toBeInTheDocument();
  });

  it("매칭되는 인용이 없으면 마커를 원문 그대로 노출한다", () => {
    render(<MarkdownContent content="근거 없는 마커 [7]." citations={[verified]} />);
    expect(screen.queryByRole("button", { name: "7" })).not.toBeInTheDocument();
    expect(screen.getByText("[7]")).toBeInTheDocument();
  });

  it("코드 블록 내부의 [n]은 인용으로 취급하지 않는다", () => {
    render(
      <MarkdownContent content={"코드: `arr[1]` 참조 [1]"} citations={[verified]} />,
    );
    // 인라인 코드의 [1]은 그대로, 본문 끝 [1]만 칩
    expect(screen.getByText("arr[1]")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "1" })).toHaveLength(1);
  });

  it("mermaid 코드펜스는 다이어그램 컴포넌트로 렌더한다", () => {
    render(
      <MarkdownContent content={"```mermaid\ngraph TD; A-->B;\n```"} />,
    );
    expect(screen.getByTestId("mermaid")).toHaveTextContent("graph TD; A-->B;");
  });
});
