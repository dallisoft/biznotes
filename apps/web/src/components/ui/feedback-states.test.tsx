import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Plus } from "lucide-react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, FilteredEmptyState } from "./empty-state";
import { ErrorState } from "./error-state";
import { LoadingState } from "./loading-state";
import { StatCard } from "./stat-card";

describe("EmptyState", () => {
  it("아이콘 타일·제목·설명·주 동작 버튼을 그리고 버튼이 동작한다", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    const { container } = render(
      <EmptyState
        icon={<Plus />}
        title="아직 노트북이 없습니다"
        description="첫 노트북을 만들어 소스를 모아 보세요."
        action={{ label: "새 노트북", onClick: onCreate, icon: <Plus /> }}
      />,
    );
    expect(screen.getByText("아직 노트북이 없습니다")).toBeInTheDocument();
    expect(screen.getByText("첫 노트북을 만들어 소스를 모아 보세요.")).toBeInTheDocument();
    expect(container.querySelector("[aria-hidden].size-12")).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "새 노트북" }));
    expect(onCreate).toHaveBeenCalledTimes(1);
  });

  it("동작이 없으면 버튼을 그리지 않는다", () => {
    render(<EmptyState title="실행 기록이 없습니다" />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("필터 결과 없음은 고정 문구 + 「필터 지우기」", async () => {
    const user = userEvent.setup();
    const onClear = vi.fn();
    render(<FilteredEmptyState onClearFilters={onClear} />);
    expect(screen.getByText("조건에 맞는 항목이 없습니다")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "필터 지우기" }));
    expect(onClear).toHaveBeenCalledTimes(1);
  });
});

describe("ErrorState", () => {
  it("기본 문구 + 「다시 시도」(refetch) + 「돌아가기」 링크, alert로 알린다", async () => {
    const user = userEvent.setup();
    const refetch = vi.fn();
    render(<ErrorState onRetry={refetch} back={{ href: "/skills", label: "스킬 목록으로" }} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("목록을 불러오지 못했습니다");
    expect(alert).toHaveTextContent("잠시 후 다시 시도해 주세요.");
    await user.click(within(alert).getByRole("button", { name: "다시 시도" }));
    expect(refetch).toHaveBeenCalledTimes(1);
    expect(within(alert).getByRole("link", { name: "스킬 목록으로" })).toHaveAttribute("href", "/skills");
  });

  it("다시 시도 중에는 버튼이 비활성(aria-disabled)이라 눌러도 다시 부르지 않고, 포커스는 버튼에 남는다", async () => {
    const user = userEvent.setup();
    const refetch = vi.fn();
    const { rerender } = render(<ErrorState onRetry={refetch} />);
    const button = screen.getByRole("button", { name: "다시 시도" });
    await user.click(button);
    expect(refetch).toHaveBeenCalledTimes(1);
    rerender(<ErrorState onRetry={refetch} retrying />);
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveFocus();
    await user.click(button);
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  describe("「다시 시도」 성공으로 오류 상태가 사라질 때의 포커스(P2 통합 — admin R-4)", () => {
    function Recoverable({ onRetry }: { onRetry?: () => void }) {
      const [ok, setOk] = useState(false);
      return (
        <div>
          <button type="button">영역 밖 버튼</button>
          <section data-testid="region">
            <p>카드 머리</p>
            {ok ? (
              <a href="/admin/mcp">MCP 서버에서 보기</a>
            ) : (
              <ErrorState
                onRetry={() => {
                  onRetry?.();
                  setOk(true);
                }}
              />
            )}
          </section>
        </div>
      );
    }

    it("버튼으로 다시 시도해 성공하면 같은 영역의 첫 포커스 가능 요소로 포커스가 옮겨진다(body로 빠지지 않음)", async () => {
      const user = userEvent.setup();
      render(<Recoverable />);
      await user.click(screen.getByRole("button", { name: "다시 시도" }));
      const link = await screen.findByRole("link", { name: "MCP 서버에서 보기" });
      await waitFor(() => expect(link).toHaveFocus());
      expect(screen.queryByRole("alert")).toBeNull();
    });

    it("키보드(Enter)로 다시 시도해도 같다", async () => {
      const user = userEvent.setup();
      render(<Recoverable />);
      screen.getByRole("button", { name: "다시 시도" }).focus();
      await user.keyboard("{Enter}");
      await waitFor(() => expect(screen.getByRole("link", { name: "MCP 서버에서 보기" })).toHaveFocus());
    });

    it("다시 시도를 누르지 않고 사라지면(다른 이유로 언마운트) 포커스를 옮기지 않는다", async () => {
      const user = userEvent.setup();
      const { rerender } = render(
        <section data-testid="region">
          <ErrorState onRetry={vi.fn()} />
          <a href="/x">다른 링크</a>
        </section>,
      );
      screen.getByRole("button", { name: "다시 시도" }).focus();
      rerender(
        <section data-testid="region">
          <a href="/x">다른 링크</a>
        </section>,
      );
      await user.keyboard("{Shift}");
      expect(screen.getByRole("link", { name: "다른 링크" })).not.toHaveFocus();
    });

    it("다시 시도 직후 포커스가 이미 다른 곳으로 갔으면 빼앗지 않는다", async () => {
      const user = userEvent.setup();
      const outside = () => screen.getByRole("button", { name: "영역 밖 버튼" });
      render(<Recoverable onRetry={() => outside().focus()} />);
      await user.click(screen.getByRole("button", { name: "다시 시도" }));
      await screen.findByRole("link", { name: "MCP 서버에서 보기" });
      await user.keyboard("{Shift}");
      expect(outside()).toHaveFocus();
    });
  });

  it("권한 없음 변형은 alert가 아니고 다시 시도 대신 호출부 대안 동작을 보인다", async () => {
    const user = userEvent.setup();
    const onDuplicate = vi.fn();
    render(
      <ErrorState
        variant="forbidden"
        title="이 스킬을 편집할 권한이 없습니다"
        actions={<button onClick={onDuplicate}>복제해서 내 스킬로 편집</button>}
        back={{ href: "/skills/s1", label: "스킬 상세로" }}
      />,
    );
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("이 스킬을 편집할 권한이 없습니다")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "다시 시도" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "복제해서 내 스킬로 편집" }));
    expect(onDuplicate).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("link", { name: "스킬 상세로" })).toHaveAttribute("href", "/skills/s1");
  });
});

