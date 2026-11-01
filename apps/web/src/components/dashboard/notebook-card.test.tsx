import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { formatDateTime } from "@/lib/format";
import type { Notebook } from "@/lib/types";
import { CARD_CONTROL_REVEAL, NotebookCard, notebookCardMeta } from "./notebook-card";

// 노트북 카드(phase-22 태그·즐겨찾기 + ui-ux-redesign P3 — plan §6.6.2 노트북 홈, HCI-4, AC-3.6).
// 카드·동작·훅 본문은 실제 코드이고 네트워크 경계(api)와 토스트만 대체한다.

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

function baseNotebook(overrides: Partial<Notebook> = {}): Notebook {
  return {
    id: "nb1",
    owner_id: "user1",
    title: "테스트 노트북",
    emoji: "📓",
    description: "",
    visibility: "private",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    source_count: 3,
    is_owner: true,
    permission: "owner",
    tags: [],
    is_favorite: false,
    ...overrides,
  };
}

function renderCard(notebook: Notebook, props: Partial<Parameters<typeof NotebookCard>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <NotebookCard notebook={notebook} onShare={() => {}} onRename={() => {}} {...props} />
    </QueryClientProvider>,
  );
}

/** 카드 동작을 감싸는 요소(체크박스 래퍼·즐겨찾기 버튼·RowActions 래퍼)의 클래스 */
const REVEAL_CLASSES = CARD_CONTROL_REVEAL.split(/\s+/);

describe("NotebookCard — 태그·즐겨찾기(phase-22-design.md §3.1/§3.2)", () => {
  it("부착된 태그를 배지로 보여준다", () => {
    renderCard(baseNotebook({ tags: ["research", "urgent"] }));
    expect(screen.getByText("research")).toBeInTheDocument();
    expect(screen.getByText("urgent")).toBeInTheDocument();
  });

  it("태그가 없으면 태그 영역을 렌더하지 않는다", () => {
    renderCard(baseNotebook({ tags: [] }));
    expect(screen.queryByTestId("notebook-tags")).not.toBeInTheDocument();
  });

  it("즐겨찾기 버튼 클릭 시 추가를 요청하고, 이미 즐겨찾기면 해제를 요청한다", async () => {
    api.post.mockResolvedValue(undefined);
    api.delete.mockResolvedValue(undefined);
    const user = userEvent.setup();
    const first = renderCard(baseNotebook({ is_favorite: false }));
    await user.click(screen.getByTestId("notebook-favorite-toggle"));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb1/favorite"));
    first.unmount();

    renderCard(baseNotebook({ is_favorite: true }));
    await user.click(screen.getByTestId("notebook-favorite-toggle"));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/api/v1/notebooks/nb1/favorite"));
  });

  it("즐겨찾기 요청이 실패하면 표준 실패 토스트를 띄운다(lib/notify)", async () => {
    const { ApiError } = await import("@/lib/api-client");
    api.post.mockRejectedValue(new ApiError(500, "boom"));
    const user = userEvent.setup();
    renderCard(baseNotebook());
    await user.click(screen.getByTestId("notebook-favorite-toggle"));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("즐겨찾기를 변경하지 못했습니다", expect.anything()),
    );
  });
});

