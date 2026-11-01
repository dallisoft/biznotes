import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mutate = vi.fn();
vi.mock("@/hooks/use-notebooks", () => ({
  useUpdateNotebook: () => ({ mutate, isPending: false }),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const { TitleSuggestionBanner } = await import(
  "@/components/notebooks/title-suggestion-banner"
);

function renderBanner(suggestions: string[]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TitleSuggestionBanner notebookId="nb1" suggestions={suggestions} />
    </QueryClientProvider>,
  );
}

describe("TitleSuggestionBanner — 노트북 제목 자동 제안(phase-22-design.md §3.3)", () => {
  beforeEach(() => {
    localStorage.clear();
    mutate.mockClear();
  });

  it("제안이 없으면 아무것도 렌더하지 않는다", () => {
    renderBanner([]);
    expect(screen.queryByTestId("title-suggestion-banner")).not.toBeInTheDocument();
  });

  it("제안 후보 각각을 버튼으로 보여준다", () => {
    renderBanner(["제목 후보 A", "제목 후보 B"]);
    expect(screen.getByRole("button", { name: "제목 후보 A" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "제목 후보 B" })).toBeInTheDocument();
  });

  it("후보를 클릭하면 해당 제목으로 업데이트를 요청한다 — 자동 변경 없음", async () => {
    const user = userEvent.setup();
    renderBanner(["제안된 제목"]);
    await user.click(screen.getByRole("button", { name: "제안된 제목" }));
    expect(mutate).toHaveBeenCalledWith(
      { id: "nb1", title: "제안된 제목" },
      expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
    );
  });

  it("닫기 버튼을 누르면 배너가 사라지고 localStorage에 기록된다", async () => {
    const user = userEvent.setup();
    renderBanner(["제목 후보"]);
    await user.click(screen.getByTestId("title-suggestion-dismiss"));
    expect(screen.queryByTestId("title-suggestion-banner")).not.toBeInTheDocument();
    expect(localStorage.getItem("nb-title-suggestion-dismissed:nb1")).toBe("1");
  });

  it("이전에 닫은 노트북은 재마운트해도 배너를 다시 보여주지 않는다", () => {
    localStorage.setItem("nb-title-suggestion-dismissed:nb1", "1");
    renderBanner(["제목 후보"]);
    expect(screen.queryByTestId("title-suggestion-banner")).not.toBeInTheDocument();
  });
});
