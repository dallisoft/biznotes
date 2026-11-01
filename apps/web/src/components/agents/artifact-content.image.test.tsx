/** 이미지 산출물(core-v0.23.0-upgrade — 슬라이드 삽화)의 「결과」 탭 표시 — 텍스트로 읽지 않고 그림으로 보인다. */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ArtifactContent } from "@/components/agents/artifact-content";
import type { AgentRun } from "@/lib/agent-types";

const { fetchText, fetchImage } = vi.hoisted(() => ({ fetchText: vi.fn(), fetchImage: vi.fn() }));
vi.mock("@/hooks/use-agents", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/use-agents")>()),
  fetchArtifact: fetchText,
  fetchArtifactObjectUrl: fetchImage,
}));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@xyflow/react/dist/style.css", () => ({}));

const RUN = { id: "run-deck", agent_name: "슬라이드 자료" } as unknown as AgentRun;
const IMAGE = { key: "run-deck/slide-image-2.png", name: "slide-image-2.png", content_type: "image/png" };

function renderContent() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ArtifactContent run={RUN} artifact={IMAGE} notebookId="nb-1" canWrite />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  fetchText.mockReset();
  fetchImage.mockReset();
});

describe("이미지 산출물", () => {
  it("인증 요청으로 받은 그림과 다운로드 링크를 보이고, 본문 텍스트·메모 저장은 없다", async () => {
    fetchImage.mockResolvedValue("blob:slide-image-2");
    renderContent();
    const box = await screen.findByTestId("artifact-image");
    expect(within(box).getByRole("img", { name: "slide-image-2.png" })).toHaveAttribute("src", "blob:slide-image-2");
    expect(within(box).getByRole("link", { name: "다운로드" })).toHaveAttribute("download", "slide-image-2.png");
    expect(fetchImage).toHaveBeenCalledWith("run-deck", "slide-image-2.png");
    expect(fetchText).not.toHaveBeenCalled();
    expect(screen.queryByTestId("artifact-save-to-note")).toBeNull();
  });

  it("받지 못하면 오류 문구를 보인다", async () => {
    fetchImage.mockRejectedValue(new Error("404"));
    renderContent();
    expect(await screen.findByText("이미지를 불러오지 못했습니다")).toBeInTheDocument();
  });
});
