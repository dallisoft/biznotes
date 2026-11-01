import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useNotebookSelection } from "@/hooks/use-notebook-selection";
import { formatDate } from "@/lib/format";
import type { Notebook } from "@/lib/types";
import { NotebookTable } from "./notebook-table";

// AC-1.1(S-9 컬럼·순서·행 메뉴는 카드와 같은 동작) · AC-1.3(비소유 행 비활성) — 네트워크 경계(api)만 대체한다.
// ui-ux-redesign AC-3.7: 「권한」 칸은 원시값(owner·write·read) 대신 「소유자/편집 가능/읽기 전용」, 비소유 행 구분은
// 「공유받음」, 행 동작은 공용 RowActions(「{제목} 동작 더보기」 + 질문형 삭제 확인).

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

function nb(id: string, overrides: Partial<Notebook> = {}): Notebook {
  return {
    id,
    owner_id: "me",
    title: `노트북 ${id}`,
    emoji: "📓",
    description: "",
    visibility: "private",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-05T00:00:00Z",
    source_count: 0,
    is_owner: true,
    permission: "owner",
    tags: [],
    is_favorite: false,
    ...overrides,
  };
}

const NOTEBOOKS = [
  nb("b", { title: "베타", emoji: "🅱️", tags: ["research", "urgent"], source_count: 7, is_favorite: true }),
  nb("s", {
    title: "공유받음",
    owner_id: "other",
    is_owner: false,
    permission: "write",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
  }),
  nb("a", { title: "알파", permission: "owner" }),
];

function Harness({
  notebooks,
  onShare = () => {},
  onRename = () => {},
  onManageTags,
}: {
  notebooks: Notebook[];
  onShare?: (id: string) => void;
  onRename?: (n: Notebook) => void;
  onManageTags?: (n: Notebook) => void;
}) {
  const selection = useNotebookSelection(notebooks, "all");
  return (
    <>
      <NotebookTable
        notebooks={notebooks}
        selection={selection}
        onShare={onShare}
        onRename={onRename}
        onManageTags={onManageTags}
      />
      <output data-testid="count">{selection.count}</output>
    </>
  );
}

function renderTable(props: Parameters<typeof Harness>[0]) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
}

function koDate(iso: string) {
  return formatDate(iso);
}

function rows() {
  return screen.getAllByTestId("notebook-row");
}