describe("NotebookCard — 구성(plan §6.6.2 노트북 홈·§6.4.4)", () => {
  it("제목은 h2(페이지 h1 「노트북」 아래 단계)이고 카드 전체가 「{제목} 열기」 링크다", () => {
    renderCard(baseNotebook());
    expect(screen.getByRole("heading", { level: 2, name: "테스트 노트북" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "테스트 노트북 열기" })).toHaveAttribute("href", "/notebooks/nb1");
  });

  it("메타 줄은 본문 안 「소스 N개 · {상대 시간} 수정」이고 회색 푸터 띠가 없다", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-15T09:00:00+09:00"));
    renderCard(baseNotebook({ source_count: 1, updated_at: "2026-10-12T09:00:00+09:00" }));
    const meta = screen.getByTestId("notebook-meta");
    expect(meta.textContent).toBe("소스 1개 · 3일 전 수정");
    const time = within(meta).getByText("3일 전");
    expect(time.tagName).toBe("TIME");
    expect(time).toHaveAttribute("dateTime", "2026-10-12T09:00:00+09:00");
    expect(time).toHaveAttribute("title", formatDateTime("2026-10-12T09:00:00+09:00"));
    expect(document.querySelector("[data-slot=card-footer]")).toBeNull();
  });

  it("notebookCardMeta — 7일 이상 지난 수정일은 날짜, 큰 소스 수는 천 단위 쉼표", () => {
    const now = new Date("2026-10-15T09:00:00+09:00");
    expect(notebookCardMeta({ source_count: 1234, updated_at: "2026-09-12T09:00:00+09:00" }, now)).toEqual({
      sources: "소스 1,234개",
      updatedAt: "2026. 9. 12.",
    });
    expect(notebookCardMeta({ source_count: 0, updated_at: "2026-10-15T08:59:30+09:00" }, now).updatedAt).toBe(
      "방금 전",
    );
  });

  it("hover는 그림자+primary 테두리이고 튀어 오르는 이동(translate)이 없다", () => {
    renderCard(baseNotebook());
    const card = screen.getByTestId("notebook-card");
    expect(card).toHaveClass("hover:shadow-sm", "hover:ring-primary/30");
    expect(card.className).not.toMatch(/translate/);
  });

  it("비소유 카드는 「공유받음」 배지가 있고 소유자 동작(더보기)이 없다", () => {
    renderCard(baseNotebook({ is_owner: false, permission: "read" }), { onManageTags: () => {} });
    expect(screen.getByText("공유받음")).toHaveAttribute("data-slot", "status-badge");
    expect(screen.queryByRole("button", { name: /동작 더보기/ })).not.toBeInTheDocument();
  });

  it("소유 카드는 「공유받음」 배지가 없다", () => {
    renderCard(baseNotebook());
    expect(screen.queryByText("공유받음")).not.toBeInTheDocument();
  });
});

describe("NotebookCard — 동작 표시 규칙(HCI-4, AC-3.6)", () => {
  function Harness({ notebook }: { notebook: Notebook }) {
    // 선택 prop을 주는 카드(대시보드) — 선택 상태는 테스트가 정한다
    const selection = {
      selectedIds: new Set<string>(),
      count: 0,
      selectableCount: 1,
      selectAllState: "unchecked" as const,
      isSelected: () => false,
      setSelected: () => {},
      toggleAll: () => {},
      deselect: () => {},
      clear: () => {},
    };
    return <NotebookCard notebook={notebook} onShare={() => {}} onRename={() => {}} selection={selection} />;
  }

  function renderHarness(notebook: Notebook) {
    return render(
      <QueryClientProvider client={new QueryClient()}>
        <Harness notebook={notebook} />
      </QueryClientProvider>,
    );
  }

  it("마우스 기기(pointer-fine)에서만 숨기고, 카드 hover·포커스(focus-within)·선택 중이면 보인다 — 터치 기기는 숨기지 않는다", () => {
    expect(REVEAL_CLASSES).toEqual(
      expect.arrayContaining([
        "pointer-fine:opacity-0",
        "pointer-fine:group-hover/notebook:opacity-100",
        "pointer-fine:group-focus-within/notebook:opacity-100",
        "pointer-fine:group-data-selected/notebook:opacity-100",
      ]),
    );
    // 숨김 규칙은 pointer-fine 조건 안에만 있다 — 조건 없는 opacity-0·invisible·hidden은 없다(coarse에서 항상 보임)
    expect(REVEAL_CLASSES.filter((c) => /(^|:)(opacity-0|invisible|hidden)$/.test(c) && !c.startsWith("pointer-fine:"))).toEqual([]);

    renderHarness(baseNotebook());
    const card = screen.getByTestId("notebook-card");
    expect(card).toHaveClass("group/notebook");
    const checkboxWrap = screen.getByRole("checkbox", { name: "테스트 노트북 선택" }).closest("[data-slot=notebook-select]");
    const favorite = screen.getByTestId("notebook-favorite-toggle");
    const moreWrap = screen.getByRole("button", { name: "테스트 노트북 동작 더보기" }).parentElement;
    for (const el of [checkboxWrap, favorite, moreWrap]) expect(el).toHaveClass(...REVEAL_CLASSES);
    // 메뉴가 열린 동안에는 트리거가 보인다
    expect(moreWrap).toHaveClass("pointer-fine:has-data-popup-open:opacity-100");
  });

  it("숨겨진 동작도 키보드 Tab 순서에 남는다(opacity만 — 링크 → 체크박스 → 즐겨찾기 → 더보기)", async () => {
    const user = userEvent.setup();
    renderHarness(baseNotebook());
    await user.tab();
    expect(screen.getByRole("link", { name: "테스트 노트북 열기" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("checkbox", { name: "테스트 노트북 선택" })).toHaveFocus();
    await user.tab();
    expect(screen.getByTestId("notebook-favorite-toggle")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "테스트 노트북 동작 더보기" })).toHaveFocus();
  });

  it("즐겨찾기한 카드의 채운 별은 상태 표시라 항상 보인다", () => {
    renderHarness(baseNotebook({ is_favorite: true }));
    const favorite = screen.getByTestId("notebook-favorite-toggle");
    expect(favorite).not.toHaveClass("pointer-fine:opacity-0");
    expect(favorite).toHaveAttribute("aria-pressed", "true");
  });

  it("터치 기기에서 즐겨찾기·더보기·체크박스 히트 영역이 44px이다(coarse)", () => {
    renderHarness(baseNotebook());
    expect(screen.getByTestId("notebook-favorite-toggle")).toHaveClass("coarse:size-11");
    expect(screen.getByRole("button", { name: "테스트 노트북 동작 더보기" })).toHaveClass("coarse:size-11");
    expect(screen.getByRole("checkbox", { name: "테스트 노트북 선택" })).toHaveClass(
      "coarse:after:-inset-x-3.5",
      "coarse:after:-inset-y-3.5",
    );
  });
});

