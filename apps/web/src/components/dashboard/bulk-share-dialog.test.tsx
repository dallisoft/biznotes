import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyGroup } from "@/hooks/use-my-groups";
import { ApiError } from "@/lib/api-client";
import type { BulkTarget } from "@/lib/notebook-bulk";
import { BulkShareDialog } from "./bulk-share-dialog";

// core-v0.20.0 AC-3.3(일괄 공유 그룹 대상화 — 대상 = 그룹 하나 + 권한, 결정 시점 기준 설명 §5.3)·AC-3.4(요청 본문 정확
// 대조·DELETE 0), notebook-bulk-actions AC-3.3(e)(제출 시 캡처)·S-6(실행 중 닫힘 금지)·AC-3.8(결과)의 그룹판.
// 다이얼로그·useMyGroups·실행 훅·실행기·결정표 본문을 그대로 쓰고 네트워크 경계(api)와 토스트만 대체한다.

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

const G_ALPHA = "group-alpha";
const G_RESEARCH = "group-research";
const GROUPS: MyGroup[] = [
  { id: G_ALPHA, name: "가나 그룹" },
  { id: G_RESEARCH, name: "연구팀" },
];

type Pending = { resolve: (v: unknown) => void; reject: (e: unknown) => void };
let pending: Map<string, Pending>;
let groups: MyGroup[] | Error;

/** 요청마다 손으로 끝내는 api — 키는 「METHOD path」. `/me/groups`만 곧바로 응답한다. */
function controlled(method: "get" | "post" | "delete", path: string) {
  return new Promise((resolve, reject) => pending.set(`${method.toUpperCase()} ${path}`, { resolve, reject }));
}

beforeEach(() => {
  pending = new Map();
  groups = GROUPS;
  api.get.mockImplementation((path: string) => {
    if (path !== "/api/v1/me/groups") return controlled("get", path);
    return groups instanceof Error ? Promise.reject(groups) : Promise.resolve(groups.map((g) => ({ ...g })));
  });
  api.post.mockImplementation((path: string) => controlled("post", path));
  api.delete.mockImplementation((path: string) => controlled("delete", path));
});

afterEach(() => vi.resetAllMocks());

const targets = (n: number): BulkTarget[] =>
  Array.from({ length: n }, (_, i) => ({ id: `nb-${i + 1}`, title: `노트북 ${i + 1}` }));

function renderDialog(list: BulkTarget[] | null) {
  const onClose = vi.fn();
  const onFinished = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (node: ReactNode) => <QueryClientProvider client={client}>{node}</QueryClientProvider>;
  const view = render(wrap(<BulkShareDialog targets={list} onClose={onClose} onFinished={onFinished} />));
  const rerender = (next: BulkTarget[] | null) =>
    view.rerender(wrap(<BulkShareDialog targets={next} onClose={onClose} onFinished={onFinished} />));
  return { onClose, onFinished, rerender };
}

const flush = () => act(() => new Promise<void>((r) => setTimeout(r, 0)));
const settle = async (key: string, value: unknown, ok = true) => {
  const p = pending.get(key);
  if (!p) throw new Error(`${key} 요청 없음 — 대기 중: ${[...pending.keys()].join(", ")}`);
  pending.delete(key);
  if (ok) p.resolve(value);
  else p.reject(value);
  await flush();
};
const share = (id: string, nb: string, permission: "read" | "write", group = G_RESEARCH) => ({
  id,
  notebook_id: nb,
  user_id: null,
  group_id: group,
  permission,
});
/** 공유 관련 요청(`/me/groups` 제외) — 「METHOD path」. */
const sharingRequests = () => [
  ...api.get.mock.calls.map(([p]) => `GET ${p}`).filter((k) => !k.endsWith("/me/groups")),
  ...api.post.mock.calls.map(([p]) => `POST ${p}`),
  ...api.delete.mock.calls.map(([p]) => `DELETE ${p}`),
];

