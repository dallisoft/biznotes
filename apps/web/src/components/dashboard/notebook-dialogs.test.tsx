import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Notebook } from "@/lib/types";
import { ManageTagsDialog } from "./manage-tags-dialog";
import { RenameNotebookDialog } from "./rename-notebook-dialog";

// 홈 카드·표의 소유자 메뉴(그리고 노트북 화면 헤더)가 여는 단건 다이얼로그 — 이름 변경·태그 관리.
// 다이얼로그·훅 본문은 실제 코드이고 네트워크 경계(api)·토스트만 대체한다. 공유 관리 대화상자(core-v0.20.0 P3 — 그룹
// 공유·전체 공개)의 시험은 `share-notebook-dialog.test.tsx`로 옮겼다(열린 회차·겹친 요청 알림의 공유 사례 포함).

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

afterEach(() => vi.resetAllMocks());

const NB: Notebook = {
  id: "nb1",
  owner_id: "me",
  title: "알파",
  emoji: "📓",
  description: "",
  visibility: "private",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
  source_count: 0,
  is_owner: true,
  permission: "owner",
  tags: ["research"],
  is_favorite: false,
};

function renderWith(node: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

describe("RenameNotebookDialog", () => {
  it("이름 입력에 「이름」 라벨이 있고, 저장하면 PATCH 후 표준 성공 토스트와 함께 닫힌다", async () => {
    const user = userEvent.setup();
    api.patch.mockResolvedValue({ ...NB, title: "베타" });
    const onOpenChange = vi.fn();
    renderWith(<RenameNotebookDialog notebook={NB} open onOpenChange={onOpenChange} />);
    const dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    const input = within(dialog).getByLabelText("이름");
    expect(input).toHaveValue("알파");
    await user.clear(input);
    await user.type(input, "베타");
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith("/api/v1/notebooks/nb1", { title: "베타" }));
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(toast.success).toHaveBeenCalledWith("노트북 이름을 변경했습니다", expect.anything());
  });

  it("실패하면 다이얼로그를 유지하고 표준 실패 토스트", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("@/lib/api-client");
    api.patch.mockRejectedValue(new ApiError(403, "Only the owner"));
    const onOpenChange = vi.fn();
    renderWith(<RenameNotebookDialog notebook={NB} open onOpenChange={onOpenChange} />);
    const dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("이름을 변경하지 못했습니다", expect.anything()),
    );
    expect(onOpenChange).not.toHaveBeenCalled();
  });
});

