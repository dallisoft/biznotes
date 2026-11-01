import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunSourceCoverage } from "@/components/agents/run-source-coverage";
import type { AgentRun } from "@/lib/agent-types";

const { fetchArtifact, sources } = vi.hoisted(() => ({
  fetchArtifact: vi.fn<(runId: string, name: string) => Promise<string>>(),
  sources: { data: [{ id: "s1", title: "첫 소스" }, { id: "s2", title: "둘째 소스" }] as { id: string; title: string }[] },
}));
vi.mock("@/hooks/use-agents", () => ({ fetchArtifact }));
vi.mock("@/hooks/use-sources", () => ({ useSources: () => sources }));

function run(names: string[]): AgentRun {
  return {
    id: "run-1",
    artifact_keys: names.map((name) => ({ key: `run-1/${name}`, name, content_type: "application/json" })),
  } as unknown as AgentRun;
}

function renderCoverage(r: AgentRun) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <RunSourceCoverage run={r} notebookId="nb-1" />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.resetAllMocks());

describe("RunSourceCoverage — 결과 위 소스 반영 표시(#217, PR-3 AC-14)", () => {
  it("근거가 없는 소스와 대체 본문 소스를 제목으로 보인다(지워진 소스는 「삭제된 소스」)", async () => {
    fetchArtifact.mockResolvedValue(
      JSON.stringify({
        scope_count: 3,
        hit_sources: ["s1"],
        missing: ["s2", "gone"],
        digest_sources: ["s1", "s2", "gone"],
        degraded: [{ id: "s2", title: "둘째 소스", reason: "generation_failed" }],
      }),
    );
    renderCoverage(run(["report.md", "coverage.json"]));
    const note = await screen.findByTestId("run-source-coverage");
    expect(note).toHaveTextContent("고른 소스 3개 중 1개에서 근거를 찾았습니다. 요약에는 3개가 들어갔습니다.");
    expect(screen.getByTestId("run-source-coverage-missing")).toHaveTextContent("근거 발췌가 없는 소스 2개둘째 소스삭제된 소스");
    expect(screen.getByTestId("run-source-coverage-degraded")).toHaveTextContent(
      "요약 대신 대체 본문(소스 가이드 요약·앞부분 발췌 등)으로 들어간 소스 1개둘째 소스",
    );
    expect(fetchArtifact).toHaveBeenCalledWith("run-1", "coverage.json");
  });

  it("빠진 소스가 없으면 아무것도 그리지 않는다", async () => {
    fetchArtifact.mockResolvedValue(JSON.stringify({ scope_count: 2, hit_sources: ["s1", "s2"], missing: [] }));
    const { container } = renderCoverage(run(["quiz.json", "coverage.json"]));
    await waitFor(() => expect(fetchArtifact).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("coverage.json이 없는 옛 run은 읽지도 그리지도 않는다", () => {
    const { container } = renderCoverage(run(["report.md"]));
    expect(container).toBeEmptyDOMElement();
    expect(fetchArtifact).not.toHaveBeenCalled();
  });
});
