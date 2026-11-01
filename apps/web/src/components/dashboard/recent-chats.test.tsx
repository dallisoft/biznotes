import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RecentChats } from "@/components/dashboard/recent-chats";

// 네트워크 경계(api)만 대체한다 — useRecentChats 훅은 실제 코드다
const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock("@/lib/api-client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api-client")>()),
  api,
}));

afterEach(() => vi.resetAllMocks());

function renderRecent() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <RecentChats />
    </QueryClientProvider>,
  );
}

const ITEM = {
  session_id: "s1",
  notebook_id: "nb1",
  notebook_title: "취업규칙",
  notebook_emoji: "🏢",
  title: "연차는 며칠인가요?",
  updated_at: "2026-10-22T01:00:00Z",
};

describe("RecentChats — 홈 「최근 대화 이어가기」(ui-ux-improvements ⑥)", () => {
  it("최근 대화를 그 노트북의 그 대화로 가는 링크로 보인다", async () => {
    api.get.mockResolvedValue([ITEM, { ...ITEM, session_id: "s2", title: "두 번째" }]);
    renderRecent();
    const region = await screen.findByRole("region", { name: "최근 대화 이어가기" });
    expect(api.get).toHaveBeenCalledWith("/api/v1/chat/recent?limit=3");
    const links = within(region).getAllByRole("link");
    expect(links).toHaveLength(2);
    expect(links[0]).toHaveAttribute("href", "/notebooks/nb1?s=s1");
    expect(links[0]).toHaveTextContent("연차는 며칠인가요?");
    expect(links[0]).toHaveTextContent("🏢 취업규칙");
  });

  it("최근 대화가 없거나 불러오지 못하면 구역을 그리지 않는다", async () => {
    api.get.mockResolvedValue([]);
    renderRecent();
    await vi.waitFor(() => expect(api.get).toHaveBeenCalled());
    expect(screen.queryByRole("region", { name: "최근 대화 이어가기" })).toBeNull();
  });
});
