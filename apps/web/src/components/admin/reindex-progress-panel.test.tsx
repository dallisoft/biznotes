import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReindexRun } from "@/lib/admin-types";
import { ReindexProgressPanel } from "./reindex-progress-panel";

const NOW = new Date("2026-10-05T00:10:00.000Z");

function run(overrides: Partial<ReindexRun> = {}): ReindexRun {
  return {
    id: "12345678-1234-1234-1234-123456789abc",
    notebook_id: null,
    failed_only: false,
    refetch: false,
    requested: 10,
    queued: 4,
    done: 5,
    failed: 1,
    created_at: "2026-10-05T00:09:00.000Z",
    finished_at: null,
    removed: 0,
    retrying: 0,
    max_attempts: 3,
    stale_only: false,
    ...overrides,
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("ReindexProgressPanel", () => {
  it("progressbar의 valuemin/max/now/valuetext와 가시 텍스트가 일치한다", () => {
    render(<ReindexProgressPanel run={run()} knownScope="all" />);
    const progress = screen.getByRole("progressbar", { name: "재인덱싱 진행률" });
    expect(progress).toHaveAttribute("aria-valuemin", "0");
    expect(progress).toHaveAttribute("aria-valuemax", "10");
    expect(progress).toHaveAttribute("aria-valuenow", "6");
    expect(progress).toHaveAttribute("aria-valuetext", "완료 6 / 대상 10");
    expect(screen.getByText("6 / 10 (60%)")).toBeVisible();
  });

  it("queued 0·failed 0이면 배지 완료, role=status 텍스트", () => {
    render(
      <ReindexProgressPanel
        run={run({ queued: 0, done: 10, failed: 0, finished_at: NOW.toISOString() })}
      />,
    );
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent("완료");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(screen.getByTestId("reindex-status")).toHaveTextContent(/^완료$/);
  });

  it("failed>0이면 완료 · 실패 있음 + destructive Badge", () => {
    render(
      <ReindexProgressPanel
        run={run({ queued: 0, done: 8, failed: 2, finished_at: NOW.toISOString() })}
      />,
    );
    const badge = screen.getByText("완료 · 실패 있음");
    // 틴트 위험 배지 글자는 대비 교정 토큰(destructive-text, plan §6.4.1)
    expect(badge).toHaveClass("text-destructive-text");
    expect(screen.getByText("실패 2")).toHaveClass("text-destructive-text");
  });

  it("집계 제외 3건 배지, 0/3 (0%), 종료 시각·경과 알 수 없음", () => {
    render(
      <ReindexProgressPanel
        run={run({ requested: 3, queued: 0, done: 0, failed: 0, finished_at: NOW.toISOString() })}
      />,
    );
    expect(screen.getAllByText(/집계 제외 3건/).length).toBeGreaterThan(0);
    expect(screen.getByText("0 / 3 (0%)")).toBeVisible();
    expect(screen.getAllByText("알 수 없음")).toHaveLength(2);
  });

  it("집계 제외 5건, 15/20 (75%), 종료(마지막 항목 갱신) 시각 라벨", () => {
    render(
      <ReindexProgressPanel
        run={run({
          requested: 20,
          queued: 0,
          done: 0,
          failed: 15,
          finished_at: NOW.toISOString(),
        })}
      />,
    );
    expect(screen.getAllByText(/집계 제외 5건/).length).toBeGreaterThan(0);
    expect(screen.getByText("15 / 20 (75%)")).toBeVisible();
    expect(screen.getByText("종료(마지막 항목 갱신) 시각")).toBeVisible();
  });

  it("활성 중 fake timer 3초 진행 시 mm:ss가 3초 증가, 종결 run은 증가 없음", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const { rerender } = render(<ReindexProgressPanel run={run()} />);
    expect(screen.getByText("01:00")).toBeVisible();
    act(() => vi.advanceTimersByTime(3_000));
    expect(screen.getByText("01:03")).toBeVisible();

    rerender(
      <ReindexProgressPanel
        run={run({ queued: 0, done: 9, finished_at: NOW.toISOString() })}
      />,
    );
    expect(screen.getByText("01:00")).toBeVisible();
    act(() => vi.advanceTimersByTime(3_000));
    expect(screen.getByText("01:00")).toBeVisible();
  });

  it("unmount 뒤 5초 진행 시 setInterval 콜백 추가 실행 0회", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const { unmount } = render(<ReindexProgressPanel run={run()} />);
    expect(vi.getTimerCount()).toBe(1);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
    act(() => vi.advanceTimersByTime(5_000));
    expect(vi.getTimerCount()).toBe(0);
  });

  it("error 전달 시 role=alert 문구와 마지막 카운트 동시 표시", () => {
    render(<ReindexProgressPanel run={run()} error={{ status: 500 }} />);
    expect(screen.getByRole("alert")).toHaveTextContent("마지막으로 확인한 카운트");
    expect(screen.getByText("완료 5")).toBeVisible();
  });

  it("3분 무변화 샘플이면 reaper 안내 문구 표시", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    render(<ReindexProgressPanel run={run()} />);
    act(() => vi.advanceTimersByTime(180_000));
    expect(screen.getByText(/15분.*60분.*3회/)).toBeVisible();
  });

  it("lastStart 전달 시 선택 후보 5 = 등록 2 + 건너뜀 3, otherActive 2면 다른 진행 중 run 2건, 100건 범위 도움말 표시", () => {
    render(
      <ReindexProgressPanel
        run={run()}
        knownScope="all"
        lastStart={{ run_id: run().id, enqueued: 2, skipped: 3 }}
        otherActive={[
          run({ id: "other-1", notebook_id: "notebook-a" }),
          run({ id: "other-2", notebook_id: "notebook-b" }),
        ]}
      />,
    );
    expect(screen.getByText(/선택 후보 5 = 등록 2 \+ 건너뜀 3/)).toBeVisible();
    expect(screen.getByText(/다른 진행 중 run 2건/)).toBeVisible();
    expect(screen.queryByText(/최근 100건/)).not.toBeInTheDocument();
  });

  it("notFound면 run을 찾을 수 없습니다 배지", () => {
    render(<ReindexProgressPanel notFound />);
    expect(screen.getByText(/run을 찾을 수 없습니다/)).toBeVisible();
  });

  it("unconfirmed면 run 상태 확인 중 배지, progressbar 없음", () => {
    render(<ReindexProgressPanel unconfirmed />);
    expect(screen.getByText("run 상태 확인 중")).toBeVisible();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("run 없이 401 오류만 있으면 조회 실패 배지와 권한 alert", () => {
    render(<ReindexProgressPanel error={{ status: 401 }} />);
    expect(screen.getByTestId("reindex-status")).toHaveTextContent("조회 실패");
    expect(screen.getByRole("alert")).toHaveTextContent("세션 또는 권한 오류 — 다시 로그인하세요");
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });

  it("run·오류·notFound가 모두 없으면 패널(테두리·testid·배지)은 없고 빈 role=status 영역만 마운트한다", () => {
    const { container } = render(<ReindexProgressPanel />);
    expect(screen.queryByTestId("reindex-progress")).not.toBeInTheDocument();
    expect(screen.queryByTestId("reindex-status")).not.toBeInTheDocument();
    expect(container.querySelector("section")).not.toHaveClass("border");
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent(/^$/);
    expect(container).toHaveTextContent(/^$/);
  });

  it("role=status 영역은 빈 상태→미확정→활성→종결 전이 내내 같은 노드로 유지되고 문구만 바뀐다", () => {
    const { rerender } = render(<ReindexProgressPanel />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/^$/);

    rerender(<ReindexProgressPanel unconfirmed />);
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("run 상태 확인 중");

    rerender(<ReindexProgressPanel run={run()} knownScope="all" />);
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("진행 중");
    expect(status).toHaveTextContent("전체");

    rerender(<ReindexProgressPanel run={run({ queued: 0, done: 9, failed: 1 })} knownScope="all" />);
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("완료 · 실패 있음");

    rerender(<ReindexProgressPanel notFound />);
    expect(screen.getByRole("status")).toBe(status);
    expect(status).toHaveTextContent("run을 찾을 수 없습니다(삭제됨)");
  });
});

describe("ReindexProgressPanel — 코어 v0.12.0 run 계약 (AC-2.1·2.4)", () => {
  it("removed·retrying 배지와 삭제로 끝난 run의 종료 시각 안내", () => {
    render(
      <ReindexProgressPanel
        run={run({
          requested: 10,
          queued: 0,
          done: 8,
          failed: 0,
          removed: 2,
          finished_at: "2026-10-05T00:09:30.000Z",
          last_update: "2026-10-05T00:09:30.000Z",
        })}
        knownScope="all"
      />,
    );
    expect(screen.getByText("삭제로 제외 2건")).toBeVisible();
    expect(screen.queryByText(/집계 제외/)).not.toBeInTheDocument();
    expect(screen.getByTestId("reindex-finished-note")).toBeVisible();
    expect(screen.getByTestId("reindex-progress-text")).toHaveTextContent("10 / 10 (100%)");
  });

  it("retrying>0이면 재시도 배지에 최대 횟수를 보인다", () => {
    render(<ReindexProgressPanel run={run({ retrying: 2, max_attempts: 3 })} knownScope="all" />);
    expect(screen.getByText("재시도 중 2건(최대 3회)")).toBeVisible();
  });
});
