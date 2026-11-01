import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useNotebookSelection } from "@/hooks/use-notebook-selection";
import type { Notebook } from "@/lib/types";
import { NotebookCard } from "./notebook-card";

// AC-1.3(비소유 비활성·설명) · AC-1.8(체크박스 클릭·Space가 링크로 가지 않음) — 네트워크 경계(api)만 대체한다.

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

afterEach(() => vi.resetAllMocks());

function nb(overrides: Partial<Notebook> = {}): Notebook {
  return {
    id: "nb1",
    owner_id: "me",
    title: "내 노트북",
    emoji: "📓",
    description: "",
    visibility: "private",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-02T00:00:00Z",
    source_count: 2,
    is_owner: true,
    permission: "owner",
    tags: [],
    is_favorite: false,
    ...overrides,
  };
}

function Harness({ notebook }: { notebook: Notebook }) {
  const selection = useNotebookSelection([notebook], "all");
  return (
    <>
      <NotebookCard notebook={notebook} onShare={() => {}} onRename={() => {}} selection={selection} />
      <output data-testid="count">{selection.count}</output>
    </>
  );
}

function renderCard(notebook: Notebook) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness notebook={notebook} />
    </QueryClientProvider>,
  );
}

describe("NotebookCard 선택 체크박스", () => {
  it("AC-1.8 체크박스 클릭은 카드 링크에 닿지 않고 선택만 바꾼다", async () => {
    const user = userEvent.setup();
    renderCard(nb());
    const link = screen.getByRole("link", { name: "내 노트북 열기" });
    const linkClicks = vi.fn();
    link.addEventListener("click", linkClicks);
    const card = screen.getByTestId("notebook-card");
    const checkbox = screen.getByRole("checkbox", { name: "내 노트북 선택" });
    expect(link.contains(checkbox)).toBe(false); // 링크 오버레이 밖(z-10 형제)에 있다

    await user.click(checkbox);
    expect(screen.getByTestId("count")).toHaveTextContent("1");
    // 값은 "true"여야 한다 — Tailwind v4 `data-selected:`(선택 테두리·HCI-4 표시)가 `[data-selected=true]`로 컴파일된다
    expect(card).toHaveAttribute("data-selected", "true");
    expect(linkClicks).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe("/");
  });

  it("AC-1.8 키보드 Space로도 토글된다", async () => {
    const user = userEvent.setup();
    renderCard(nb());
    const checkbox = screen.getByRole("checkbox", { name: "내 노트북 선택" });
    checkbox.focus();
    expect(checkbox).toHaveFocus();
    await user.keyboard(" ");
    expect(checkbox).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("count")).toHaveTextContent("1");
    await user.keyboard(" ");
    expect(checkbox).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("count")).toHaveTextContent("0");
  });

  it("AC-1.3 비소유 카드는 체크박스가 비활성이고 설명을 접근 가능한 이름·툴팁으로 준다", async () => {
    const user = userEvent.setup();
    renderCard(nb({ owner_id: "other", is_owner: false, permission: "write" }));
    const checkbox = screen.getByRole("checkbox", {
      name: "내 노트북 선택 불가 — 소유자만 일괄 작업할 수 있습니다",
    });
    expect(checkbox).toHaveAttribute("aria-disabled", "true");
    expect(checkbox.parentElement).toHaveAttribute("title", "소유자만 일괄 작업할 수 있습니다");
    await user.click(checkbox);
    expect(checkbox).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("count")).toHaveTextContent("0");
  });

  it("AC-1.1 선택 prop이 없으면 체크박스를 그리지 않는다(기존 카드 그대로)", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <NotebookCard notebook={nb()} onShare={() => {}} onRename={() => {}} />
      </QueryClientProvider>,
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("notebook-card")).toBeInTheDocument();
  });

  it("AC-1.1 카드의 즐겨찾기는 그대로 요청한다(공유 컴포넌트로 옮긴 뒤에도 같은 경로)", async () => {
    api.post.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderCard(nb());
    await user.click(screen.getByTestId("notebook-favorite-toggle"));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb1/favorite");
    expect(screen.getByTestId("count")).toHaveTextContent("0");
  });
});
