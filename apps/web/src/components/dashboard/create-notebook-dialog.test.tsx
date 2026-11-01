import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Notebook } from "@/lib/types";
import { CreateNotebookDialog, notebookPath } from "./create-notebook-dialog";

// 새 노트북 다이얼로그 — 생성 후 이동(HCI-3, AC-3.6)·제어형 열기(빈 상태)·이모지 선택. 다이얼로그·훅 본문은 실제 코드이고
// 네트워크 경계(api)·App Router·토스트만 대체한다.

const { api, toast, router } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn() },
  router: { push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));
vi.mock("next/navigation", () => ({ useRouter: () => router }));

afterEach(() => vi.resetAllMocks());

function created(id: string, title: string): Notebook {
  return {
    id,
    owner_id: "me",
    title,
    emoji: "🔬",
    description: "",
    visibility: "private",
    created_at: "2026-10-15T00:00:00Z",
    updated_at: "2026-10-15T00:00:00Z",
    source_count: 0,
    is_owner: true,
    permission: "owner",
    tags: [],
    is_favorite: false,
  };
}

function renderWith(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe("CreateNotebookDialog", () => {
  it("notebookPath — 새 노트북 작업 화면 경로", () => {
    expect(notebookPath({ id: "abc" })).toBe("/notebooks/abc");
  });

  it("고른 이모지로 만들고 성공하면 토스트 → 닫힘 → /notebooks/{id}로 이동, 다시 열면 빈 입력이다", async () => {
    const user = userEvent.setup();
    api.post.mockResolvedValue(created("nb-9", "실험 기록"));
    renderWith(<CreateNotebookDialog />);
    await user.click(screen.getByTestId("create-notebook"));
    const dialog = await screen.findByRole("dialog", { name: "새 노트북 만들기" });
    const emojis = within(within(dialog).getByRole("group", { name: "이모지" })).getAllByRole("button");
    expect(emojis[0]).toHaveAttribute("aria-pressed", "true");
    await user.click(within(dialog).getByRole("button", { name: "🔬" }));
    expect(within(dialog).getByRole("button", { name: "🔬" })).toHaveAttribute("aria-pressed", "true");
    expect(emojis[0]).toHaveAttribute("aria-pressed", "false");
    await user.type(within(dialog).getByLabelText("제목"), "실험 기록");
    await user.click(within(dialog).getByRole("button", { name: "만들기" }));

    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/notebooks/nb-9"));
    expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks", { title: "실험 기록", emoji: "🔬" });
    expect(toast.success).toHaveBeenCalledWith("노트북을 만들었습니다", expect.anything());
    // 토스트가 이동보다 먼저다(이동 뒤에도 사용자가 결과를 본다)
    expect(toast.success.mock.invocationCallOrder[0]).toBeLessThan(router.push.mock.invocationCallOrder[0]);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    await user.click(screen.getByTestId("create-notebook"));
    const again = await screen.findByRole("dialog", { name: "새 노트북 만들기" });
    expect(within(again).getByLabelText("제목")).toHaveValue("");
    expect(within(again).getByRole("button", { name: "📓" })).toHaveAttribute("aria-pressed", "true");
  });

  it("요청 중에는 「만드는 중…」으로 비활성이고 Enter를 거듭 눌러도 요청은 한 번이다", async () => {
    const user = userEvent.setup();
    let resolve: (v: Notebook) => void = () => {};
    api.post.mockImplementation(() => new Promise<Notebook>((r) => (resolve = r)));
    renderWith(<CreateNotebookDialog />);
    await user.click(screen.getByTestId("create-notebook"));
    const dialog = await screen.findByRole("dialog");
    const input = within(dialog).getByLabelText("제목");
    await user.type(input, "한 번만{Enter}");
    await user.type(input, "{Enter}");
    expect(within(dialog).getByRole("button", { name: "만드는 중…" })).toBeDisabled();
    expect(api.post).toHaveBeenCalledTimes(1);
    resolve(created("nb-1", "한 번만"));
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/notebooks/nb-1"));
    expect(router.push).toHaveBeenCalledTimes(1);
  });

  it("제어형 — 바깥 상태로 열고(빈 상태의 「새 노트북」), Esc로 닫으면 onOpenChange(false)를 알린다", async () => {
    const user = userEvent.setup();
    const changes: boolean[] = [];
    function Host() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            바깥에서 열기
          </button>
          <CreateNotebookDialog
            open={open}
            onOpenChange={(next) => {
              changes.push(next);
              setOpen(next);
            }}
          />
        </>
      );
    }
    renderWith(<Host />);
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(screen.getByRole("button", { name: "바깥에서 열기" }));
    expect(await screen.findByRole("dialog", { name: "새 노트북 만들기" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(changes).toEqual([false]);
  });
});
