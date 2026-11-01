import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SourcePanel } from "@/components/sources/source-panel";
import type { Source, SourceDetail } from "@/lib/source-types";
import { useSourceFocus } from "@/stores/source-focus";
import { useSourceSelection } from "@/stores/source-selection";

// API 경계만 대체한다 — useSources·useSourceDetail·useDeleteSource 등 훅과 react-query 무효화,
// SourceDetailInline·AddSourceDialog는 실제 코드로 돈다(§3.3-3 R6).
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const SOURCES_PATH = "/api/v1/notebooks/nb1/sources";

function source(overrides: Partial<Source> = {}): Source {
  return {
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
    created_at: "2026-10-16T00:00:00Z",
    updated_at: "2026-10-16T00:00:00Z",
    ...overrides,
  };
}

function detailOf(s: Source): SourceDetail {
  return { ...s, parsed_preview: `${s.title} 본문 마크다운`, summary: null, keywords: null };
}

// 서버 상태 — 삭제가 목록에 반영되어야 「상세 중 삭제 → 목록」 흐름을 실제로 확인할 수 있다.
let server: Source[] = [];

beforeEach(() => {
  server = [
    source(),
    source({
      id: "s2",
      type: "web",
      title: "두 번째 소스",
      origin_url: "https://example.com/second",
      token_count: 200,
      chunk_count: 5,
    }),
  ];
  api.get.mockReset();
  api.delete.mockReset();
  api.get.mockImplementation((path: string) => {
    if (path === SOURCES_PATH) return Promise.resolve(server.map((s) => ({ ...s })));
    const guide = /^\/api\/v1\/sources\/([^/]+)\/guide$/.exec(path);
    if (guide) return Promise.resolve({ summary: "요약 한 줄", keywords: ["키워드"] });
    const one = /^\/api\/v1\/sources\/([^/]+)$/.exec(path);
    const found = one && server.find((s) => s.id === one[1]);
    return found ? Promise.resolve(detailOf(found)) : Promise.reject(new Error(`예상 밖 GET ${path}`));
  });
  api.delete.mockImplementation((path: string) => {
    server = server.filter((s) => path !== `/api/v1/sources/${s.id}`);
    return Promise.resolve(undefined);
  });
});

afterEach(() => {
  act(() => useSourceFocus.getState().clear());
  act(() => useSourceSelection.setState({ selected: {} }));
});

function renderPanel({ canWrite = true, visible = true } = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const ui = (v: boolean) => (
    <QueryClientProvider client={queryClient}>
      {/* 패널 밖의 포커스 대상(채팅 입력 자리) */}
      <input aria-label="채팅 입력" />
      <SourcePanel notebookId="nb1" canWrite={canWrite} visible={v} />
    </QueryClientProvider>
  );
  const result = render(ui(visible));
  return { queryClient, setVisible: (v: boolean) => result.rerender(ui(v)) };
}

const sourceButton = (title: string) =>
  within(screen.getByTestId("source-list")).getByRole("button", { name: title });
const panelHeading = () => screen.getByRole("heading", { level: 2, name: /^소스/ });

