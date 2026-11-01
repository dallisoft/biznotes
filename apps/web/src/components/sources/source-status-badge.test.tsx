import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Source } from "@/lib/source-types";
import { SourceStatusBadge } from "./source-panel";

const source = (status: Source["status"], error_message: string | null = null) =>
  ({ status, error_message }) as Source;

describe("SourceStatusBadge (source-panel)", () => {
  it("준비됨은 배지를 그리지 않는다", () => {
    const { container } = render(<SourceStatusBadge source={source("ready")} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("실패는 위험 톤 「실패」이고 사유를 title로 둔다", () => {
    render(<SourceStatusBadge source={source("failed", "파싱 오류")} />);
    const badge = screen.getByText("실패");
    expect(badge).toHaveAttribute("data-tone", "danger");
    expect(badge).toHaveAttribute("title", "파싱 오류");
  });

  it.each([
    ["pending", "대기 중"],
    ["parsing", "파싱 중"],
    ["chunking", "청킹 중"],
    ["embedding", "임베딩 중"],
  ] as const)("%s는 정보 톤 「%s」 + 진행 표시", (status, text) => {
    render(<SourceStatusBadge source={source(status)} />);
    const badge = screen.getByText(text);
    expect(badge).toHaveAttribute("data-tone", "info");
    expect(badge).toHaveClass("motion-safe:animate-pulse");
  });
});
