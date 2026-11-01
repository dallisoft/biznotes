import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusBadge, type StatusBadgeProps } from "./status-badge";

describe("StatusBadge", () => {
  it.each([
    ["neutral", ["bg-muted", "text-muted-foreground"]],
    ["info", ["bg-info/10", "text-info-text", "dark:bg-info/15"]],
    ["success", ["bg-success/10", "text-success-text", "dark:bg-success/15"]],
    ["warning", ["bg-warning/10", "text-warning-text", "dark:bg-warning/15"]],
    ["danger", ["bg-destructive/10", "text-destructive-text", "dark:bg-destructive/15"]],
  ] as const)("%s 톤은 틴트 배경 + 톤 글자 토큰을 쓰고 텍스트를 보인다", (tone, classes) => {
    render(<StatusBadge tone={tone}>상태 문구</StatusBadge>);
    const badge = screen.getByText("상태 문구");
    expect(badge).toHaveAttribute("data-tone", tone);
    expect(badge).toHaveClass(...classes);
  });

  it("기본 톤은 neutral, 아이콘은 장식으로 숨기고 텍스트는 그대로 읽힌다", () => {
    render(<StatusBadge icon={<svg data-testid="icon" />}>완료</StatusBadge>);
    const badge = screen.getByText("완료");
    expect(badge).toHaveAttribute("data-tone", "neutral");
    expect(screen.getByTestId("icon").parentElement).toHaveAttribute("aria-hidden");
    expect(badge).toHaveTextContent(/^완료$/);
  });

  it("pulse는 움직임 줄이기를 존중하는 깜박임 클래스만 쓴다", () => {
    render(<StatusBadge pulse>실행 중</StatusBadge>);
    expect(screen.getByText("실행 중")).toHaveClass("motion-safe:animate-pulse");
    expect(screen.getByText("실행 중")).not.toHaveClass("animate-pulse");
  });

  it("텍스트 없이는 렌더할 수 없다(타입) — children은 필수 문자열·숫자", () => {
    // 아래 줄들은 `pnpm typecheck`(tsc --noEmit)에서 오류여야 한다. 오류가 아니게 되면 @ts-expect-error가 실패한다.
    // @ts-expect-error children 누락
    const missing: StatusBadgeProps = { tone: "success" };
    // @ts-expect-error 요소(아이콘만)는 텍스트가 아니다
    const iconOnly: StatusBadgeProps = { tone: "danger", children: <span /> };
    // @ts-expect-error null은 텍스트가 아니다
    const nullText: StatusBadgeProps = { children: null };
    const ok: StatusBadgeProps = { tone: "info", children: 3 };
    expect([missing, iconOnly, nullText, ok]).toHaveLength(4);
    render(<StatusBadge {...ok} />);
    expect(screen.getByText("3")).toBeInTheDocument();
  });
});
