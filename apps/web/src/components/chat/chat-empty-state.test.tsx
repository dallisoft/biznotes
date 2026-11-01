/**
 * 빈 대화 화면(plan §6.6.2): 문구 「이 노트북에서 무엇을 작업할까요?」(노트북을 「노트」로 부르지 않음 — P-05 ⑦),
 * 쓰기 권한이 없으면(읽기 전용 공유) 소스 추가 카드를 그리지 않는다(서버 403 막다른 길 방지).
 * 네트워크 경계(`api`)만 대체한다 — ConnectCards·훅은 실제 코드다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatEmptyState } from "@/components/chat/chat-empty-state";

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

beforeEach(() => {
  api.get.mockImplementation(() => new Promise(() => {}));
});
afterEach(() => vi.resetAllMocks());

function renderEmpty(props: Partial<Parameters<typeof ChatEmptyState>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ChatEmptyState
        notebookId="nb1"
        isStreaming={false}
        regenerating={false}
        onRegenerate={vi.fn()}
        onAskQuestion={vi.fn()}
        {...props}
      />
    </QueryClientProvider>,
  );
}

const OVERVIEW = {
  summary: "커피 추출 요약",
  suggested_questions: ["에스프레소 압력은?"],
  title_suggestions: [],
  generated_at: "2026-10-01T00:00:00Z",
  stale: false,
};

describe("ChatEmptyState", () => {
  it("요약이 없을 때 「이 노트북에서 무엇을 작업할까요?」와 「소스 추가하기」 카드를 보인다", () => {
    renderEmpty();
    expect(screen.getByText("이 노트북에서 무엇을 작업할까요?")).toBeInTheDocument();
    expect(screen.queryByText(/이 노트에서/)).toBeNull();
    expect(screen.getByTestId("connect-cards")).toBeInTheDocument();
  });

  it("쓰기 권한이 없으면(읽기 전용) 요약 유무와 관계없이 소스 추가 카드를 그리지 않는다", () => {
    renderEmpty({ canWrite: false });
    expect(screen.getByText("이 노트북에서 무엇을 작업할까요?")).toBeInTheDocument();
    expect(screen.queryByTestId("connect-cards")).toBeNull();
    expect(screen.queryByText(/소스를 추가한 뒤/)).toBeNull();
  });

  it("요약이 있으면 요약 카드 + 추천 질문을 보인다", () => {
    renderEmpty({ overview: OVERVIEW, sourceCount: 1 });
    expect(screen.getByTestId("overview-card")).toHaveTextContent("커피 추출 요약");
    expect(screen.getByRole("button", { name: "에스프레소 압력은?" })).toBeInTheDocument();
  });

  it("개요 입력 소스 수(source_count, 코어 #449)가 있으면 「소스 N개 기준」을 보이고 0·없으면 숨긴다(#286)", () => {
    renderEmpty({ overview: { ...OVERVIEW, source_count: 1234 }, sourceCount: 1234 });
    expect(screen.getByTestId("overview-source-count")).toHaveTextContent("소스 1,234개 기준");
    cleanup();
    renderEmpty({ overview: { ...OVERVIEW, source_count: 0 }, sourceCount: 1 });
    expect(screen.queryByTestId("overview-source-count")).toBeNull();
  });

  it("소스가 1개 이상이면 「소스 추가하기」 카드를 그리지 않는다(ui-ux-improvements ①)", () => {
    renderEmpty({ overview: OVERVIEW, sourceCount: 1 });
    expect(screen.queryByTestId("connect-cards")).toBeNull();
  });

  it("요약을 만드는 중이라도 소스가 있으면 카드를 그리지 않는다", () => {
    renderEmpty({ overview: { ...OVERVIEW, summary: null, stale: true }, sourceCount: 2 });
    expect(screen.getByText("소스 요약을 생성하는 중…")).toBeInTheDocument();
    expect(screen.queryByTestId("connect-cards")).toBeNull();
  });

  it("짧은 요약은 접지 않는다", () => {
    renderEmpty({ overview: OVERVIEW, sourceCount: 1 });
    expect(screen.getByTestId("overview-summary").className).not.toContain("line-clamp-2");
    expect(screen.queryByRole("button", { name: "더 보기" })).toBeNull();
  });

  it("긴 소스 요약은 기본으로 2줄로 접혀 있고 「더 보기」로 펼친다", async () => {
    const user = userEvent.setup();
    renderEmpty({ overview: { ...OVERVIEW, summary: "커피 추출 요약 ".repeat(20) }, sourceCount: 1 });
    const summary = screen.getByTestId("overview-summary");
    expect(summary.className).toContain("line-clamp-2");
    const toggle = screen.getByRole("button", { name: "더 보기" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(summary.className).not.toContain("line-clamp-2");
    expect(screen.getByRole("button", { name: "접기" })).toHaveAttribute("aria-expanded", "true");
  });

  it("요약이 있어도 읽기 전용이면 카드가 없다", () => {
    renderEmpty({ overview: OVERVIEW, canWrite: false });
    expect(screen.getByTestId("overview-card")).toBeInTheDocument();
    expect(screen.queryByTestId("connect-cards")).toBeNull();
  });
});
