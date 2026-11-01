import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AgentRun } from "@/lib/agent-types";
import { RunStatusBadge } from "./agent-panel";
import { LogStatusBadge } from "./run-detail-view";

// agent-panel → mindmap-viewer가 불러오는 @xyflow 스타일시트는 Vitest PostCSS 변환과 충돌한다 — 스타일시트만 비운다
// (mindmap-viewer.test.tsx와 같은 처리, 로직 mock 아님).
vi.mock("@xyflow/react/dist/style.css", () => ({}));

// 지역 배지 → 공용 StatusBadge 교체(plan §6.5, AC-1.7 StatusBadge): 톤 매핑과 문구를 실제 컴포넌트로 확인한다.

const run = (status: AgentRun["status"], progress = 0) => ({ status, progress }) as AgentRun;

describe("RunStatusBadge (agent-panel)", () => {
  it.each([
    ["queued", "대기", "neutral", false],
    ["running", "실행 중 40%", "info", true],
    ["awaiting_approval", "승인 대기", "warning", true],
    ["succeeded", "완료", "success", false],
    ["failed", "실패", "danger", false],
    ["cancelled", "취소됨", "neutral", false],
  ] as const)("%s → 「%s」 %s 톤 (진행 표시 %s)", (status, text, tone, pulse) => {
    render(<RunStatusBadge run={run(status, 40)} />);
    const badge = screen.getByText(text);
    expect(badge).toHaveAttribute("data-slot", "status-badge");
    expect(badge).toHaveAttribute("data-tone", tone);
    expect(badge.classList.contains("motion-safe:animate-pulse")).toBe(pulse);
  });

  it("미지 상태는 원시값을 중립 톤으로 보인다", () => {
    render(<RunStatusBadge run={run("paused" as AgentRun["status"])} />);
    expect(screen.getByText("paused")).toHaveAttribute("data-tone", "neutral");
  });
});

describe("LogStatusBadge (run-detail-view)", () => {
  // 노드 실행 로그 상태(코어 ExecutionLog: queued·running·succeeded·failed)는 실행 상태 라벨 사전으로 보인다(DP-3).
  it.each([
    ["queued", "대기", "neutral"],
    ["running", "실행 중", "neutral"],
    ["succeeded", "완료", "success"],
    ["failed", "실패", "danger"],
  ] as const)("%s → 「%s」 %s 톤", (status, text, tone) => {
    render(<LogStatusBadge status={status} />);
    const badge = screen.getByText(text);
    expect(badge).toHaveAttribute("data-slot", "status-badge");
    expect(badge).toHaveAttribute("data-tone", tone);
    expect(screen.queryByText(status)).toBeNull();
  });

  it("미지 상태는 원시값을 중립 톤으로 보인다", () => {
    render(<LogStatusBadge status="skipped" />);
    expect(screen.getByText("skipped")).toHaveAttribute("data-tone", "neutral");
  });
});
