import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isTextLike,
  requiredReviewPaths,
  ReviewVersionPanel,
} from "@/components/skills/review-version-panel";

vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn(), getText: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "@/lib/api-client";

const mockGet = vi.mocked(api.get);
const mockGetText = vi.mocked(api.getText);

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const FILES = [
  { path: "SKILL.md", role: "skill_md", size: 10, media_type: "text/markdown", hash: "a" },
  { path: "scripts/fill.py", role: "script", size: 20, media_type: "text/x-python", hash: "b" },
  { path: "assets/logo.png", role: "asset", size: 30, media_type: "image/png", hash: "c" },
];

afterEach(() => {
  mockGet.mockReset();
  mockGetText.mockReset();
});

describe("review helpers", () => {
  it("텍스트로 볼 수 있는 파일만 원문을 연다", () => {
    expect(isTextLike({ media_type: "text/markdown", role: "skill_md" })).toBe(true);
    expect(isTextLike({ media_type: "application/octet-stream", role: "script" })).toBe(true);
    expect(isTextLike({ media_type: "application/json", role: "reference" })).toBe(true);
    expect(isTextLike({ media_type: "image/png", role: "asset" })).toBe(false);
  });

  it("스크립트 허가는 scripts/ 전부, 공개 요청은 SKILL.md를 확인해야 한다", () => {
    expect(requiredReviewPaths(FILES, "scripts")).toEqual(["scripts/fill.py"]);
    expect(requiredReviewPaths(FILES, "expose_org")).toEqual(["SKILL.md"]);
    expect(requiredReviewPaths([{ path: "SKILL.md" }], "scripts")).toEqual(["SKILL.md"]);
    expect(requiredReviewPaths([], "expose_org")).toEqual([]);
  });
});

describe("ReviewVersionPanel (P0-03)", () => {
  it("검토 엔드포인트로 파일 트리와 SKILL.md 원문을 열고, 열람 뒤에만 확인을 알린다", async () => {
    mockGet.mockResolvedValue(FILES);
    mockGetText.mockResolvedValue("---\nname: s\n---\n본문");
    const onReviewed = vi.fn();

    render(wrap(<ReviewVersionPanel versionId="v1" kind="expose_org" onReviewed={onReviewed} />));

    expect(await screen.findByTestId("review-file-content")).toHaveTextContent("본문");
    expect(mockGet).toHaveBeenCalledWith("/api/v1/admin/skills/versions/v1/files");
    expect(mockGetText).toHaveBeenCalledWith("/api/v1/admin/skills/versions/v1/files/SKILL.md");
    await waitFor(() => expect(onReviewed).toHaveBeenLastCalledWith(true));
    expect(onReviewed).toHaveBeenCalledWith(false); // 열기 전에는 확인 전이었다
  });

  it("스크립트 허가 요청은 스크립트 원문을 열어야 확인이 된다", async () => {
    mockGet.mockResolvedValue(FILES);
    mockGetText.mockImplementation(async (path: string) =>
      path.endsWith("fill.py") ? "print('fill')" : "skill body",
    );
    const onReviewed = vi.fn();

    render(wrap(<ReviewVersionPanel versionId="v1" kind="scripts" onReviewed={onReviewed} />));

    await screen.findByText("skill body");
    expect(onReviewed).not.toHaveBeenCalledWith(true); // SKILL.md만으로는 부족하다
    fireEvent.click(screen.getByRole("button", { name: /scripts\/fill\.py/ }));
    expect(await screen.findByText("print('fill')")).toBeInTheDocument();
    expect(mockGetText).toHaveBeenCalledWith(
      "/api/v1/admin/skills/versions/v1/files/scripts/fill.py",
    );
    await waitFor(() => expect(onReviewed).toHaveBeenLastCalledWith(true));
  });

  it("바이너리는 원문을 요청하지 않는다", async () => {
    mockGet.mockResolvedValue(FILES);
    mockGetText.mockResolvedValue("body");

    render(wrap(<ReviewVersionPanel versionId="v1" kind="expose_org" onReviewed={() => {}} />));
    await screen.findByText("body");
    fireEvent.click(screen.getByRole("button", { name: /assets\/logo\.png/ }));

    expect(await screen.findByText(/바이너리 파일/)).toBeInTheDocument();
    expect(mockGetText).toHaveBeenCalledTimes(1);
  });

  it("원문을 열지 못하면 오류를 보이고 확인으로 치지 않는다", async () => {
    mockGet.mockResolvedValue(FILES);
    mockGetText.mockRejectedValue(new Error("File not found"));
    const onReviewed = vi.fn();

    render(wrap(<ReviewVersionPanel versionId="v1" kind="expose_org" onReviewed={onReviewed} />));

    expect(await screen.findByText("File not found")).toBeInTheDocument();
    expect(onReviewed).not.toHaveBeenCalledWith(true);
  });
});
