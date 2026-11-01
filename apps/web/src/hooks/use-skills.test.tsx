import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  skillExportUrl,
  useCloneSkill,
  usePublishSkillVersion,
  useSkills,
} from "@/hooks/use-skills";

vi.mock("@/lib/api-client", () => ({
  api: { get: vi.fn(), post: vi.fn() },
  ApiError: class ApiError extends Error {},
}));

import { api } from "@/lib/api-client";

const mockGet = vi.mocked(api.get);
const mockPost = vi.mocked(api.post);

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

describe("useSkills", () => {
  it("검색어를 쿼리스트링으로 전달한다", async () => {
    mockGet.mockResolvedValue([]);
    const { result } = renderHook(() => useSkills({ q: "보고서" }), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockGet).toHaveBeenCalledWith("/api/v1/skills?q=%EB%B3%B4%EA%B3%A0%EC%84%9C");
  });

  it("필터가 없으면 접미사 없이 호출한다", async () => {
    mockGet.mockResolvedValue([]);
    const { result } = renderHook(() => useSkills(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockGet).toHaveBeenCalledWith("/api/v1/skills");
  });
});

describe("useCloneSkill", () => {
  it("clone 엔드포인트를 호출한다", async () => {
    mockPost.mockResolvedValue({ skill_id: "new", status: "draft" });
    const { result } = renderHook(() => useCloneSkill(), { wrapper });
    result.current.mutate("src-id");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockPost).toHaveBeenCalledWith("/api/v1/skills/src-id/clone");
    expect(result.current.data).toEqual({ skill_id: "new", status: "draft" });
  });
});


describe("usePublishSkillVersion", () => {
  it("version publish 엔드포인트를 호출한다", async () => {
    mockPost.mockResolvedValue({ status: "published", version_id: "v1", request_id: null });
    const { result } = renderHook(() => usePublishSkillVersion("skill-1"), { wrapper });

    result.current.mutate("v1");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mockPost).toHaveBeenCalledWith("/api/v1/skills/skill-1/versions/v1/publish");
  });

  it("검토가 필요하면 review_pending 결과를 그대로 돌려준다(202)", async () => {
    mockPost.mockResolvedValue({ status: "review_pending", version_id: "v1", request_id: "r1" });
    const { result } = renderHook(() => usePublishSkillVersion("skill-1"), { wrapper });

    result.current.mutate("v1");

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({ status: "review_pending", request_id: "r1" });
  });
});

describe("skillExportUrl", () => {
  it("기본은 래퍼형·제품 매니페스트 포함이다", () => {
    expect(skillExportUrl("v1")).toContain("/api/v1/skills/versions/v1/export");
    expect(skillExportUrl("v1")).not.toContain("?");
  });

  it("옵션을 쿼리스트링으로 붙인다", () => {
    const url = skillExportUrl("v1", { layout: "flat", portable: true });
    expect(url).toContain("layout=flat");
    expect(url).toContain("portable=true");
  });
});