describe("NotebookTable", () => {
  it("AC-1.1 S-9 컬럼을 이 순서로 그린다", () => {
    renderTable({ notebooks: NOTEBOOKS });
    const headers = within(screen.getByTestId("notebook-table"))
      .getAllByRole("columnheader")
      .map((th) => th.textContent?.trim());
    expect(headers).toEqual([
      "", // 선택(모두 선택 체크박스 — 이름은 aria-label)
      "제목",
      "구분",
      "권한",
      "태그",
      "소스 수",
      "생성일",
      "수정일",
      "즐겨찾기",
      "메뉴",
    ]);
    expect(screen.getByRole("checkbox", { name: "표시된 노트북 모두 선택" })).toBeInTheDocument();
  });

  it("좁은 화면(< 768) — 선택·제목 열은 왼쪽에 고정(배경·선택 행 배경)되고 나머지 열은 스크롤되며, 가로 스크롤 단서가 있다(§6.7 — P3-03)", () => {
    renderTable({ notebooks: NOTEBOOKS });
    const table = screen.getByTestId("notebook-table");
    const sticky = ["max-md:sticky", "max-md:z-10", "max-md:bg-background", "max-md:in-data-[state=selected]:bg-muted"];
    const [selectHead, titleHead, ...restHeads] = within(table).getAllByRole("columnheader");
    // 선택 열 폭(좁은 화면 1.75rem)과 제목 열 고정 위치(left 1.75rem)가 같아야 사이가 비거나 겹치지 않는다
    expect(selectHead).toHaveClass(...sticky, "max-md:left-0", "max-md:w-7", "w-10");
    expect(titleHead).toHaveClass(...sticky, "max-md:left-7");
    for (const th of restHeads) expect(th).not.toHaveClass("max-md:sticky");
    for (const row of rows()) {
      const [selectCell, titleCell, ...rest] = within(row).getAllByRole("cell");
      expect(selectCell).toHaveClass(...sticky, "max-md:left-0", "max-md:w-7");
      expect(titleCell).toHaveClass(...sticky, "max-md:left-7");
      for (const td of rest) expect(td).not.toHaveClass("max-md:sticky");
    }
    // 고정 제목 열이 좁은 화면을 다 차지하지 않게 링크 폭을 줄인다(접근 가능한 이름은 전체 제목)
    expect(screen.getByRole("link", { name: "알파" })).toHaveClass("max-md:max-w-36");
    // 가로 스크롤 단서(Table scrollCue)
    expect(table.closest("[data-slot=table-scroll]")?.querySelector("[data-slot=table-scroll-cue]")).not.toBeNull();
  });

  it("AC-1.1 받은 순서 그대로 행을 그리고 각 셀 값을 보인다", () => {
    renderTable({ notebooks: NOTEBOOKS });
    expect(rows().map((r) => within(r).getAllByRole("cell")[1].textContent)).toEqual([
      "🅱️베타",
      "📓공유받음",
      "📓알파",
    ]);

    const [beta, sharedRow, alpha] = rows().map((r) => within(r).getAllByRole("cell"));
    expect(within(beta[1]).getByRole("link")).toHaveAttribute("href", "/notebooks/b");
    expect(beta[2]).toHaveTextContent("내 노트북");
    expect(beta[3].textContent).toBe("소유자");
    expect(within(beta[4]).getByText("research")).toBeInTheDocument();
    expect(within(beta[4]).getByText("urgent")).toBeInTheDocument();
    expect(beta[5]).toHaveTextContent("7");
    expect(beta[6].textContent).toBe(koDate("2026-10-01T00:00:00Z"));
    expect(beta[7].textContent).toBe(koDate("2026-10-05T00:00:00Z"));
    expect(within(beta[8]).getByRole("button", { name: "즐겨찾기 해제" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(within(beta[9]).getByRole("button", { name: "베타 동작 더보기" })).toBeInTheDocument();

    expect(sharedRow[2].textContent).toBe("공유받음");
    expect(sharedRow[3].textContent).toBe("편집 가능");
    expect(within(sharedRow[8]).getByRole("button", { name: "즐겨찾기" })).toBeInTheDocument();
    expect(within(sharedRow[9]).queryByRole("button")).toBeNull(); // 비소유 — 소유자 동작 없음

    expect(alpha[3].textContent).toBe("소유자");
  });

  it("관리자 목록 — 남의 노트북(owner·is_owner false) 「권한」은 「관리자 권한」, 실제 소유는 「소유자」(core-v0.20.0 AC-1.9 ④)", () => {
    // 코어 v0.20.0 목록 API가 관리자에게 주는 실제 쌍: 남의 노트북 (False, owner), 실제 소유 (True, owner)
    renderTable({
      notebooks: [
        nb("pub", { title: "남의 공개", owner_id: "other", visibility: "public", is_owner: false, permission: "owner" }),
        nb("sh", { title: "남의 공유", owner_id: "other", is_owner: false, permission: "owner" }),
        nb("mine", { title: "내 것" }),
      ],
    });
    const cells = rows().map((r) => within(r).getAllByRole("cell")[3].textContent);
    expect(cells).toEqual(["관리자 권한", "관리자 권한", "소유자"]);
    expect(within(rows()[0]).getAllByRole("cell")[9].querySelector("button")).toBeNull(); // 소유자 메뉴는 여전히 is_owner만
  });

  it("AC-1.3 비소유 행 체크박스는 비활성이고, 헤더 모두 선택은 소유 행만 고른다", async () => {
    const user = userEvent.setup();
    renderTable({ notebooks: NOTEBOOKS });
    const sharedBox = screen.getByRole("checkbox", {
      name: "공유받음 선택 불가 — 소유자만 일괄 작업할 수 있습니다",
    });
    expect(sharedBox).toHaveAttribute("aria-disabled", "true");
    expect(sharedBox.parentElement).toHaveAttribute("title", "소유자만 일괄 작업할 수 있습니다");

    await user.click(screen.getByRole("checkbox", { name: "표시된 노트북 모두 선택" }));
    expect(screen.getByTestId("count")).toHaveTextContent("2");
    expect(sharedBox).toHaveAttribute("aria-checked", "false");
    expect(rows().map((r) => r.getAttribute("data-state"))).toEqual(["selected", null, "selected"]);
    expect(screen.getByRole("checkbox", { name: "표시된 노트북 모두 선택" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });

  it("AC-1.1 행 메뉴는 카드 메뉴와 같은 콜백을 부른다(이름 변경·공유·태그 관리)", async () => {
    const user = userEvent.setup();
    const onShare = vi.fn();
    const onRename = vi.fn();
    const onManageTags = vi.fn();
    renderTable({ notebooks: NOTEBOOKS, onShare, onRename, onManageTags });
    const alphaRow = rows()[2];

    await user.click(within(alphaRow).getByRole("button", { name: "알파 동작 더보기" }));
    expect((await screen.findAllByRole("menuitem")).map((m) => m.textContent)).toEqual([
      "이름 변경",
      "공유",
      "태그 관리",
      "삭제",
    ]);
    await user.click(screen.getByRole("menuitem", { name: "이름 변경" }));
    // 다이얼로그를 여는 항목 — 고르면 실행된다(포커스 규칙은 RowActions — row-actions-dialog.test.tsx)
    await waitFor(() => expect(onRename).toHaveBeenCalledWith(NOTEBOOKS[2]));

    await user.click(within(alphaRow).getByRole("button", { name: "알파 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "공유" }));
    await waitFor(() => expect(onShare).toHaveBeenCalledWith("a"));

    await user.click(within(alphaRow).getByRole("button", { name: "알파 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "태그 관리" }));
    await waitFor(() => expect(onManageTags).toHaveBeenCalledWith(NOTEBOOKS[2]));
  });

  it("AC-1.1 행 메뉴 삭제는 카드와 같은 확인을 거쳐 단건 DELETE를 보낸다", async () => {
    api.delete.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderTable({ notebooks: NOTEBOOKS });
    await user.click(within(rows()[2]).getByRole("button", { name: "알파 동작 더보기" }));
    await user.click(await screen.findByRole("menuitem", { name: "삭제" }));
    const dialog = await screen.findByRole("alertdialog", { name: "‘알파’ 노트북을 삭제할까요?" });
    expect(dialog).toHaveTextContent("소스·메모·대화가 모두 삭제되며 되돌릴 수 없습니다.");
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "취소" })).toHaveFocus()); // 기본 포커스 = 취소
    await user.click(within(dialog).getByRole("button", { name: "삭제" }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/api/v1/notebooks/a"));
    expect(api.delete).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith("노트북을 삭제했습니다", expect.anything()),
    );
  });

  it("AC-1.1 즐겨찾기 열은 카드와 같은 요청을 보낸다", async () => {
    api.delete.mockResolvedValue(undefined);
    api.post.mockResolvedValue(undefined);
    const user = userEvent.setup();
    renderTable({ notebooks: NOTEBOOKS });
    await user.click(within(rows()[0]).getByRole("button", { name: "즐겨찾기 해제" }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/api/v1/notebooks/b/favorite"));
    await user.click(within(rows()[2]).getByRole("button", { name: "즐겨찾기" }));
    await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/notebooks/a/favorite"));
  });
});
