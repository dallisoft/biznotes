import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyGroup } from "@/hooks/use-my-groups";
import { ApiError } from "@/lib/api-client";
import { BULK_MESSAGES } from "@/lib/notebook-bulk";
import type { Notebook, NotebookShare } from "@/lib/types";
import { type ShareDialogNotebook, ShareNotebookDialog } from "./share-notebook-dialog";

// core-v0.20.0 AC-3.1(공유 관리 대화상자 §5.8 ①~④)·AC-3.2(전체 공개 토글 ⑤)·AC-3.4(요청 본문 정확 대조).
// 대화상자·훅(useMyGroups·useNotebookShares·useShareWithGroup·useRevokeNotebookShare·useUpdateNotebook)·라벨 본문은 실제
// 코드이고, 네트워크 경계(api)와 토스트만 대체한다. api는 코어 v0.20.0 공유 API를 흉내 낸 메모리 서버가 응답한다 —
// `POST /share {group_id}`는 소유자 소속 그룹이 아니면 404 "Target group not found", 같은 그룹이면 처음 행의 권한을
// 요청 값으로 바꿔(낮추기 포함) 그 행을 돌려준다(#222·#278). 첫 묶음은 실패 분기다(core-v0.17.1 회고 §5-6).

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

const NB = "nb1";
const OWNED: ShareDialogNotebook = { id: NB, visibility: "private", is_owner: true };
// id 앞 8자가 서로 다르다 — 화면은 이름·이메일 없이 앞 8자로 같은 종류의 행을 구별한다
const G_ALPHA = "a0a0a0a0-0000-4000-8000-000000000001";
const G_RESEARCH = "a1111111-0000-4000-8000-000000000002";
const G_FOREIGN_C = "cccccccc-0000-4000-8000-000000000003";
const G_FOREIGN_D = "dddddddd-0000-4000-8000-000000000004";
const GROUPS: MyGroup[] = [
  { id: G_ALPHA, name: "가나 그룹" },
  { id: G_RESEARCH, name: "연구팀" },
];

type Perm = "read" | "write";
const row = (id: string, permission: Perm, target: { group: string }, notebook = NB): NotebookShare => ({
  id,
  notebook_id: notebook,
  group_id: target.group,
  permission,
});

const sharesPath = (nb = NB) => `/api/v1/notebooks/${nb}/shares`;
const sharePath = (nb = NB) => `/api/v1/notebooks/${nb}/share`;

/** 코어처럼 동작하는 메모리 서버. `memberOf`는 POST가 받아들이는 소유자 소속 그룹(기본 = `/me/groups`가 주는 그룹). */
let server: { groups: MyGroup[]; memberOf: Set<string> | null; rows: NotebookShare[]; seq: number };

function installServer(init: { groups?: MyGroup[]; rows?: NotebookShare[] } = {}) {
  server = {
    groups: (init.groups ?? GROUPS).map((g) => ({ ...g })),
    memberOf: null,
    rows: (init.rows ?? []).map((r) => ({ ...r })),
    seq: 0,
  };
  api.get.mockImplementation(async (path: string) => {
    if (path === "/api/v1/me/groups") return server.groups.map((g) => ({ ...g }));
    const m = /^\/api\/v1\/notebooks\/([^/]+)\/shares$/.exec(path);
    if (m) return server.rows.filter((r) => r.notebook_id === m[1]).map((r) => ({ ...r }));
    throw new Error(`unexpected GET ${path}`);
  });
  api.post.mockImplementation(async (path: string, body: { group_id: string; permission: Perm }) => {
    const m = /^\/api\/v1\/notebooks\/([^/]+)\/share$/.exec(path);
    if (!m) throw new Error(`unexpected POST ${path}`);
    const member = server.memberOf ?? new Set(server.groups.map((g) => g.id));
    if (!member.has(body.group_id)) throw new ApiError(404, "Target group not found");
    const same = server.rows.find((r) => r.notebook_id === m[1] && r.group_id === body.group_id);
    if (same) {
      same.permission = body.permission;
      return { ...same };
    }
    const created = row(`share-new-${++server.seq}`, body.permission, { group: body.group_id }, m[1]);
    server.rows.push(created);
    return { ...created };
  });
  api.delete.mockImplementation(async (path: string) => {
    const id = path.split("/").pop();
    const before = server.rows.length;
    server.rows = server.rows.filter((r) => r.id !== id);
    if (server.rows.length === before) throw new ApiError(404, "Share not found");
  });
  api.patch.mockImplementation(async (path: string, body: { visibility: Notebook["visibility"] }) => ({
    ...notebookOut(path.split("/").pop() ?? NB),
    ...body,
  }));
}

function notebookOut(id: string): Notebook {
  return {
    id,
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
    tags: [],
    is_favorite: false,
  };
}

beforeEach(() => installServer());
afterEach(() => vi.resetAllMocks());

const newClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });

function renderDialog(notebook: ShareDialogNotebook | null = OWNED, client = newClient()) {
  const onOpenChange = vi.fn();
  const ui = (open: boolean, nb: ShareDialogNotebook | null) => (
    <QueryClientProvider client={client}>
      <ShareNotebookDialog notebook={nb} open={open} onOpenChange={onOpenChange} />
    </QueryClientProvider>
  );
  const view = render(ui(true, notebook));
  return {
    client,
    onOpenChange,
    rerender: (open: boolean, nb: ShareDialogNotebook | null = notebook) => view.rerender(ui(open, nb)),
  };
}