describe("NotebookCard — 소유자 동작 더보기(RowActions)", () => {
  it("「{제목} 동작 더보기」 메뉴는 이름 변경·공유·태그 관리 / 삭제 순이고 같은 콜백을 부른다", async () => {
    const user = userEvent.setup();
    const onRename = vi.fn();
    const onShare = vi.fn();
    const onManageTags = vi.fn();
    const nb = baseNotebook();
    renderCard(nb, { onRename, onShare, onManageTags });
    await user.click(screen.getByRole("button", { name: "테스트 노트북 동작 더보기" }));
    expect((await screen.findAllByRole("menuitem")).map((m) => m.textContent)).toEqual([
      "이름 변경",
      "공유",
      "태그 관리",
      "삭제",
    ]);
    expect(screen.getByRole("menu").querySelector("[role=separator]")).not.toBeNull();
    await user.click(screen.getByRole("menuitem", { name: "공유" }));
    // 다이얼로그를 여는 항목 — 고르면 실행된다(포커스 규칙은 RowActions — row-actions-dialog.test.tsx)
    await waitFor(() => expect(onShare).toHaveBeenCalledWith("nb1"));
    expect(onRename).not.toHaveBeenCalled();
    expect(onManageTags).not.toHaveBeenCalled();
  });

  it("삭제는 질문형 확인을 거치고, 취소하면 요청 0건이고 포커스가 더보기로 돌아온다", async () => {
    const user = userEvent.setup();
    renderCard(baseNotebook());
    const more = screen.getByRole("button", { name: "테스트 노트북 동작 더보기" });
    await user.click(more);
    await user.click(await screen.findByRole("menuitem", { name: "삭제" }));
    const dialog = await screen.findByRole("alertdialog", { name: "‘테스트 노트북’ 노트북을 삭제할까요?" });
    await user.click(within(dialog).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(more).toHaveFocus());
    expect(api.delete).not.toHaveBeenCalled();
  });

  it("삭제 실패(500)면 표준 실패 토스트를 띄우고 카드는 남는다", async () => {
    const { ApiError } = await import("@/lib/api-client");
    api.delete.mockRejectedValue(new ApiError(500, "boom"));
    const user = userEvent.setup();
    renderCard(baseNotebook());
    await user.click(screen.getByRole("button", { name: "테스트 노트북 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "삭제" }));
    const dialog = await screen.findByRole("alertdialog");
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("노트북을 삭제하지 못했습니다", expect.anything()),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(screen.getByTestId("notebook-card")).toBeInTheDocument();
  });
});