type User = ReturnType<typeof userEvent.setup>;
const groupSelect = () => screen.getByTestId("bulk-share-group-select");
const permissionSelect = () => screen.getByTestId("bulk-share-permission");
const submitButton = () => screen.getByTestId("bulk-share-submit");
async function pick(user: User, trigger: HTMLElement, name: string) {
  await user.click(trigger);
  await user.click(await screen.findByRole("option", { name }));
}

describe("BulkShareDialog — 입력·요약(core-v0.20.0 AC-3.3 — 대상 = 그룹)", () => {
  it("그룹 선택(/me/groups 이름 그대로)과 권한 선택만 있고 이메일 입력은 없다 — 제출 전 「노트북 N개 · 그룹 · 권한」 요약", async () => {
    const user = userEvent.setup();
    renderDialog(targets(12));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAttribute("data-testid", "bulk-share-dialog");
    expect(within(dialog).getByRole("heading")).toHaveTextContent("노트북 12개 공유");
    expect(within(screen.getByRole("list", { name: "공유할 노트북" })).getAllByRole("listitem")).toHaveLength(10);
    expect(dialog).toHaveTextContent("외 2개");

    expect(within(dialog).queryByRole("textbox")).toBeNull();
    expect(within(dialog).queryByLabelText("이메일")).toBeNull();
    expect(within(dialog).queryAllByRole("radio")).toHaveLength(0);
    await within(dialog).findByTestId("bulk-share-group-select");
    expect(within(dialog).getByLabelText("그룹")).toBe(groupSelect());
    expect(within(dialog).getByLabelText("권한")).toBe(permissionSelect());
    expect(groupSelect()).toHaveTextContent("그룹 선택");
    expect(permissionSelect()).toHaveTextContent("읽기 전용");
    expect(submitButton()).toHaveTextContent("노트북 12개 공유");
    expect(submitButton()).toBeDisabled(); // 그룹을 고르기 전
    expect(screen.queryByTestId("bulk-share-summary")).toBeNull();

    await user.click(groupSelect());
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["가나 그룹", "연구팀"]);
    await user.click(screen.getByRole("option", { name: "연구팀" }));
    expect(screen.getByTestId("bulk-share-summary").textContent).toBe("노트북 12개 · 연구팀 그룹 · 읽기 전용");
    await user.click(permissionSelect());
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["읽기 전용", "편집 가능"]);
    await user.click(screen.getByRole("option", { name: "편집 가능" }));
    expect(screen.getByTestId("bulk-share-summary").textContent).toBe("노트북 12개 · 연구팀 그룹 · 편집 가능");
    expect(dialog).not.toHaveTextContent(/\((read|write)\)/);
    expect(submitButton()).toBeEnabled();
    expect(api.get.mock.calls.map(([p]) => p)).toEqual(["/api/v1/me/groups"]); // /auth/me·/admin/groups 0건
    expect(sharingRequests()).toEqual([]);
  });

  it("그룹 0개면 선택 대신 안내 문구(단건과 같은 문구)이고 제출할 수 없다 — 요청 0", async () => {
    groups = [];
    renderDialog(targets(2));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByTestId("bulk-share-no-groups")).toHaveTextContent(
      /^공유할 그룹이 없습니다 — 관리자에게 그룹을 요청하세요$/,
    );
    expect(within(dialog).queryByTestId("bulk-share-group-select")).toBeNull();
    expect(submitButton()).toBeDisabled();
    expect(sharingRequests()).toEqual([]);
  });

  it("그룹 목록 조회 실패면 bulk-share-groups-error와 「다시 시도」 — 성공하면 선택이 나타난다", async () => {
    const user = userEvent.setup();
    groups = new ApiError(500, "boom-internal");
    renderDialog(targets(1));
    const dialog = await screen.findByRole("dialog");
    const error = await within(dialog).findByTestId("bulk-share-groups-error");
    expect(error).toHaveTextContent("그룹 목록을 불러오지 못했습니다");
    expect(dialog).not.toHaveTextContent("boom-internal");
    expect(submitButton()).toBeDisabled();
    groups = GROUPS;
    await user.click(within(error).getByRole("button", { name: "다시 시도" }));
    expect(await within(dialog).findByTestId("bulk-share-group-select")).toBeInTheDocument();
  });

  it("그룹 목록을 기다리는 동안은 제출할 수 없다", async () => {
    api.get.mockImplementation(() => new Promise(() => {}));
    renderDialog(targets(1));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("status")).toHaveTextContent("그룹 목록을 불러오는 중…");
    expect(submitButton()).toBeDisabled();
  });

  it("취소하면 요청이 0건이고 닫힌다", async () => {
    const user = userEvent.setup();
    const { onClose } = renderDialog(targets(2));
    await screen.findByTestId("bulk-share-group-select");
    await pick(user, groupSelect(), "연구팀");
    await user.click(screen.getByRole("button", { name: "취소" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(sharingRequests()).toEqual([]);
  });

  it("입력 단계의 X 닫기 버튼은 이름이 「닫기」 하나이고, 누르면 요청 없이 닫힌다(E-P5I-1)", async () => {
    const user = userEvent.setup();
    const { onClose } = renderDialog(targets(2));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByRole("button", { name: "닫기" })).toHaveLength(1);
    expect(within(dialog).queryByRole("button", { name: "Close" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "닫기" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(sharingRequests()).toEqual([]);
  });

  it("대상이 없으면 열리지 않고 그룹 목록도 읽지 않는다", () => {
    renderDialog(null);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it("설명은 그룹 대상·결정 시점 기준 문구이고 「권한을 낮추지 않습니다」·이메일 문구는 DOM에 없다(core-v0.20.0 §5.3)", async () => {
    renderDialog(targets(2));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAccessibleDescription(
      "선택한 그룹에 권한을 추가합니다. 이미 같거나 높은 권한으로 공유된 노트북은 건너뜁니다. 진행 중 다른 곳에서 바꾼 공유는 이 권한으로 바뀔 수 있습니다.",
    );
    expect(document.body).not.toHaveTextContent("권한을 낮추지 않습니다");
    expect(document.body).not.toHaveTextContent("이메일");
  });
});

describe("BulkShareDialog — 실행·결과(S-6, AC-3.3(e), AC-3.8, core-v0.20.0 AC-3.3·3.4)", () => {
  it("제출하면 값이 고정되고, 실행 중에는 Esc·바깥 클릭·닫기 버튼으로 닫히지 않으며, 본문은 정확히 {group_id, permission}이고 결과에 상태별 문구를 보인다", async () => {
    const user = userEvent.setup();
    const { onClose, onFinished, rerender } = renderDialog(targets(4));
    await screen.findByTestId("bulk-share-group-select");
    await pick(user, groupSelect(), "연구팀");
    await pick(user, permissionSelect(), "편집 가능");
    await user.click(submitButton());

    expect(screen.getByRole("heading", { name: "노트북 공유 중" })).toBeInTheDocument();
    expect(screen.getByTestId("bulk-progress")).toHaveTextContent("0 / 4");
    expect(screen.getByTestId("bulk-share-summary")).toHaveTextContent("노트북 4개 · 연구팀 그룹 · 편집 가능");
    expect(screen.queryByTestId("bulk-share-group-select")).toBeNull(); // 실행 중에는 입력을 바꿀 수 없다
    expect(screen.queryByRole("button", { name: "닫기" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "취소" })).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    const overlay = document.querySelector("[data-slot=dialog-overlay]");
    expect(overlay).not.toBeNull();
    await user.click(overlay as Element);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    // 부모가 다른 대상을 넘겨도 이번 실행에는 반영되지 않는다(제출 시 캡처)
    rerender([{ id: "other", title: "다른 노트북" }]);

    await settle("GET /api/v1/notebooks/nb-1/shares", []);
    await settle("GET /api/v1/notebooks/nb-2/shares", [share("old-2", "nb-2", "read")]);
    await settle("GET /api/v1/notebooks/nb-3/shares", [share("old-3", "nb-3", "write")]);
    await settle("POST /api/v1/notebooks/nb-1/share", share("s1", "nb-1", "write"));
    expect(screen.getByTestId("bulk-progress")).toHaveTextContent(/^\d \/ 4$/);
    await settle("GET /api/v1/notebooks/nb-4/shares", []);
    await settle("POST /api/v1/notebooks/nb-2/share", share("old-2", "nb-2", "write")); // 같은 행이 write로
    await settle("POST /api/v1/notebooks/nb-4/share", new TypeError("Failed to fetch"), false);

    expect(sharingRequests().some((k) => k.includes("/other/"))).toBe(false);
    expect(api.post.mock.calls.map(([p, body]) => [p, body])).toStrictEqual([
      ["/api/v1/notebooks/nb-1/share", { group_id: G_RESEARCH, permission: "write", mode: "upgrade_only" }],
      ["/api/v1/notebooks/nb-2/share", { group_id: G_RESEARCH, permission: "write", mode: "upgrade_only" }],
      ["/api/v1/notebooks/nb-4/share", { group_id: G_RESEARCH, permission: "write", mode: "upgrade_only" }],
    ]);
    expect(api.delete).not.toHaveBeenCalled();

    expect(screen.getByRole("heading", { name: "노트북 공유 결과" })).toBeInTheDocument();
    expect(screen.getByTestId("bulk-result-summary")).toHaveTextContent(
      "공유 2 · 건너뜀 1 · 실패 0 · 확인 불가 1 · 미실행 0",
    );
    const rows = screen.getAllByTestId("bulk-result-item");
    expect(rows.map((r) => [r.dataset.status, r.dataset.unconfirmed ?? null])).toEqual([
      ["succeeded", null],
      ["succeeded", null],
      ["skipped", null],
      ["failed", "true"],
    ]);
    expect(rows[0]).toHaveTextContent("공유됨");
    expect(rows[1]).toHaveTextContent("편집 가능 권한으로 변경됨");
    expect(rows[2]).toHaveTextContent("이미 편집 가능 권한으로 공유됨");
    expect(rows[3]).toHaveTextContent("확인 불가");
    expect(rows[3]).toHaveTextContent("처리 여부를 확인할 수 없습니다");
    expect(screen.queryByRole("button", { name: /되돌리기|취소/ })).not.toBeInTheDocument(); // D-09
    expect(onFinished).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith("공유 2 · 건너뜀 1 · 실패 0 · 확인 불가 1 · 미실행 0");

    // 결과 단계의 「닫기」는 푸터 버튼 하나뿐이다 — X(같은 이름)는 숨긴다(E-P5I-1)
    expect(screen.getAllByRole("button", { name: "닫기" })).toHaveLength(1);
    expect(document.querySelector("[data-slot=dialog-close]")).toBeNull();
    await user.click(screen.getByRole("button", { name: "닫기" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("중지하면 진행 중 요청까지만 끝내고 그 응답대로 기록한다 — DELETE 없음", async () => {
    const user = userEvent.setup();
    renderDialog(targets(5));
    await screen.findByTestId("bulk-share-group-select");
    await pick(user, groupSelect(), "연구팀");
    await user.click(submitButton());
    await settle("GET /api/v1/notebooks/nb-1/shares", [share("old-1", "nb-1", "read")]); // 건너뜀 → 그 줄이 nb-4를 시작
    await settle("GET /api/v1/notebooks/nb-2/shares", []);

    await user.click(screen.getByRole("button", { name: "중지" }));
    expect(screen.getByRole("button", { name: "중지하는 중…" })).toBeDisabled();
    expect(screen.getByRole("dialog")).toBeInTheDocument(); // 진행 중 요청(POST nb-2·GET nb-3·nb-4)을 기다린다
    await settle("POST /api/v1/notebooks/nb-2/share", share("s2", "nb-2", "read"));
    // 중지 뒤 GET이 끝난 항목 — POST를 보내지 않는다
    await settle("GET /api/v1/notebooks/nb-3/shares", []);
    await settle("GET /api/v1/notebooks/nb-4/shares", []);

    expect(api.delete).not.toHaveBeenCalled();
    expect(api.post).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("bulk-result-notice")).toHaveTextContent("중지했습니다");
    expect(screen.getByTestId("bulk-result-summary")).toHaveTextContent(
      "공유 1 · 건너뜀 1 · 실패 0 · 확인 불가 0 · 미실행 3",
    );
    expect(screen.queryByTestId("bulk-result-warning")).not.toBeInTheDocument();
  });

  it("그룹 404 \"Target group not found\"면 대상 중단 안내를 보이고 나머지는 미실행이다 — 서버 원문 없음", async () => {
    const user = userEvent.setup();
    renderDialog(targets(4));
    await screen.findByTestId("bulk-share-group-select");
    await pick(user, groupSelect(), "연구팀");
    await user.click(submitButton());
    await settle("GET /api/v1/notebooks/nb-1/shares", []);
    await settle("POST /api/v1/notebooks/nb-1/share", new ApiError(404, "Target group not found"), false);
    await settle("GET /api/v1/notebooks/nb-2/shares", []);
    await settle("GET /api/v1/notebooks/nb-3/shares", []);

    expect(api.post).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("bulk-result-notice")).toHaveTextContent(
      "이 그룹에는 공유할 수 없어 중단했습니다 — 소유자가 속한 그룹인지 확인한 뒤 남은 항목을 다시 실행하세요",
    );
    expect(screen.getAllByTestId("bulk-result-item").map((r) => r.dataset.status)).toEqual([
      "failed",
      "not_run",
      "not_run",
      "not_run",
    ]);
    expect(screen.getAllByTestId("bulk-result-item")[0]).toHaveTextContent("이 그룹에는 공유할 수 없습니다");
    expect(document.body).not.toHaveTextContent("Target group not found");
  });

  it("모두 공유·건너뜀이면 성공 토스트를 띄우고, 다시 열면 빈 선택·읽기 전용으로 시작한다", async () => {
    const user = userEvent.setup();
    const { onClose, rerender } = renderDialog(targets(1));
    await screen.findByTestId("bulk-share-group-select");
    await pick(user, groupSelect(), "가나 그룹");
    await pick(user, permissionSelect(), "편집 가능");
    await user.click(submitButton());
    await settle("GET /api/v1/notebooks/nb-1/shares", []);
    await settle("POST /api/v1/notebooks/nb-1/share", share("s1", "nb-1", "write", G_ALPHA));
    expect(api.post.mock.calls[0][1]).toStrictEqual({ group_id: G_ALPHA, permission: "write", mode: "upgrade_only" });
    expect(toast.success).toHaveBeenCalledWith("공유 1 · 건너뜀 0 · 실패 0 · 확인 불가 0 · 미실행 0");

    await user.keyboard("{Escape}"); // 끝난 뒤에는 닫힌다
    expect(onClose).toHaveBeenCalledTimes(1);
    rerender(null);
    rerender(targets(2));
    expect(await screen.findByRole("heading", { name: "노트북 2개 공유" })).toBeInTheDocument();
    expect(groupSelect()).toHaveTextContent("그룹 선택");
    expect(permissionSelect()).toHaveTextContent("읽기 전용");
    expect(submitButton()).toBeDisabled();
  });
});