function deferred<T = unknown>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const openDialog = () => screen.findByRole("dialog", { name: "노트북 공유" });
/** 목록이 그려질 때까지 기다린다(공유·그룹 조회가 끝나야 행 표기를 정한다). */
const listReady = async (dialog: HTMLElement) => within(dialog).findByTestId("share-list");
const shareRows = () => screen.getAllByTestId("share-row");
const rowById = (id: string) => {
  const found = shareRows().find((r) => r.dataset.shareId === id);
  if (!found) throw new Error(`share row ${id} 없음`);
  return found;
};
type User = ReturnType<typeof userEvent.setup>;
async function pick(user: User, trigger: HTMLElement, name: string) {
  await user.click(trigger);
  await user.click(await screen.findByRole("option", { name }));
}
const postsTo = (nb = NB) => api.post.mock.calls.filter(([p]) => p === sharePath(nb));

// ---- AC-3.1 첫 묶음 — 실패 분기(core-v0.17.1 회고 §5-6) ----

describe("ShareNotebookDialog — 실패 분기(AC-3.1 첫 묶음)", () => {
  it("그룹 0개 — 선택·제출 대신 안내 문구만 있고, 전체 공개 토글이 보이며 기존 행은 해제할 수 있다", async () => {
    const user = userEvent.setup();
    installServer({
      groups: [],
      rows: [row("s-foreign-d", "read", { group: G_FOREIGN_D }), row("s-foreign-c", "write", { group: G_FOREIGN_C })],
    });
    renderDialog();
    const dialog = await openDialog();
    expect(await within(dialog).findByTestId("share-no-groups")).toHaveTextContent(
      /^공유할 그룹이 없습니다 — 관리자에게 그룹을 요청하세요$/,
    );
    expect(within(dialog).queryByTestId("share-group-select")).toBeNull();
    expect(within(dialog).queryByTestId("share-permission-select")).toBeNull();
    expect(within(dialog).queryByTestId("share-submit")).toBeNull();
    expect(within(dialog).getByRole("switch", { name: "전체 공개" })).toHaveAttribute("aria-checked", "false");

    await listReady(dialog);
    expect(shareRows().map((r) => r.dataset.shareKind)).toEqual(["foreign-group", "foreign-group"]);
    expect(within(dialog).queryAllByTestId("share-row-permission")).toHaveLength(0); // 해제만
    await user.click(within(rowById("s-foreign-c")).getByTestId("share-row-revoke"));
    const confirm = await screen.findByRole("alertdialog", { name: "공유를 해제할까요?" });
    await user.click(within(confirm).getByRole("button", { name: "해제" }));
    await waitFor(() => expect(shareRows().map((r) => r.dataset.shareId)).toEqual(["s-foreign-d"]));
    expect(api.delete.mock.calls).toEqual([[`${sharesPath()}/s-foreign-c`]]);
    expect(api.post).not.toHaveBeenCalled();
  });

  it("목록 조회 실패 — share-list-error와 「다시 시도」, 서버 원문 없음. 다시 시도가 성공하면 목록이 보인다", async () => {
    const user = userEvent.setup();
    installServer({ rows: [row("s-g", "read", { group: G_RESEARCH })] });
    const base = api.get.getMockImplementation();
    let fail = true;
    api.get.mockImplementation((path: string) =>
      path === sharesPath() && fail ? Promise.reject(new ApiError(500, "boom-internal")) : base?.(path),
    );
    renderDialog();
    const dialog = await openDialog();
    const error = await within(dialog).findByTestId("share-list-error");
    expect(error).toHaveTextContent("공유 목록을 불러오지 못했습니다");
    expect(within(dialog).queryByTestId("share-list")).toBeNull();
    expect(dialog).not.toHaveTextContent("boom-internal");
    // 새 공유 입력은 목록 실패와 무관하게 그대로다
    expect(within(dialog).getByTestId("share-group-select")).toBeInTheDocument();

    fail = false;
    await user.click(within(error).getByRole("button", { name: "다시 시도" }));
    await listReady(dialog);
    expect(shareRows().map((r) => [r.dataset.shareId, r.dataset.shareKind])).toEqual([["s-g", "group"]]);
    expect(within(dialog).queryByTestId("share-list-error")).toBeNull();
  });

  it("그룹 목록 조회 실패 — share-groups-error, 그룹 행은 소속을 단정하지 않고 「그룹 · id 앞 8자」로 보인다. 다시 시도하면 이름이 보인다", async () => {
    const user = userEvent.setup();
    installServer({ rows: [row("s-g", "write", { group: G_RESEARCH })] });
    const base = api.get.getMockImplementation();
    let fail = true;
    api.get.mockImplementation((path: string) =>
      path === "/api/v1/me/groups" && fail ? Promise.reject(new TypeError("Failed to fetch")) : base?.(path),
    );
    renderDialog();
    const dialog = await openDialog();
    const error = await within(dialog).findByTestId("share-groups-error");
    expect(error).toHaveTextContent("그룹 목록을 불러오지 못했습니다");
    expect(within(dialog).queryByTestId("share-group-select")).toBeNull();
    await listReady(dialog);
    const r = rowById("s-g");
    expect(r.dataset.shareKind).toBe("group");
    expect(within(r).getByTestId("share-row-target")).toHaveTextContent(/^그룹 · a1111111$/);
    expect(r).not.toHaveTextContent("소유자가 속하지 않은 그룹");

    fail = false;
    await user.click(within(error).getByRole("button", { name: "다시 시도" }));
    await within(dialog).findByTestId("share-group-select");
    await waitFor(() => expect(within(rowById("s-g")).getByTestId("share-row-target")).toHaveTextContent(/^연구팀$/));
  });

  it("비소속 그룹 행 + /me/groups 실패(PR #154 P3-04) — 소속 미확인 행에는 권한 변경 control이 없어 권한 변경 POST가 0이고, 해제는 그 행 id로 된다. 소속이 확인되면 소속 그룹 행만 권한 변경이 생긴다", async () => {
    const user = userEvent.setup();
    installServer({
      rows: [row("s-foreign-c", "write", { group: G_FOREIGN_C }), row("s-member", "read", { group: G_RESEARCH })],
    });
    const base = api.get.getMockImplementation();
    let fail = true;
    api.get.mockImplementation((path: string) =>
      path === "/api/v1/me/groups" && fail ? Promise.reject(new ApiError(500, "boom-internal")) : base?.(path),
    );
    renderDialog();
    const dialog = await openDialog();
    const error = await within(dialog).findByTestId("share-groups-error");
    await listReady(dialog);
    // 두 행 모두 소속을 모른다 — 중립 표기 「그룹 · id 앞 8자」, data-share-kind는 계약 값 그대로(group), 권한은 글자로만(해제만)
    for (const [id, short, perm] of [
      ["s-foreign-c", "cccccccc", "편집 가능"],
      ["s-member", "a1111111", "읽기 전용"],
    ] as const) {
      const r = rowById(id);
      expect(r.dataset.shareKind).toBe("group");
      expect(within(r).getByTestId("share-row-target")).toHaveTextContent(new RegExp(`^그룹 · ${short}$`));
      expect(within(r).queryByTestId("share-row-permission")).toBeNull();
      expect(within(r).getByTestId("share-row-permission-label")).toHaveTextContent(new RegExp(`^${perm}$`));
      expect(within(r).getByTestId("share-row-revoke")).toBeEnabled();
    }
    expect(within(dialog).queryAllByTestId("share-row-permission")).toHaveLength(0);
    expect(within(dialog).queryAllByRole("combobox")).toHaveLength(0); // 새 공유 입력도 그룹 실패라 없다 — 권한을 바꿀 control이 하나도 없다

    // 해제 — 확인창에 중립 표기·권한, 그 행 id로만 DELETE 1회
    await user.click(within(rowById("s-foreign-c")).getByTestId("share-row-revoke"));
    const confirm = await screen.findByRole("alertdialog", { name: "공유를 해제할까요?" });
    expect(within(confirm).getByTestId("share-revoke-target")).toHaveTextContent(/^그룹 · cccccccc · 편집 가능$/);
    await user.click(within(confirm).getByRole("button", { name: "해제" }));
    await waitFor(() => expect(shareRows().map((r) => r.dataset.shareId)).toEqual(["s-member"]));
    expect(api.delete.mock.calls).toEqual([[`${sharesPath()}/s-foreign-c`]]);
    expect(api.post).not.toHaveBeenCalled();

    // 다시 시도해 소속이 확인되면 소속 그룹(연구팀) 행만 권한을 바꿀 수 있다
    fail = false;
    await user.click(within(error).getByRole("button", { name: "다시 시도" }));
    await within(dialog).findByTestId("share-group-select");
    await waitFor(() => expect(within(rowById("s-member")).getByTestId("share-row-target")).toHaveTextContent(/^연구팀$/));
    expect(within(rowById("s-member")).getByTestId("share-row-permission")).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it("404 \"Target group not found\" — 선택 칸 아래 인라인 오류(서버 원문 없음)·토스트 없음·창 유지, 그룹 목록을 다시 읽어 빠진 그룹은 선택지에서 사라진다", async () => {
    const user = userEvent.setup();
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    const trigger = within(dialog).getByTestId("share-group-select");
    await pick(user, trigger, "연구팀");
    // 대화상자를 연 뒤 소유자가 「연구팀」에서 빠졌다 — 서버는 404, 다시 읽은 /me/groups에는 없다
    server.memberOf = new Set([G_ALPHA]);
    server.groups = [{ id: G_ALPHA, name: "가나 그룹" }];
    await user.click(within(dialog).getByTestId("share-submit"));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent(BULK_MESSAGES.targetGroupMissing);
    expect(BULK_MESSAGES.targetGroupMissing).toBe("이 그룹에는 공유할 수 없습니다 — 소유자가 속한 그룹이 아닙니다");
    expect(trigger).toHaveAttribute("aria-invalid", "true");
    expect(trigger).toHaveAttribute("aria-describedby", alert.id);
    expect(dialog).not.toHaveTextContent("Target group not found");
    expect(toast.error).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "노트북 공유" })).toBe(dialog);

    await waitFor(() =>
      expect(api.get.mock.calls.filter(([p]) => p === "/api/v1/me/groups").length).toBeGreaterThanOrEqual(2),
    );
    await user.click(trigger);
    await waitFor(async () =>
      expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["가나 그룹"]),
    );
    await user.keyboard("{Escape}");
    expect(within(dialog).getByTestId("share-submit")).toBeDisabled(); // 고른 그룹이 사라졌다
  });

  it("네트워크 오류 — 표준 실패 토스트(연결 안내), 입력은 그대로라 같은 값으로 다시 보낼 수 있다", async () => {
    const user = userEvent.setup();
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    await pick(user, within(dialog).getByTestId("share-group-select"), "연구팀");
    api.post.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await user.click(within(dialog).getByTestId("share-submit"));
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "노트북을 공유하지 못했습니다",
        expect.objectContaining({ description: expect.stringContaining("서버에 연결하지 못했습니다") }),
      ),
    );
    expect(within(dialog).queryByRole("alert")).toBeNull();
    expect(within(dialog).getByTestId("share-group-select")).toHaveTextContent("연구팀");
    expect(within(dialog).getByTestId("share-submit")).toBeEnabled();
    expect(screen.queryAllByTestId("share-row")).toHaveLength(0);

    await user.click(within(dialog).getByTestId("share-submit"));
    await waitFor(() => expect(shareRows().map((r) => r.dataset.shareKind)).toEqual(["group"]));
    expect(postsTo().map(([, body]) => body)).toEqual([
      { group_id: G_RESEARCH, permission: "read" },
      { group_id: G_RESEARCH, permission: "read" },
    ]);
  });

  it("권한 변경 실패(500) — 표준 실패 토스트, 그 행은 원래 권한(값·표시)으로 돌아간다", async () => {
    const user = userEvent.setup();
    installServer({ rows: [row("s-g", "read", { group: G_RESEARCH })] });
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    api.post.mockRejectedValueOnce(new ApiError(500, "boom"));
    await pick(user, within(rowById("s-g")).getByTestId("share-row-permission"), "편집 가능");
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("공유 권한을 변경하지 못했습니다", expect.anything()),
    );
    const r = rowById("s-g");
    await waitFor(() => expect(within(r).getByTestId("share-row-permission")).toHaveTextContent("읽기 전용"));
    expect(r).toHaveAttribute("data-permission", "read");
    expect(server.rows[0].permission).toBe("read");
    expect(dialog).not.toHaveTextContent("boom");
  });

  it("대기 중 비활성 — 공유 제출·행 권한 변경·해제 확인·전체 공개 토글은 요청이 끝날 때까지 다시 누를 수 없다", async () => {
    const user = userEvent.setup();
    installServer({
      rows: [row("s-g", "read", { group: G_RESEARCH }), row("s-foreign-d", "read", { group: G_FOREIGN_D })],
    });
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    const base = api.post.getMockImplementation();

    // ① 새 공유 제출
    const posting = deferred();
    api.post.mockImplementationOnce(() => posting.promise);
    await pick(user, within(dialog).getByTestId("share-group-select"), "가나 그룹");
    await user.click(within(dialog).getByTestId("share-submit"));
    const submit = within(dialog).getByTestId("share-submit");
    expect(submit).toBeDisabled();
    expect(submit).toHaveTextContent("공유하는 중…");
    expect(within(dialog).getByTestId("share-group-select")).toBeDisabled();
    expect(within(dialog).getByTestId("share-permission-select")).toBeDisabled();
    await user.click(submit);
    expect(postsTo()).toHaveLength(1);
    await act(async () => posting.resolve(await base?.(sharePath(), { group_id: G_ALPHA, permission: "read" })));
    await waitFor(() => expect(within(dialog).getByTestId("share-submit")).toHaveTextContent(/^공유$/));

    // ② 그룹 행 권한 변경 — 그 행의 동작만 막힌다
    const changing = deferred();
    api.post.mockImplementationOnce(() => changing.promise);
    await pick(user, within(rowById("s-g")).getByTestId("share-row-permission"), "편집 가능");
    expect(rowById("s-g")).toHaveAttribute("aria-busy", "true");
    expect(within(rowById("s-g")).getByTestId("share-row-permission")).toBeDisabled();
    expect(within(rowById("s-g")).getByTestId("share-row-revoke")).toBeDisabled();
    expect(within(rowById("s-foreign-d")).getByTestId("share-row-revoke")).toBeEnabled();
    await act(async () => changing.resolve(await base?.(sharePath(), { group_id: G_RESEARCH, permission: "write" })));
    await waitFor(() => expect(within(rowById("s-g")).getByTestId("share-row-revoke")).toBeEnabled());

    // ③ 해제 확인
    const deleting = deferred();
    api.delete.mockImplementationOnce(() => deleting.promise);
    await user.click(within(rowById("s-foreign-d")).getByTestId("share-row-revoke"));
    const confirm = await screen.findByRole("alertdialog", { name: "공유를 해제할까요?" });
    await user.click(within(confirm).getByRole("button", { name: "해제" }));
    const busy = await within(confirm).findByRole("button", { name: "해제하는 중…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    await user.click(busy);
    expect(api.delete).toHaveBeenCalledTimes(1);
    await act(async () => deleting.resolve(undefined));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());

    // ④ 전체 공개 토글
    const patching = deferred<Notebook>();
    api.patch.mockImplementationOnce(() => patching.promise);
    const toggle = within(dialog).getByRole("switch", { name: "전체 공개" });
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-disabled", "true");
    await user.click(toggle);
    expect(api.patch).toHaveBeenCalledTimes(1);
    await act(async () => patching.resolve({ ...notebookOut(NB), visibility: "public" }));
    await waitFor(() => expect(toggle).not.toHaveAttribute("aria-disabled", "true"));
  });

  it("해제 실패(500)면 확인 창이 열린 채 다시 시도할 수 있고, 이미 없는 행(404)이면 창을 닫고 목록을 다시 읽어 그 행이 사라진다", async () => {
    const user = userEvent.setup();
    installServer({
      rows: [row("s-foreign-c", "read", { group: G_FOREIGN_C }), row("s-foreign-d", "write", { group: G_FOREIGN_D })],
    });
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);

    api.delete.mockRejectedValueOnce(new ApiError(500, "boom"));
    await user.click(within(rowById("s-foreign-c")).getByTestId("share-row-revoke"));
    let confirm = await screen.findByRole("alertdialog", { name: "공유를 해제할까요?" });
    await user.click(within(confirm).getByRole("button", { name: "해제" }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("공유를 해제하지 못했습니다", expect.anything()));
    expect(await within(confirm).findByRole("button", { name: "해제" })).not.toHaveAttribute("aria-disabled");
    expect(shareRows()).toHaveLength(2);
    await user.click(within(confirm).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());

    // 다른 곳에서 이미 해제된 행 — 서버 404
    server.rows = server.rows.filter((r) => r.id !== "s-foreign-d");
    await user.click(within(rowById("s-foreign-d")).getByTestId("share-row-revoke"));
    confirm = await screen.findByRole("alertdialog", { name: "공유를 해제할까요?" });
    await user.click(within(confirm).getByRole("button", { name: "해제" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    await waitFor(() => expect(shareRows().map((r) => r.dataset.shareId)).toEqual(["s-foreign-c"]));
    expect(api.delete.mock.calls.map(([p]) => p)).toEqual([
      `${sharesPath()}/s-foreign-c`,
      `${sharesPath()}/s-foreign-d`,
    ]);
  });
});

// ---- AC-3.1 ①~④ 정상 경로 · AC-3.4 요청 본문 ----

describe("ShareNotebookDialog — 그룹 공유·권한 변경·대상 구별(AC-3.1, AC-3.4)", () => {
  it("그룹 선택지는 /me/groups의 이름 그대로(순서 유지), 권한 선택지는 「읽기 전용」·「편집 가능」이고 처음 값은 읽기 전용이다", async () => {
    const user = userEvent.setup();
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    const group = within(dialog).getByTestId("share-group-select");
    expect(group).toHaveTextContent("그룹 선택");
    expect(within(dialog).getByLabelText("그룹")).toBe(group);
    expect(within(dialog).getByTestId("share-submit")).toBeDisabled(); // 그룹을 고르기 전
    await user.click(group);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["가나 그룹", "연구팀"]);
    await user.keyboard("{Escape}");

    const permission = within(dialog).getByTestId("share-permission-select");
    expect(within(dialog).getByLabelText("권한")).toBe(permission);
    expect(permission).toHaveTextContent("읽기 전용");
    expect(permission.textContent).not.toMatch(/\b(read|write)\b/);
    await user.click(permission);
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["읽기 전용", "편집 가능"]);
    expect(api.get.mock.calls.map(([p]) => p).sort()).toEqual(["/api/v1/me/groups", sharesPath()]);
  });

  it("그룹·「편집 가능」으로 공유하면 본문이 정확히 {group_id, permission}이고, 창은 열린 채 목록에 새 행이 생기며 입력이 비워진다", async () => {
    const user = userEvent.setup();
    const { onOpenChange } = renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    expect(within(dialog).getByTestId("share-list-empty")).toHaveTextContent("아직 공유한 대상이 없습니다");
    await pick(user, within(dialog).getByTestId("share-group-select"), "연구팀");
    await pick(user, within(dialog).getByTestId("share-permission-select"), "편집 가능");
    await user.click(within(dialog).getByTestId("share-submit"));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(1));
    const [path, body] = api.post.mock.calls[0];
    expect(path).toBe(sharePath());
    expect(body).toStrictEqual({ group_id: G_RESEARCH, permission: "write" });
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("「연구팀」 그룹에 편집 가능 권한으로 공유했습니다", expect.anything()),
    );
    await waitFor(() =>
      expect(shareRows().map((r) => [r.dataset.shareKind, r.dataset.permission])).toEqual([["group", "write"]]),
    );
    expect(within(shareRows()[0]).getByTestId("share-row-target")).toHaveTextContent(/^연구팀$/);
    expect(within(dialog).queryByTestId("share-list-empty")).toBeNull();
    expect(within(dialog).getByTestId("share-group-select")).toHaveTextContent("그룹 선택");
    expect(within(dialog).getByTestId("share-permission-select")).toHaveTextContent("읽기 전용");
    expect(within(dialog).getByTestId("share-submit")).toBeDisabled();
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(api.delete).not.toHaveBeenCalled();
  });

  it("제출 버튼 위에 하향 안내 「이미 공유된 그룹이면 권한이 이 값으로 바뀝니다(낮추기 포함)」 한 줄", async () => {
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    const hint = within(dialog).getByTestId("share-downgrade-hint");
    expect(hint).toHaveTextContent(/^이미 공유된 그룹이면 권한이 이 값으로 바뀝니다\(낮추기 포함\)$/);
    expect(within(dialog).getAllByText(/이 값으로 바뀝니다/)).toHaveLength(1);
    const submit = within(dialog).getByTestId("share-submit");
    expect(hint.compareDocumentPosition(submit) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(dialog).not.toHaveTextContent("이미 공유된 사용자면");
  });

  it("그룹 행 권한 변경은 같은 본문 형식의 POST이고 같은 행 id가 돌아와 행 수가 그대로다 — 낮추기(편집 가능 → 읽기 전용)도 보낸다", async () => {
    const user = userEvent.setup();
    installServer({ rows: [row("s-g", "read", { group: G_RESEARCH }), row("s-foreign-d", "read", { group: G_FOREIGN_D })] });
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    expect(within(rowById("s-foreign-d")).queryByTestId("share-row-permission")).toBeNull();
    const trigger = () => within(rowById("s-g")).getByTestId("share-row-permission");
    expect(trigger()).toHaveAccessibleName("연구팀 권한");

    await pick(user, trigger(), "편집 가능");
    await waitFor(() => expect(rowById("s-g")).toHaveAttribute("data-permission", "write"));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("권한을 바꿨습니다 — 연구팀 · 편집 가능", expect.anything()),
    );
    await pick(user, trigger(), "읽기 전용");
    await waitFor(() => expect(rowById("s-g")).toHaveAttribute("data-permission", "read"));

    expect(postsTo().map(([, body]) => body)).toStrictEqual([
      { group_id: G_RESEARCH, permission: "write" },
      { group_id: G_RESEARCH, permission: "read" },
    ]);
    expect(shareRows().map((r) => r.dataset.shareId)).toEqual(["s-g", "s-foreign-d"]);
    expect(trigger()).toHaveTextContent("읽기 전용");
  });

  it("같은 권한을 다시 고르면 요청을 보내지 않는다", async () => {
    const user = userEvent.setup();
    installServer({ rows: [row("s-g", "write", { group: G_RESEARCH })] });
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    await pick(user, within(rowById("s-g")).getByTestId("share-row-permission"), "편집 가능");
    expect(api.post).not.toHaveBeenCalled();
  });

  it("대상 구별 — 비구성원 그룹 2행이 서로 다른 표기(id 앞 8자)·권한을 보이고, 하나를 해제하면 그 행 id로만 DELETE 1회·나머지 1행 유지", async () => {
    const user = userEvent.setup();
    installServer({
      rows: [
        row("s-foreign-c", "write", { group: G_FOREIGN_C }),
        row("s-foreign-d", "read", { group: G_FOREIGN_D }),
      ],
    });
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);

    const view = shareRows().map((r) => [
      r.dataset.shareId,
      r.dataset.shareKind,
      r.dataset.permission,
      within(r).getByTestId("share-row-target").textContent,
      within(r).getByTestId("share-row-permission-label").textContent,
    ]);
    expect(view).toEqual([
      ["s-foreign-c", "foreign-group", "write", "소유자가 속하지 않은 그룹 · cccccccc", "편집 가능"],
      ["s-foreign-d", "foreign-group", "read", "소유자가 속하지 않은 그룹 · dddddddd", "읽기 전용"],
    ]);
    expect(within(dialog).queryAllByTestId("share-row-permission")).toHaveLength(0); // 둘 다 해제만
    const revokeNames = shareRows().map((r) => within(r).getByTestId("share-row-revoke").getAttribute("aria-label"));
    expect(new Set(revokeNames).size).toBe(2);

    await user.click(within(rowById("s-foreign-c")).getByTestId("share-row-revoke"));
    const confirm = await screen.findByRole("alertdialog", { name: "공유를 해제할까요?" });
    expect(within(confirm).getByTestId("share-revoke-target")).toHaveTextContent(
      /^소유자가 속하지 않은 그룹 · cccccccc · 편집 가능$/,
    );
    expect(confirm).toHaveTextContent("해제한 뒤에는 이 화면에서 다시 공유할 수 없습니다");
    expect(confirm).not.toHaveTextContent("dddddddd");
    await user.click(within(confirm).getByRole("button", { name: "해제" }));

    await waitFor(() => expect(shareRows().map((r) => r.dataset.shareId)).toEqual(["s-foreign-d"]));
    expect(api.delete.mock.calls).toEqual([[`${sharesPath()}/s-foreign-c`]]);
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        "공유를 해제했습니다 — 소유자가 속하지 않은 그룹 · cccccccc · 편집 가능",
        expect.anything(),
      ),
    );
    expect(api.post).not.toHaveBeenCalled();
  });

  it("그룹 행의 해제 확인 문구에는 그룹 이름·권한과 구성원 안내가 있다 — 취소하면 요청 0", async () => {
    const user = userEvent.setup();
    installServer({ rows: [row("s-g", "write", { group: G_RESEARCH })] });
    renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    await user.click(within(rowById("s-g")).getByTestId("share-row-revoke"));
    const confirm = await screen.findByRole("alertdialog", { name: "공유를 해제할까요?" });
    expect(within(confirm).getByTestId("share-revoke-target")).toHaveTextContent(/^연구팀 · 편집 가능$/);
    expect(confirm).toHaveTextContent("이 그룹의 구성원은 이 공유로는 노트북을 더 볼 수 없습니다");
    await user.click(within(confirm).getByRole("button", { name: "취소" }));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(api.delete).not.toHaveBeenCalled();
    expect(shareRows()).toHaveLength(1);
  });

  it("무효화 범위 — 공유·권한 변경·해제는 그 노트북의 공유 목록만 무효화하고 노트북 목록·상세는 건드리지 않는다", async () => {
    const user = userEvent.setup();
    installServer({ rows: [row("s-foreign-d", "read", { group: G_FOREIGN_D })] });
    const client = newClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderDialog(OWNED, client);
    const dialog = await openDialog();
    await listReady(dialog);

    await pick(user, within(dialog).getByTestId("share-group-select"), "연구팀");
    await user.click(within(dialog).getByTestId("share-submit"));
    await waitFor(() => expect(shareRows()).toHaveLength(2));
    const created = shareRows()[1].dataset.shareId ?? "";
    await pick(user, within(rowById(created)).getByTestId("share-row-permission"), "편집 가능");
    await waitFor(() => expect(rowById(created)).toHaveAttribute("data-permission", "write"));
    await user.click(within(rowById("s-foreign-d")).getByTestId("share-row-revoke"));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "해제" }));
    await waitFor(() => expect(shareRows()).toHaveLength(1));

    const keys = invalidate.mock.calls.map(([filters]) => JSON.stringify(filters?.queryKey));
    expect(keys).toEqual([JSON.stringify(["notebook-shares", NB]), JSON.stringify(["notebook-shares", NB]), JSON.stringify(["notebook-shares", NB])]);
  });
});

