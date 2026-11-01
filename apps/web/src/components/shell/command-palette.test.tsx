import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "@/components/shell/command-palette";
import type { SearchResults } from "@/hooks/use-global-search";
import { useCommandPalette } from "@/stores/command-palette";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push }),
}));

const searchState: {
  query: string;
  results: { data: SearchResults | undefined; isPending: boolean; isFetching: boolean };
} = {
  query: "",
  results: { data: undefined, isPending: false, isFetching: false },
};
const semanticState: {
  data: SearchResults | undefined;
  isPending: boolean;
  isFetching: boolean;
} = { data: undefined, isPending: false, isFetching: false };
const semanticSpy = vi.fn(() => semanticState);
vi.mock("@/hooks/use-global-search", () => ({
  useGlobalSearch: () => searchState,
  useSemanticSearch: (q: string) => {
    void q;
    return semanticSpy();
  },
}));

const notebooksState: { data: unknown[] | undefined } = { data: undefined };
vi.mock("@/hooks/use-notebooks", () => ({
  useNotebooks: () => notebooksState,
}));

const RESULTS: SearchResults = {
  notebooks: [{ id: "nb1", title: "커피 노트", emoji: "☕" }],
  sources: [{ id: "s1", notebook_id: "nb2", title: "녹차 자료", type: "text" }],
  notes: [{ id: "n1", notebook_id: "nb3", title: "회의 정리", snippet: "결정사항…" }],
};

function openPalette() {
  act(() => useCommandPalette.getState().setOpen(true));
}

afterEach(() => {
  push.mockReset();
  semanticSpy.mockClear();
  act(() => useCommandPalette.getState().setOpen(false));
  searchState.query = "";
  searchState.results = { data: undefined, isPending: false, isFetching: false };
  semanticState.data = undefined;
  notebooksState.data = undefined;
});

describe("CommandPalette", () => {
  it("Cmd+K로 열린다", async () => {
    render(<CommandPalette />);
    expect(screen.queryByTestId("global-search-input")).not.toBeInTheDocument();
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(screen.getByTestId("global-search-input")).toBeInTheDocument();
  });

  it("결과를 노트북/소스/메모 그룹으로 렌더한다", () => {
    searchState.query = "테스트";
    searchState.results = { data: RESULTS, isPending: false, isFetching: false };
    openPalette();
    render(<CommandPalette />);

    const list = screen.getByTestId("global-search-results");
    expect(list).toHaveTextContent("노트북");
    expect(list).toHaveTextContent("☕ 커피 노트");
    expect(list).toHaveTextContent("소스");
    expect(list).toHaveTextContent("녹차 자료");
    expect(list).toHaveTextContent("메모");
    expect(list).toHaveTextContent("결정사항…");
  });

  it("화살표 키로 이동 후 Enter로 해당 노트북에 진입한다", async () => {
    searchState.query = "테스트";
    searchState.results = { data: RESULTS, isPending: false, isFetching: false };
    openPalette();
    render(<CommandPalette />);

    const input = screen.getByTestId("global-search-input");
    await userEvent.type(input, "{ArrowDown}{ArrowDown}{Enter}");
    expect(push).toHaveBeenCalledWith("/notebooks/nb3"); // 세 번째 항목(메모)
  });

  it("결과가 없으면 의미 검색 액션만 노출된다", () => {
    searchState.query = "없는검색어";
    searchState.results = {
      data: { notebooks: [], sources: [], notes: [] },
      isPending: false,
      isFetching: false,
    };
    openPalette();
    render(<CommandPalette />);
    // 키워드 매치는 없지만 본문 의미 검색 진입점은 항상 제공
    expect(screen.getByTestId("semantic-search-action")).toBeInTheDocument();
    expect(screen.queryByText("결과가 없습니다")).not.toBeInTheDocument();
  });

  it("의미 검색 액션 클릭 → 본문 매치 결과를 렌더한다", async () => {
    searchState.query = "찻잎 온도";
    searchState.results = {
      data: { notebooks: [], sources: [], notes: [] },
      isPending: false,
      isFetching: false,
    };
    semanticState.data = {
      notebooks: [],
      sources: [],
      notes: [],
      chunks: [
        {
          chunk_id: "c1",
          notebook_id: "nb9",
          source_id: "s1",
          source_title: "차 우리기 가이드",
          snippet: "녹차는 70~80도의 물에서 2분간 우린다",
          score: 0.9,
        },
      ],
    };
    openPalette();
    render(<CommandPalette />);

    await userEvent.click(screen.getByTestId("semantic-search-action"));
    const list = screen.getByTestId("global-search-results");
    expect(list).toHaveTextContent("본문");
    expect(list).toHaveTextContent("차 우리기 가이드");
    expect(list).toHaveTextContent("70~80도");

    // 본문 결과 클릭 → 해당 노트북으로 이동
    await userEvent.click(screen.getByText("차 우리기 가이드"));
    expect(push).toHaveBeenCalledWith("/notebooks/nb9");
  });
});

describe("CommandPalette — 검색어 없을 때(ui-ux-improvements ⑦)", () => {
  const nb = (id: string, title: string, updated_at: string) => ({
    id,
    title,
    emoji: "📒",
    updated_at,
  });

  it("최근 노트북(수정 시각 최신순 5개)과 빠른 이동을 보인다", () => {
    notebooksState.data = [
      nb("a", "가장 오래된", "2026-10-01T00:00:00Z"),
      nb("b", "최신", "2026-10-21T00:00:00Z"),
      nb("c", "중간", "2026-10-10T00:00:00Z"),
      nb("d", "넷", "2026-10-05T00:00:00Z"),
      nb("e", "다섯", "2026-10-04T00:00:00Z"),
      nb("f", "여섯", "2026-10-03T00:00:00Z"),
    ];
    render(<CommandPalette />);
    openPalette();
    const results = screen.getByTestId("global-search-results");
    expect(results).toHaveTextContent("최근 노트북");
    const recent = Array.from(results.querySelectorAll("[data-row-kind=recent]")).map((e) => e.textContent);
    expect(recent).toEqual(["📒 최신", "📒 중간", "📒 넷", "📒 다섯", "📒 여섯"]);
    expect(results).toHaveTextContent("바로 가기");
    expect(screen.getByRole("button", { name: "스킬" })).toBeInTheDocument();
  });

  it("Enter로 첫 최근 노트북에, 바로 가기는 그 페이지로 간다", async () => {
    notebooksState.data = [nb("b", "최신", "2026-10-21T00:00:00Z")];
    render(<CommandPalette />);
    openPalette();
    await userEvent.keyboard("{Enter}");
    expect(push).toHaveBeenCalledWith("/notebooks/b");

    openPalette();
    await userEvent.click(screen.getByRole("button", { name: "스킬" }));
    expect(push).toHaveBeenLastCalledWith("/skills");
  });

  it("노트북 목록을 아직 받지 못했어도 바로 가기는 보인다", () => {
    render(<CommandPalette />);
    openPalette();
    expect(screen.getByRole("button", { name: "노트북 홈" })).toBeInTheDocument();
    expect(screen.getByTestId("global-search-results")).not.toHaveTextContent("최근 노트북");
  });
});
