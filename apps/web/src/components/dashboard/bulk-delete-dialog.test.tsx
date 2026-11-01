import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { BulkTarget } from "@/lib/notebook-bulk";
import { BulkDeleteDialog } from "./bulk-delete-dialog";

// AC-2.3(확인 다이얼로그)·AC-2.4(진행·닫힘 금지·중지) — 다이얼로그·실행 훅·실행기 본문을 그대로 쓰고
// 네트워크 경계(api)와 토스트만 대체한다.

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

const targets = (n: number): BulkTarget[] =>
  Array.from({ length: n }, (_, i) => ({ id: `nb-${i + 1}`, title: `노트북 ${i + 1}` }));

/** 요청마다 손으로 끝내는 DELETE. */
function controlledDeletes() {
  const pending = new Map<string, { resolve: () => void; reject: (e: unknown) => void }>();
  api.delete.mockImplementation(
    (path: string) =>
      new Promise<void>((resolve, reject) => {
        pending.set(path.replace("/api/v1/notebooks/", ""), { resolve, reject });
      }),
  );
  return {
    started: () => api.delete.mock.calls.map(([p]) => String(p).replace("/api/v1/notebooks/", "")),
    resolve: (id: string) => pending.get(id)?.resolve(),
    reject: (id: string, e: unknown) => pending.get(id)?.reject(e),
  };
}

function renderDialog(list: BulkTarget[] | null) {
  const onClose = vi.fn();
  const onFinished = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <BulkDeleteDialog targets={list} onClose={onClose} onFinished={onFinished} />
    </QueryClientProvider>,
  );
  return { onClose, onFinished };
}

const flush = () => act(() => new Promise<void>((r) => setTimeout(r, 0)));

describe("BulkDeleteDialog — 확인(AC-2.3, D-12)", () => {
  it("개수·제목 최대 10개 + 「외 k개」·비가역 경고·「노트북 N개 영구 삭제」 버튼을 보인다", () => {
    renderDialog(targets(12));
    const dialog = screen.getByRole("alertdialog");
    expect(within(dialog).getByRole("heading")).toHaveTextContent("노트북 12개를 영구 삭제할까요?");
    const items = within(screen.getByRole("list", { name: "삭제할 노트북" })).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(targets(10).map((t) => t.title));
    expect(screen.getByTestId("bulk-delete-rest")).toHaveTextContent("외 2개");
    expect(dialog).toHaveTextContent(
      "소스·메모·대화·에이전트 실행 기록·공유 설정이 모두 영구 삭제되며 되돌릴 수 없습니다",
    );
    expect(dialog).toHaveTextContent("공유받은 사용자도 더 이상 접근할 수 없습니다");
    expect(screen.getByRole("button", { name: "노트북 12개 영구 삭제" })).toBeEnabled();
  });

  it("10개 이하면 「외 k개」가 없다", () => {
    renderDialog(targets(3));
    expect(within(screen.getByRole("list", { name: "삭제할 노트북" })).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByTestId("bulk-delete-rest")).not.toBeInTheDocument();
  });

  it("취소하면 요청이 0건이고 닫힌다", async () => {
    const user = userEvent.setup();
    const { onClose, onFinished } = renderDialog(targets(2));
    await user.click(screen.getByRole("button", { name: "취소" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(api.delete).not.toHaveBeenCalled();
    expect(onFinished).not.toHaveBeenCalled();
  });

  it("대상이 없으면 열리지 않는다", () => {
    renderDialog(null);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});

describe("BulkDeleteDialog — 실행(AC-2.4·AC-2.5)", () => {
  it("실행 중에는 진행 k/N을 보이고 Esc·바깥 클릭으로 닫히지 않으며, 중지 뒤 미시작 항목은 미실행이다", async () => {
    const user = userEvent.setup();
    const d = controlledDeletes();
    const { onClose, onFinished } = renderDialog(targets(5));

    await user.click(screen.getByRole("button", { name: "노트북 5개 영구 삭제" }));
    expect(screen.getByTestId("bulk-progress")).toHaveTextContent("0 / 5");
    expect(d.started()).toEqual(["nb-1", "nb-2", "nb-3"]);
    expect(screen.queryByRole("button", { name: "취소" })).not.toBeInTheDocument();

    await user.keyboard("{Escape}");
    const overlay = document.querySelector("[data-slot=alert-dialog-overlay]");
    expect(overlay).not.toBeNull();
    await user.click(overlay as Element);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();

    d.resolve("nb-1");
    await flush();
    expect(screen.getByTestId("bulk-progress")).toHaveTextContent("1 / 5");
    expect(d.started()).toEqual(["nb-1", "nb-2", "nb-3", "nb-4"]);

    await user.click(screen.getByRole("button", { name: "중지" }));
    expect(screen.getByRole("button", { name: "중지하는 중…" })).toBeDisabled();
    d.resolve("nb-2");
    d.reject("nb-3", new TypeError("Failed to fetch"));
    await flush();
    expect(screen.getByRole("alertdialog")).toBeInTheDocument(); // nb-4 응답 대기 중
    d.resolve("nb-4");
    await flush();

    expect(d.started()).toEqual(["nb-1", "nb-2", "nb-3", "nb-4"]); // 중지 뒤 새 요청 0건
    expect(screen.getByTestId("bulk-result-summary")).toHaveTextContent(
      "삭제 3 · 실패 0 · 확인 불가 1 · 미실행 1",
    );
    expect(screen.getByTestId("bulk-result-notice")).toHaveTextContent("중지했습니다");
    const rows = screen.getAllByTestId("bulk-result-item");
    expect(rows.map((r) => [r.dataset.status, r.dataset.unconfirmed ?? null])).toEqual([
      ["succeeded", null],
      ["succeeded", null],
      ["failed", "true"],
      ["succeeded", null],
      ["not_run", null],
    ]);
    expect(rows[2]).toHaveTextContent("확인 불가");
    expect(rows[2]).toHaveTextContent("처리 여부를 확인할 수 없습니다");
    expect(onFinished).toHaveBeenCalledTimes(1);
    expect(toast.error).toHaveBeenCalledWith("삭제 3 · 실패 0 · 확인 불가 1 · 미실행 1");

    await user.click(screen.getByRole("button", { name: "닫기" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("모두 성공하면 성공 토스트를 띄우고 결과가 끝나면 Esc로 닫을 수 있다", async () => {
    const user = userEvent.setup();
    api.delete.mockResolvedValue(undefined);
    const { onClose, onFinished } = renderDialog(targets(2));
    await user.click(screen.getByRole("button", { name: "노트북 2개 영구 삭제" }));
    await flush();
    expect(toast.success).toHaveBeenCalledWith("삭제 2 · 실패 0 · 확인 불가 0 · 미실행 0");
    expect(onFinished.mock.calls[0][0].results.map((r: { status: string }) => r.status)).toEqual([
      "succeeded",
      "succeeded",
    ]);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
