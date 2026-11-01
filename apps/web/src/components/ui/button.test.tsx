import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Button, buttonVariants } from "./button";

// AC-1.2 — 버튼 변형이 대비 검증(theme-contrast.test.ts)이 가정하는 토큰을 실제로 쓰는지 고정한다.
// 클래스 문자열은 tailwind-merge(cn)를 거친 실제 렌더 결과로 확인한다.

const classesOf = (el: HTMLElement) => el.className.split(/\s+/);

describe("Button 변형 토큰", () => {
  it("destructive: 틴트 위에 text-destructive-text 글자를 쓴다(U8b)", () => {
    render(<Button variant="destructive">삭제</Button>);
    const classes = classesOf(screen.getByRole("button", { name: "삭제" }));
    expect(classes).toContain("text-destructive-text");
    expect(classes).not.toContain("text-destructive");
    expect(classes).toEqual(expect.arrayContaining(["bg-destructive/10", "hover:bg-destructive/20"]));
  });

  it("destructive: 포커스 표시는 불투명 border-destructive이고 기본 border-ring을 대체한다(U8c)", () => {
    render(<Button variant="destructive">삭제</Button>);
    const classes = classesOf(screen.getByRole("button", { name: "삭제" }));
    expect(classes).toContain("focus-visible:border-destructive");
    expect(classes).not.toContain("focus-visible:border-destructive/40");
    // twMerge가 같은 그룹(포커스 테두리색)의 기본값을 지워야 위험 버튼 포커스가 빨강으로 보인다
    expect(classes).not.toContain("focus-visible:border-ring");
  });

  it("default: hover는 불투명 섞기(/80) 대신 primary-hover 토큰을 쓴다(U3)", () => {
    render(<Button>저장</Button>);
    const classes = classesOf(screen.getByRole("button", { name: "저장" }));
    expect(classes).toEqual(
      expect.arrayContaining(["bg-primary", "text-primary-foreground", "hover:bg-primary-hover"]),
    );
    expect(classes).not.toContain("hover:bg-primary/80");
    // 다른 변형의 포커스 표시는 불투명 border-ring 그대로(U12)
    expect(classes).toContain("focus-visible:border-ring");
  });

  it("sm 크기는 12px 미만·임의 글자 크기를 쓰지 않는다(AC-1.4)", () => {
    const classes = buttonVariants({ size: "sm" }).split(/\s+/);
    expect(classes).toContain("text-xs");
    expect(classes.some((c) => /^text-\[/.test(c))).toBe(false);
  });

  it("어떤 변형도 text-white·text-black이나 하드코딩 팔레트를 쓰지 않는다", () => {
    const variants = ["default", "outline", "secondary", "ghost", "destructive", "link"] as const;
    for (const variant of variants) {
      const cls = buttonVariants({ variant });
      expect(cls, variant).not.toMatch(/\btext-(white|black)\b/);
      expect(cls, variant).not.toMatch(/-(red|green|emerald|amber|blue|sky|rose|orange)-\d{2,3}\b/);
    }
  });
});
