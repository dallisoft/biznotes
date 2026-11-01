/**
 * 빈 대화 화면의 「소스 추가하기」 카드.
 *
 * 네트워크 경계(`api`)만 대체한다. 카드·다이얼로그·훅은 실제 코드다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConnectCards } from "@/components/chat/connect-cards";

const { api, toast } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), postForm: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));
vi.mock("sonner", () => ({ toast }));

const never = () => new Promise<never>(() => {});

let replies: Record<string, unknown>;

beforeEach(() => {
  replies = {
    "/api/v1/notebooks/nb1/sources": [
      { id: "s1", title: "a" },
      { id: "s2", title: "b" },
      { id: "s3", title: "c" },
    ],
  };
  api.get.mockImplementation((path: string) =>
    path in replies ? Promise.resolve(replies[path]) : never(),
  );
});

afterEach(() => vi.resetAllMocks());

function renderCards() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ConnectCards notebookId="nb1" />
    </QueryClientProvider>,
  );
}

describe("ConnectCards", () => {
  it("「소스 추가」 카드 하나만 보인다", () => {
    renderCards();
    expect(screen.getByRole("heading", { name: "소스 추가하기" })).toBeInTheDocument();
    const cards = within(screen.getByTestId("connect-cards")).getAllByRole("button");
    expect(cards).toHaveLength(1);
    expect(cards[0]).toHaveTextContent("소스 추가");
  });

  it("카드로 연 다이얼로그 설명은 실제 소스 수와 서버 상한을 보인다(항상 0/1000이 아니다)", async () => {
    const user = userEvent.setup();
    replies["/api/v1/sources/upload-limits"] = {
      max_upload_bytes: 50 * 1024 * 1024,
      max_sources_per_notebook: 500,
    };
    renderCards();
    await waitFor(() => expect(api.get).toHaveBeenCalledWith("/api/v1/notebooks/nb1/sources"));
    await user.click(screen.getByTestId("connect-card-upload"));
    const dialog = await screen.findByRole("dialog", { name: "소스 추가" });
    expect(await within(dialog).findByText("3/500 사용 중")).toBeInTheDocument();
    expect(within(dialog).getByRole("tab", { name: "파일" })).toHaveAttribute("aria-selected", "true");
  });
});