describe("LoadingState", () => {
  it.each([
    ["cards", 6],
    ["table", 5],
    ["form", 3],
  ] as const)("%s 프리셋은 role=status 「불러오는 중」 + 숨긴 스켈레톤 %i개", (variant, n) => {
    const { container } = render(<LoadingState variant={variant} />);
    const status = screen.getByRole("status");
    expect(status).toHaveTextContent(/^불러오는 중$/);
    expect(status).toHaveAttribute("data-variant", variant);
    expect(container.querySelectorAll("[data-testid='loading-skeleton']")).toHaveLength(n);
    expect(container.querySelector("[aria-hidden]")).toContainElement(
      container.querySelector("[data-testid='loading-skeleton']") as HTMLElement,
    );
  });

  it("카드 프리셋의 열 수는 콘텐츠 폭이 정한다(T1 — 뷰포트 브레이크포인트 아님, P3 IR-H7)", () => {
    const { container } = render(<LoadingState variant="cards" />);
    const grid = (container.querySelector("[data-testid='loading-skeleton']") as HTMLElement).parentElement!;
    expect(grid).toHaveClass("grid-cols-[repeat(auto-fill,minmax(min(260px,100%),1fr))]");
    expect(grid.className).not.toMatch(/(^|\s)(sm|md|lg|xl|2xl):grid-cols-/);
  });

  it("개수·문구를 바꿀 수 있다", () => {
    const { container } = render(<LoadingState variant="table" count={2} label="사용자를 불러오는 중" />);
    expect(screen.getByRole("status")).toHaveTextContent("사용자를 불러오는 중");
    expect(container.querySelectorAll("[data-testid='loading-skeleton']")).toHaveLength(2);
  });
});

describe("StatCard", () => {
  // 모니터링 화면에 있던 지역 정의(이동 전) — 공용 컴포넌트가 같은 DOM을 그리는지 비교한다.
  function LegacyStatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
    return (
      <Card>
        <CardHeader className="pb-2">
          <CardDescription>{label}</CardDescription>
          <CardTitle className="text-2xl tabular-nums">{value}</CardTitle>
        </CardHeader>
        {hint && <CardContent className="pt-0 text-xs text-muted-foreground">{hint}</CardContent>}
      </Card>
    );
  }

  it.each([
    [{ label: "총 실행", value: "12" }],
    [{ label: "성공률", value: "92%", hint: "성공 11 / 실패 1" }],
  ])("모니터링 지역 구현과 렌더 결과가 같다 %#", (props) => {
    const legacy = render(<LegacyStatCard {...props} />).container.innerHTML;
    const shared = render(<StatCard {...props} />).container.innerHTML;
    expect(shared).toBe(legacy);
  });

  it("valueTitle을 주면 값 요소의 title이 정확값이고, 없으면 title 속성이 없다(P1-01)", () => {
    const { unmount } = render(<StatCard label="총 비용" value="< $0.01" valueTitle="$0.004" />);
    expect(screen.getByText("< $0.01")).toHaveAttribute("title", "$0.004");
    unmount();
    render(<StatCard label="총 비용" value="$1.20" valueTitle={undefined} />);
    expect(screen.getByText("$1.20")).not.toHaveAttribute("title");
  });

  it("링크를 주면 카드 아래 텍스트 링크(카드 라벨 포함 이름)를 붙인다", () => {
    render(<StatCard label="검토 대기" value="3" href="/admin/skills" linkLabel="검토하기" />);
    expect(screen.getByRole("link", { name: "검토하기 — 검토 대기" })).toHaveAttribute(
      "href",
      "/admin/skills",
    );
  });
});