describe("ManageTagsDialog", () => {
  const TAGS = [
    { id: "t1", name: "research" },
    { id: "t2", name: "urgent" },
  ];

  it("태그는 눌림 상태(aria-pressed) 버튼이고 키보드(Enter·Space)로 붙이고 뗄 수 있다", async () => {
    const user = userEvent.setup();
    api.get.mockImplementation((path: string) =>
      path === "/api/v1/tags" ? Promise.resolve(TAGS) : Promise.reject(new Error(path)),
    );
    api.post.mockResolvedValue(undefined);
    api.delete.mockResolvedValue(undefined);
    renderWith(<ManageTagsDialog notebook={NB} open onOpenChange={() => {}} />);
    const dialog = await screen.findByRole("dialog", { name: "태그 관리" });
    const group = within(dialog).getByRole("group", { name: "태그" });
    const research = await within(group).findByRole("button", { name: "research" });
    const urgent = within(group).getByRole("button", { name: "urgent" });
    expect(research).toHaveAttribute("aria-pressed", "true");
    expect(urgent).toHaveAttribute("aria-pressed", "false");

    urgent.focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb1/tags/t2"));
    research.focus();
    await user.keyboard(" ");
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/api/v1/notebooks/nb1/tags/t1"));
  });

  it("새 태그 입력·만들기 버튼에 이름이 있고, 만들면 바로 붙인다 — 실패는 표준 실패 토스트", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("@/lib/api-client");
    api.get.mockResolvedValue([]);
    api.post.mockImplementation((path: string) =>
      path === "/api/v1/tags"
        ? Promise.resolve({ id: "t9", name: "new" })
        : Promise.reject(new ApiError(500, "boom")),
    );
    renderWith(<ManageTagsDialog notebook={NB} open onOpenChange={() => {}} />);
    const dialog = await screen.findByRole("dialog", { name: "태그 관리" });
    await user.type(within(dialog).getByLabelText("새 태그 이름"), "new");
    await user.click(within(dialog).getByRole("button", { name: "태그 만들기" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/nb1/tags/t9"));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("태그를 추가하지 못했습니다", expect.anything()),
    );
  });
});

// P3 재리뷰 P3-R01 — 폼(입력·인라인 오류)과 요청 완료 뒤의 「그 창」 후처리는 열린 회차에 묶인다. 부모의 닫기 콜백이
// 세대를 가리지 않아도(여기서는 vi.fn) 다이얼로그 스스로 이전 회차의 요청이 새 회차를 건드리지 않게 한다.
// (공유 사례는 `share-notebook-dialog.test.tsx`)
describe("단건 다이얼로그 — 열린 회차(P3-R01)", () => {
  function deferred() {
    let resolve!: (value: unknown) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }
  const client = () => new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

  it("이름 변경: 열린 동안 노트북 캐시가 바뀌어도 입력 중인 초안을 덮지 않고, 다시 열면 그때의 이름으로 시작한다", async () => {
    const user = userEvent.setup();
    const qc = client();
    const ui = (open: boolean, nb: Notebook) => (
      <QueryClientProvider client={qc}>
        <RenameNotebookDialog notebook={nb} open={open} onOpenChange={() => {}} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui(true, NB));
    let dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    const input = within(dialog).getByLabelText("이름");
    expect(input).toHaveValue("알파");
    await user.clear(input);
    await user.type(input, "입력 중인 초안");
    rerender(ui(true, { ...NB, title: "서버가 바꾼 이름" }));
    expect(within(dialog).getByLabelText("이름")).toHaveValue("입력 중인 초안");

    rerender(ui(false, { ...NB, title: "서버가 바꾼 이름" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    rerender(ui(true, { ...NB, title: "서버가 바꾼 이름" }));
    dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    expect(within(dialog).getByLabelText("이름")).toHaveValue("서버가 바꾼 이름");
  });

  it("이름 변경: 다시 연 뒤 이전 PATCH가 성공해도 토스트만 띄우고 새 창을 닫지 않는다", async () => {
    const user = userEvent.setup();
    const req = deferred();
    api.patch.mockReturnValue(req.promise);
    const onOpenChange = vi.fn();
    const qc = client();
    const ui = (open: boolean) => (
      <QueryClientProvider client={qc}>
        <RenameNotebookDialog notebook={NB} open={open} onOpenChange={onOpenChange} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui(true));
    let dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    await user.clear(within(dialog).getByLabelText("이름"));
    await user.type(within(dialog).getByLabelText("이름"), "첫 이름");
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    rerender(ui(false));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    rerender(ui(true));
    dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    expect(within(dialog).getByRole("button", { name: "저장" })).toBeEnabled();
    await user.clear(within(dialog).getByLabelText("이름"));
    await user.type(within(dialog).getByLabelText("이름"), "새 초안");

    await act(async () => req.resolve({ ...NB, title: "첫 이름" }));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("노트북 이름을 변경했습니다", expect.anything()),
    );
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    expect(within(screen.getByRole("dialog", { name: "노트북 이름 변경" })).getByLabelText("이름")).toHaveValue("새 초안");
  });
});

// P3 재리뷰 2 P3-R2-01 — 다시 연 창에서 두 번째 요청을 보내도, 먼저 보낸 요청의 성공·실패 알림은 정확히 한 번 뜨고 두 번째
// 요청의 폼·「…하는 중」·결과 처리는 그대로다(요청마다 mutateAsync의 Promise로 알림을 붙인다).
describe("단건 다이얼로그 — 겹친 요청의 알림(P3-R2-01)", () => {
  function deferred() {
    let resolve!: (value: unknown) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }
  async function overlapRename(outcome: "success" | "failure") {
    const user = userEvent.setup();
    const { ApiError } = await import("@/lib/api-client");
    const first = deferred();
    const second = deferred();
    api.patch.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    const onOpenChange = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    const ui = (open: boolean) => (
      <QueryClientProvider client={qc}>
        <RenameNotebookDialog notebook={NB} open={open} onOpenChange={onOpenChange} />
      </QueryClientProvider>
    );
    const { rerender } = render(ui(true));
    let dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    await user.clear(within(dialog).getByLabelText("이름"));
    await user.type(within(dialog).getByLabelText("이름"), "첫 이름");
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    rerender(ui(false));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    rerender(ui(true));
    dialog = await screen.findByRole("dialog", { name: "노트북 이름 변경" });
    await user.clear(within(dialog).getByLabelText("이름"));
    await user.type(within(dialog).getByLabelText("이름"), "두 번째 이름");
    await user.click(within(dialog).getByRole("button", { name: "저장" }));
    expect(api.patch).toHaveBeenCalledTimes(2);
    expect(api.patch).toHaveBeenLastCalledWith("/api/v1/notebooks/nb1", { title: "두 번째 이름" });
    expect(await within(dialog).findByRole("button", { name: "저장하는 중…" })).toBeDisabled();

    if (outcome === "success") {
      await act(async () => first.resolve({ ...NB, title: "첫 이름" }));
      await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
      expect(toast.success).toHaveBeenCalledWith("노트북 이름을 변경했습니다", expect.anything());
      expect(toast.error).not.toHaveBeenCalled();
    } else {
      await act(async () => first.reject(new ApiError(500, "boom")));
      await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
      expect(toast.error).toHaveBeenCalledWith("이름을 변경하지 못했습니다", expect.anything());
      expect(toast.success).not.toHaveBeenCalled();
    }
    expect(screen.getByRole("dialog", { name: "노트북 이름 변경" })).toBe(dialog);
    expect(within(dialog).getByLabelText("이름")).toHaveValue("두 번째 이름");
    expect(within(dialog).getByRole("button", { name: "저장하는 중…" })).toBeDisabled();
    expect(onOpenChange).not.toHaveBeenCalledWith(false);

    await act(async () => second.resolve({ ...NB, title: "두 번째 이름" }));
    await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(outcome === "success" ? 2 : 1));
    expect(toast.error).toHaveBeenCalledTimes(outcome === "success" ? 0 : 1);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  }

  it("이름 변경: 첫 요청이 두 번째 요청 대기 중에 성공해도 그 성공 알림이 한 번 뜨고, 두 번째 요청은 따로 알린다", async () => {
    await overlapRename("success");
  });

  it("이름 변경: 첫 요청이 두 번째 요청 대기 중에 500으로 실패해도 그 실패 알림이 한 번 뜨고, 두 번째 요청은 따로 알린다", async () => {
    await overlapRename("failure");
  });
});