describe("SourcePanel — 소스 상세는 목록 아래가 아니라 소스 패널 전체", () => {
  it("소스를 누르면 대화상자 없이 목록 대신 상세 화면이 되고, 「목록」으로 돌아오면 그 소스 항목으로 포커스가 돌아온다", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "두 번째 소스" }));

    const view = await screen.findByTestId("source-detail-view");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("source-list")).toBeNull();
    // 목록 머리글(「소스」·소스 추가)도 상세 머리글로 바뀐다
    expect(screen.queryByRole("heading", { level: 2, name: /^소스/ })).toBeNull();
    expect(screen.queryByTestId("add-source")).toBeNull();
    const heading = within(view).getByRole("heading", { level: 2, name: "두 번째 소스" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(view).toHaveTextContent("준비됨 · 청크 5개 · 토큰 200");
    expect(await within(view).findByText("두 번째 소스 본문 마크다운")).toBeInTheDocument();
    expect(within(view).getByRole("link", { name: "원본 링크" })).toHaveAttribute(
      "href",
      "https://example.com/second",
    );
    expect(await within(view).findByText("요약 한 줄")).toBeInTheDocument();

    await user.click(within(view).getByTestId("source-detail-back"));
    expect(screen.queryByTestId("source-detail-view")).toBeNull();
    expect(screen.getByTestId("add-source")).toBeInTheDocument();
    await waitFor(() => expect(sourceButton("두 번째 소스")).toHaveFocus());
  });

  it("인용 포커스 요청은 그 소스의 상세 화면을 바로 열고, 다른 소스 상세가 열려 있으면 그 소스로 바꾼다", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "첫 번째 소스" }));
    await screen.findByRole("heading", { level: 2, name: "첫 번째 소스" });

    act(() => useSourceFocus.getState().request("s2"));

    const heading = await screen.findByRole("heading", { level: 2, name: "두 번째 소스" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.queryByRole("heading", { level: 2, name: "첫 번째 소스" })).toBeNull();
    expect(useSourceFocus.getState().requestedSourceId).toBeNull();

    // 돌아가면 인용으로 연 소스의 항목으로 포커스가 간다
    await user.click(screen.getByTestId("source-detail-back"));
    await waitFor(() => expect(sourceButton("두 번째 소스")).toHaveFocus());
  });

  it("[codex D7] 숨김 상태(visible=false)에서는 요청을 소모하지 않고 기다렸다가, 보이면 상세 화면을 연다", async () => {
    const { setVisible } = renderPanel({ visible: false });
    await screen.findByTestId("source-list");

    act(() => useSourceFocus.getState().request("s2"));
    // 목록 갱신 등 재렌더가 일어나도 숨김 동안은 그대로다
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(screen.queryByTestId("source-detail-view")).toBeNull();
    expect(screen.getByTestId("source-list")).toBeInTheDocument();
    expect(useSourceFocus.getState().requestedSourceId).toBe("s2");

    setVisible(true);
    const heading = await screen.findByRole("heading", { level: 2, name: "두 번째 소스" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(useSourceFocus.getState().requestedSourceId).toBeNull();
  });

  it("상세가 열린 동안 그 소스가 삭제되면 목록으로 돌아오고, 포커스는 패널 머리글로 간다", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderPanel();
    await user.click(await screen.findByRole("button", { name: "두 번째 소스" }));
    await waitFor(() =>
      expect(screen.getByRole("heading", { level: 2, name: "두 번째 소스" })).toHaveFocus(),
    );

    // 다른 곳(다른 탭·사용자)에서 지워지고 목록이 다시 읽힌다
    server = server.filter((s) => s.id !== "s2");
    await act(() => queryClient.invalidateQueries({ queryKey: ["sources", "nb1"] }));

    expect(await screen.findByTestId("source-list")).toBeInTheDocument();
    expect(screen.queryByTestId("source-detail-view")).toBeNull();
    expect(within(screen.getByTestId("source-list")).queryByText("두 번째 소스")).toBeNull();
    await waitFor(() => expect(panelHeading()).toHaveFocus());
  });

  it("삭제로 목록에 돌아올 때 포커스가 패널 밖(채팅 입력)에 있으면 빼앗지 않는다", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderPanel();
    await user.click(await screen.findByRole("button", { name: "두 번째 소스" }));
    await screen.findByTestId("source-detail-view");
    const chatInput = screen.getByRole("textbox", { name: "채팅 입력" });
    await user.click(chatInput);

    server = server.filter((s) => s.id !== "s2");
    await act(() => queryClient.invalidateQueries({ queryKey: ["sources", "nb1"] }));

    expect(await screen.findByTestId("source-list")).toBeInTheDocument();
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    expect(chatInput).toHaveFocus();
  });

  it("상세에서 돌아온 뒤에도 목록의 선택 체크박스·삭제는 그대로 동작한다", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "첫 번째 소스" }));
    await user.click(await screen.findByTestId("source-detail-back"));

    // 첫 로드 때 ready 소스는 모두 선택된다 — 체크박스로 해제할 수 있다
    const checkbox = screen.getByRole("checkbox", { name: "첫 번째 소스 선택" });
    await waitFor(() => expect(checkbox).toBeChecked());
    await user.click(checkbox);
    expect(checkbox).not.toBeChecked();

    const row = within(screen.getByTestId("source-list"))
      .getByRole("button", { name: "첫 번째 소스" })
      .closest("li") as HTMLElement;
    await user.click(within(row).getByRole("button", { name: "소스 삭제" }));
    const confirm = await screen.findByRole("alertdialog");
    await user.click(within(confirm).getByRole("button", { name: "삭제" }));

    await waitFor(() => expect(api.delete).toHaveBeenCalledWith("/api/v1/sources/s1"));
    await waitFor(() =>
      expect(within(screen.getByTestId("source-list")).queryByText("첫 번째 소스")).toBeNull(),
    );
    expect(screen.queryByTestId("source-detail-view")).toBeNull();
  });
});
