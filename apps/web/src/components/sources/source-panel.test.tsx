import { render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SourcePanel } from "@/components/sources/source-panel";
import type { Source } from "@/lib/source-types";
import { useSourceFocus } from "@/stores/source-focus";
import { useSourceSelection } from "@/stores/source-selection";

vi.mock("@/components/sources/add-source-dialog", () => ({
  AddSourceDialog: () => <div data-testid="add-source-dialog" />,
}));
vi.mock("@/components/sources/source-detail-inline", () => ({
  SourceDetailInline: ({ source, highlightQuote }: { source: Source; highlightQuote?: string | null }) => (
    <div data-testid="source-detail-inline" data-quote={highlightQuote ?? ""}>
      {source.title}
    </div>
  ),
}));

const SOURCES: Source[] = [
  {
    id: "s1",
    notebook_id: "nb1",
    type: "text",
    title: "첫 번째 소스",
    origin_url: null,
    mime_type: null,
    size_bytes: null,
    status: "ready",
    error_message: null,
    token_count: 100,
    chunk_count: 3,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "s2",
    notebook_id: "nb1",
    type: "web",
    title: "두 번째 소스",
    origin_url: "https://example.com",
    mime_type: null,
    size_bytes: null,
    status: "ready",
    error_message: null,
    token_count: 200,
    chunk_count: 5,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  },
];

vi.mock("@/hooks/use-sources", () => ({
  useSources: () => ({ data: SOURCES, isPending: false }),
  useDeleteSource: () => ({ mutate: vi.fn() }),
  useRetrySource: () => ({ mutate: vi.fn(), isPending: false }),
}));

afterEach(() => {
  act(() => useSourceFocus.getState().clear());
  act(() => useSourceSelection.setState({ selected: {} }));
});

describe("SourcePanel — [Phase 18c] 인용 클릭 시 소스 포커스", () => {
  it("requestedSourceId가 설정되면 목록 대신 해당 소스의 상세 화면을 연다", async () => {
    render(<SourcePanel notebookId="nb1" canWrite={false} />);

    act(() => useSourceFocus.getState().request("s2"));

    await waitFor(() => {
      expect(screen.getByTestId("source-detail-inline")).toHaveTextContent("두 번째 소스");
    });
    expect(screen.queryByTestId("source-list")).not.toBeInTheDocument();
    // 1회성 요청이므로 처리 후 초기화됨
    expect(useSourceFocus.getState().requestedSourceId).toBeNull();
  });

  it("존재하지 않는 소스 id는 무시한다", () => {
    render(<SourcePanel notebookId="nb1" canWrite={false} />);
    act(() => useSourceFocus.getState().request("missing"));
    expect(screen.queryByTestId("source-detail-inline")).not.toBeInTheDocument();
    expect(screen.getByTestId("source-list")).toBeInTheDocument();
  });

  it("[codex D7] 패널이 hidden(visible=false)이면 상세를 열지 않고 요청을 보존한다", async () => {
    const { rerender } = render(
      <SourcePanel notebookId="nb1" canWrite={false} visible={false} />,
    );

    act(() => useSourceFocus.getState().request("s2"));

    // hidden 상태에서는 상세 열기도, 요청 소모도 일어나지 않아야 함
    expect(screen.queryByTestId("source-detail-inline")).not.toBeInTheDocument();
    expect(screen.getByTestId("source-list")).toBeInTheDocument();
    expect(useSourceFocus.getState().requestedSourceId).toBe("s2");

    // 탭 전환으로 visible=true가 되면 그제서야 처리된다
    rerender(<SourcePanel notebookId="nb1" canWrite={false} visible={true} />);

    await waitFor(() => {
      expect(screen.getByTestId("source-detail-inline")).toHaveTextContent("두 번째 소스");
    });
    expect(useSourceFocus.getState().requestedSourceId).toBeNull();
  });
});

describe("SourcePanel — 인용 발췌 전달·읽기 모드 알림(ui-ux-improvements ②)", () => {
  it("인용 요청의 발췌를 상세 화면에 넘기고, 상세가 열리고 닫힐 때 onDetailOpenChange를 부른다", async () => {
    const onDetailOpenChange = vi.fn();
    render(<SourcePanel notebookId="nb1" canWrite={false} onDetailOpenChange={onDetailOpenChange} />);

    act(() => useSourceFocus.getState().request("s2", "두 번째 소스의 인용 발췌"));

    const detail = await screen.findByTestId("source-detail-inline");
    expect(detail).toHaveAttribute("data-quote", "두 번째 소스의 인용 발췌");
    expect(useSourceFocus.getState().requestedQuote).toBeNull();
    await waitFor(() => expect(onDetailOpenChange).toHaveBeenLastCalledWith(true));
  });
});