// ---- AC-3.2 전체 공개 토글(§5.8 ⑤) ----

describe("ShareNotebookDialog — 전체 공개 토글(AC-3.2)", () => {
  const patches = () => api.patch.mock.calls.map(([p, body]) => [p, body]);

  it("비공개 → 켜면 본문이 정확히 {visibility: \"public\"}이고 곧바로 켜짐으로 보이며 성공 토스트", async () => {
    const user = userEvent.setup();
    renderDialog({ ...OWNED, visibility: "private" });
    const dialog = await openDialog();
    const toggle = within(dialog).getByRole("switch", { name: "전체 공개" });
    expect(toggle).toHaveAttribute("data-testid", "share-public-toggle");
    expect(toggle).toHaveAttribute("aria-checked", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true"); // 응답 전(낙관 표시)
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("전체 공개를 켰습니다", expect.anything()));
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(patches()).toStrictEqual([[`/api/v1/notebooks/${NB}`, { visibility: "public" }]]);
  });

  it("공개 → 끄면 {visibility: \"private\"}", async () => {
    const user = userEvent.setup();
    renderDialog({ ...OWNED, visibility: "public" });
    const toggle = within(await openDialog()).getByRole("switch", { name: "전체 공개" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    await user.click(toggle);
    await waitFor(() => expect(toast.success).toHaveBeenCalledWith("전체 공개를 껐습니다", expect.anything()));
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(patches()).toStrictEqual([[`/api/v1/notebooks/${NB}`, { visibility: "private" }]]);
  });

  it("실패하면 원래 값으로 돌아가고 표준 실패 토스트", async () => {
    const user = userEvent.setup();
    api.patch.mockRejectedValueOnce(new ApiError(403, "Only the owner can change visibility"));
    renderDialog({ ...OWNED, visibility: "private" });
    const dialog = await openDialog();
    const toggle = within(dialog).getByRole("switch", { name: "전체 공개" });
    await user.click(toggle);
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("전체 공개를 변경하지 못했습니다", expect.anything()));
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(dialog).not.toHaveTextContent("Only the owner can change visibility");
  });

  it("실제 소유자가 아니면(is_owner=false — 관리자 승격 등) 토글이 없다", async () => {
    renderDialog({ ...OWNED, is_owner: false });
    const dialog = await openDialog();
    await listReady(dialog);
    expect(within(dialog).queryByRole("switch")).toBeNull();
    expect(within(dialog).queryByTestId("share-public-toggle")).toBeNull();
  });

  it("성공하면 목록·상세 캐시의 그 노트북 visibility를 바꾸고 둘 다 무효화한다 — 공유 목록은 건드리지 않는다. 부모가 새 값을 넘기면 그 값을 따른다", async () => {
    const user = userEvent.setup();
    const client = newClient();
    client.setQueryData(["notebooks", "all"], [notebookOut(NB)]);
    client.setQueryData(["notebook", NB], notebookOut(NB));
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const { rerender } = renderDialog({ ...OWNED, visibility: "private" }, client);
    const dialog = await openDialog();
    const toggle = within(dialog).getByRole("switch", { name: "전체 공개" });
    await user.click(toggle);
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(client.getQueryData<Notebook[]>(["notebooks", "all"])?.[0].visibility).toBe("public");
    expect(client.getQueryData<Notebook>(["notebook", NB])?.visibility).toBe("public");
    const keys = invalidate.mock.calls.map(([f]) => JSON.stringify(f?.queryKey));
    expect(keys).toContain(JSON.stringify(["notebooks"]));
    expect(keys).toContain(JSON.stringify(["notebook", NB]));
    expect(keys.some((k) => k.includes("notebook-shares"))).toBe(false);

    // 부모(목록 캐시)가 다른 곳의 변경으로 private를 넘기면 그 값을 따른다
    rerender(true, { ...OWNED, visibility: "public" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    rerender(true, { ...OWNED, visibility: "private" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
  });
});

// ---- 열린 회차·겹친 요청 알림(P3 재리뷰 P3-R01·P3-R2-01 — 그룹판) ----

describe("ShareNotebookDialog — 열린 회차(P3-R01)", () => {
  it("닫고 다시 열면 빈 입력이고 「공유하는 중」도 아니다 — 이전 요청이 성공해도 토스트만 띄우고 새 입력을 지우지 않으며 창을 닫지 않는다", async () => {
    const user = userEvent.setup();
    const req = deferred();
    const base = api.post.getMockImplementation();
    api.post.mockImplementationOnce(() => req.promise);
    const { onOpenChange, rerender } = renderDialog();
    let dialog = await openDialog();
    await listReady(dialog);
    await pick(user, within(dialog).getByTestId("share-group-select"), "가나 그룹");
    await user.click(within(dialog).getByTestId("share-submit"));
    expect(within(dialog).getByTestId("share-submit")).toHaveTextContent("공유하는 중…");
    rerender(false);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    rerender(true);
    dialog = await openDialog();
    await listReady(dialog);
    expect(within(dialog).getByTestId("share-group-select")).toHaveTextContent("그룹 선택");
    expect(within(dialog).getByTestId("share-submit")).toHaveTextContent(/^공유$/);
    await pick(user, within(dialog).getByTestId("share-group-select"), "연구팀");

    await act(async () => req.resolve(await base?.(sharePath(), { group_id: G_ALPHA, permission: "read" })));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("「가나 그룹」 그룹에 읽기 전용 권한으로 공유했습니다", expect.anything()),
    );
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(within(screen.getByRole("dialog", { name: "노트북 공유" })).getByTestId("share-group-select")).toHaveTextContent(
      "연구팀",
    );
    // 서버 성공에 따른 캐시 갱신은 회차와 무관하게 한다 — 새 창 목록에 이전 요청의 행이 보인다
    await waitFor(() => expect(shareRows().map((r) => r.dataset.shareKind)).toEqual(["group"]));
  });

  it("같은 회차의 그룹 404는 선택 칸 아래, 이전 회차의 그룹 404는 토스트 — 새 창에 인라인 오류가 새지 않는다", async () => {
    const user = userEvent.setup();
    const req = deferred();
    api.post.mockImplementationOnce(() => req.promise);
    const { rerender } = renderDialog();
    let dialog = await openDialog();
    await listReady(dialog);
    await pick(user, within(dialog).getByTestId("share-group-select"), "연구팀");
    await user.click(within(dialog).getByTestId("share-submit"));
    rerender(false);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    rerender(true);
    dialog = await openDialog();
    await listReady(dialog);
    await pick(user, within(dialog).getByTestId("share-group-select"), "가나 그룹");

    await act(async () => req.reject(new ApiError(404, "Target group not found")));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("노트북을 공유하지 못했습니다", expect.anything()));
    expect(within(dialog).queryByRole("alert")).toBeNull();
    expect(within(dialog).getByTestId("share-group-select")).not.toHaveAttribute("aria-invalid");
    expect(within(dialog).getByTestId("share-group-select")).toHaveTextContent("가나 그룹");
  });

  it("열린 채 대상 노트북이 바뀌면 새 회차 — 입력과 이전 대상의 인라인 오류가 사라지고 새 노트북의 목록을 읽는다", async () => {
    const user = userEvent.setup();
    installServer({ rows: [row("s-nb2", "read", { group: G_ALPHA }, "nb2")] });
    server.memberOf = new Set([G_ALPHA]);
    const { rerender } = renderDialog();
    const dialog = await openDialog();
    await listReady(dialog);
    await pick(user, within(dialog).getByTestId("share-group-select"), "연구팀");
    await user.click(within(dialog).getByTestId("share-submit"));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(BULK_MESSAGES.targetGroupMissing);

    rerender(true, { ...OWNED, id: "nb2" });
    await waitFor(() => expect(shareRows().map((r) => r.dataset.shareId)).toEqual(["s-nb2"]));
    expect(within(dialog).queryByRole("alert")).toBeNull();
    expect(within(dialog).getByTestId("share-group-select")).toHaveTextContent("그룹 선택");
    expect(api.get.mock.calls.map(([p]) => p)).toContain(sharesPath("nb2"));
  });
});

describe("ShareNotebookDialog — 겹친 요청의 알림(P3-R2-01)", () => {
  async function overlap(outcome: "success" | "failure") {
    const user = userEvent.setup();
    const first = deferred();
    const second = deferred();
    const base = api.post.getMockImplementation();
    api.post.mockImplementationOnce(() => first.promise).mockImplementationOnce(() => second.promise);
    const { onOpenChange, rerender } = renderDialog();
    let dialog = await openDialog();
    await listReady(dialog);
    await pick(user, within(dialog).getByTestId("share-group-select"), "가나 그룹");
    await user.click(within(dialog).getByTestId("share-submit"));
    rerender(false);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    rerender(true);
    dialog = await openDialog();
    await listReady(dialog);
    await pick(user, within(dialog).getByTestId("share-group-select"), "연구팀");
    await user.click(within(dialog).getByTestId("share-submit"));
    expect(api.post).toHaveBeenCalledTimes(2);
    expect(within(dialog).getByTestId("share-submit")).toHaveTextContent("공유하는 중…");

    if (outcome === "success") {
      await act(async () => first.resolve(await base?.(sharePath(), { group_id: G_ALPHA, permission: "read" })));
      await waitFor(() => expect(toast.success).toHaveBeenCalledTimes(1));
      expect(toast.success).toHaveBeenCalledWith("「가나 그룹」 그룹에 읽기 전용 권한으로 공유했습니다", expect.anything());
      expect(toast.error).not.toHaveBeenCalled();
    } else {
      await act(async () => first.reject(new ApiError(500, "boom")));
      await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
      expect(toast.error).toHaveBeenCalledWith("노트북을 공유하지 못했습니다", expect.anything());
      expect(toast.success).not.toHaveBeenCalled();
    }
    // 두 번째 요청의 창·입력·「공유하는 중」은 그대로다
    expect(screen.getByRole("dialog", { name: "노트북 공유" })).toBe(dialog);
    expect(within(dialog).getByTestId("share-group-select")).toHaveTextContent("연구팀");
    expect(within(dialog).getByTestId("share-submit")).toHaveTextContent("공유하는 중…");
    expect(within(dialog).queryByRole("alert")).toBeNull();

    // 두 번째 요청이 성공 — 그 알림이 따로 뜨고, 창은 열린 채 목록에 새 행이 보인다(관리 대화상자)
    await act(async () => second.resolve(await base?.(sharePath(), { group_id: G_RESEARCH, permission: "read" })));
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("「연구팀」 그룹에 읽기 전용 권한으로 공유했습니다", expect.anything()),
    );
    expect(toast.success).toHaveBeenCalledTimes(outcome === "success" ? 2 : 1);
    expect(toast.error).toHaveBeenCalledTimes(outcome === "success" ? 0 : 1);
    expect(onOpenChange).not.toHaveBeenCalledWith(false);
    await waitFor(() =>
      expect(shareRows().map((r) => within(r).getByTestId("share-row-target").textContent)).toContain("연구팀"),
    );
  }

  it("첫 요청이 두 번째 요청 대기 중에 성공해도 그 성공 알림이 한 번 뜨고, 두 번째 요청은 따로 알린다", async () => {
    await overlap("success");
  });

  it("첫 요청이 두 번째 요청 대기 중에 500으로 실패해도 그 실패 알림이 한 번 뜨고, 두 번째 요청은 따로 알린다", async () => {
    await overlap("failure");
  });
});
