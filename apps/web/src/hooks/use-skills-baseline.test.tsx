/**
 * 기준선(`1079bbf..c7e682e`) 추적표 공백 행 — `use-skills.ts` 훅(계획 AC-21 (a), #59~#82).
 *
 * 경계(`@/lib/api-client`)만 대체하고 훅 본문(경로·페이로드·무효화 키)은 실제로 실행한다.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  fetchAdminReviewFile,
  useAdminReviewRequests,
  useAdminReviewVersionFiles,
  useAdminSetPublishedVersion,
  useAdminSkills,
  useAdminUpdateSkill,
  useApproveReviewRequest,
  useDeleteSkillFile,
  useImportSkillZip,
  useRejectReviewRequest,
  useRequestSkillReview,
  useSkill,
  useSkillCatalog,
  useSkillDraftFiles,
  useSkillFrontmatter,
  useSkillReviewRequests,
  useSkillVersions,
  useSkillZipCandidates,
  useValidateSkillFrontmatter,
  useWriteSkillBinaryFile,
  useWriteSkillFile,
  useWriteSkillFrontmatter,
} from "@/hooks/use-skills";

vi.mock("@/lib/api-client", () => ({
  api: {
    get: vi.fn(),
    getText: vi.fn(),
    post: vi.fn(),
    postForm: vi.fn(),
    put: vi.fn(),
    putBytes: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

import { api } from "@/lib/api-client";

const m = vi.mocked(api);

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidated = vi.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const keys = () => invalidated.mock.calls.map(([filters]) => filters?.queryKey);
  return { wrapper, keys };
}

afterEach(() => {
  for (const fn of Object.values(m)) (fn as ReturnType<typeof vi.fn>).mockReset();
});

describe("단건 조회 훅 (#59·#60·#61·#63)", () => {
  it.each([
    ["useSkill", useSkill, "/api/v1/skills/s1"],
    ["useSkillVersions", useSkillVersions, "/api/v1/skills/s1/versions"],
    ["useSkillDraftFiles", useSkillDraftFiles, "/api/v1/skills/s1/files"],
    ["useSkillFrontmatter", useSkillFrontmatter, "/api/v1/skills/s1/frontmatter"],
  ] as const)("%s는 스킬 id 경로를 조회하고 id가 없으면 호출하지 않는다", async (_n, hook, path) => {
    const { wrapper } = setup();
    m.get.mockResolvedValue({});
    const { result } = renderHook(() => hook("s1"), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.get).toHaveBeenCalledWith(path);

    m.get.mockClear();
    const idle = renderHook(() => hook(null), { wrapper });
    expect(idle.result.current.fetchStatus).toBe("idle");
    expect(m.get).not.toHaveBeenCalled();
  });
});

describe("가져오기 (#64·#65)", () => {
  it("useImportSkillZip은 selected_skill_path를 FormData로 보내고 목록을 무효화한다", async () => {
    const { wrapper, keys } = setup();
    m.postForm.mockResolvedValue({ id: "s1", import_report: {} });
    const { result } = renderHook(() => useImportSkillZip(), { wrapper });
    const file = new File(["zip"], "pack.zip");

    result.current.mutate({ file, selectedSkillPath: "pack/b/SKILL.md" });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const [path, fd] = m.postForm.mock.calls[0] as [string, FormData];
    expect(path).toBe("/api/v1/skills/import-zip");
    expect(fd.get("file")).toBeInstanceOf(File);
    expect(fd.get("selected_skill_path")).toBe("pack/b/SKILL.md");
    expect(keys()).toContainEqual(["skills"]);
  });

  it("선택 경로가 없으면 필드를 보내지 않는다", async () => {
    const { wrapper } = setup();
    m.postForm.mockResolvedValue({ id: "s1" });
    const { result } = renderHook(() => useImportSkillZip(), { wrapper });
    result.current.mutate({ file: new File(["zip"], "pack.zip") });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const fd = m.postForm.mock.calls[0][1] as FormData;
    expect(fd.has("selected_skill_path")).toBe(false);
  });

  it("useSkillZipCandidates는 후보 조회 엔드포인트를 쓴다", async () => {
    const { wrapper } = setup();
    m.postForm.mockResolvedValue({ candidates: [] });
    const { result } = renderHook(() => useSkillZipCandidates(), { wrapper });
    result.current.mutate(new File(["zip"], "pack.zip"));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.postForm.mock.calls[0][0]).toBe("/api/v1/skills/import-zip/candidates");
  });
});

describe("파일 쓰기·삭제 (#66·#67·#70)", () => {
  it("useWriteSkillFile은 경로를 세그먼트 단위로 인코딩하고 SKILL.md면 frontmatter도 무효화한다", async () => {
    const { wrapper, keys } = setup();
    m.put.mockResolvedValue(undefined);
    const { result } = renderHook(() => useWriteSkillFile("s1"), { wrapper });

    result.current.mutate({ path: "references/한 글.md", content: "x" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.put).toHaveBeenCalledWith(
      "/api/v1/skills/s1/files/references/%ED%95%9C%20%EA%B8%80.md",
      { content: "x" },
    );
    expect(keys()).toEqual([["skill-files", "s1"]]);

    result.current.mutate({ path: "SKILL.md", content: "y" });
    await waitFor(() => expect(keys()).toContainEqual(["skill-frontmatter", "s1"]));
  });

  it("useWriteSkillBinaryFile은 원본 바이트를 putBytes로 보낸다", async () => {
    const { wrapper, keys } = setup();
    m.putBytes.mockResolvedValue(undefined);
    const { result } = renderHook(() => useWriteSkillBinaryFile("s1"), { wrapper });
    const file = new File([new Uint8Array([1, 2])], "logo.png");

    result.current.mutate({ path: "assets/logo.png", file });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.putBytes).toHaveBeenCalledWith("/api/v1/skills/s1/files/assets/logo.png", file);
    expect(keys()).toEqual([["skill-files", "s1"]]);
  });

  it("useDeleteSkillFile은 인코딩된 경로로 DELETE한다", async () => {
    const { wrapper } = setup();
    m.delete.mockResolvedValue(undefined);
    const { result } = renderHook(() => useDeleteSkillFile("s1"), { wrapper });
    result.current.mutate("scripts/a b.py");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.delete).toHaveBeenCalledWith("/api/v1/skills/s1/files/scripts/a%20b.py");
  });
});

describe("frontmatter (#68·#69)", () => {
  it("useWriteSkillFrontmatter는 source_hash를 보내고 관련 키 4개를 무효화한다", async () => {
    const { wrapper, keys } = setup();
    m.put.mockResolvedValue({ editable: true });
    const { result } = renderHook(() => useWriteSkillFrontmatter("s1"), { wrapper });

    result.current.mutate({ fields: { name: "n" }, sourceHash: "h1" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.put).toHaveBeenCalledWith("/api/v1/skills/s1/frontmatter", {
      fields: { name: "n" },
      source_hash: "h1",
    });
    expect(keys()).toEqual([
      ["skill-frontmatter", "s1"],
      ["skill-file-content", "s1", "SKILL.md"],
      ["skill", "s1"],
      ["skills"],
    ]);
  });

  it("useValidateSkillFrontmatter는 검증 엔드포인트로 같은 페이로드를 보낸다", async () => {
    const { wrapper } = setup();
    m.post.mockResolvedValue({ ok: true });
    const { result } = renderHook(() => useValidateSkillFrontmatter("s1"), { wrapper });
    result.current.mutate({ fields: { description: "d" }, sourceHash: "h2" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.post).toHaveBeenCalledWith("/api/v1/skills/s1/frontmatter/validate", {
      fields: { description: "d" },
      source_hash: "h2",
    });
  });
});

describe("카탈로그·검토 요청 (#72·#73·#74)", () => {
  it("useSkillCatalog", async () => {
    const { wrapper } = setup();
    m.get.mockResolvedValue({ enabled: true, entries: [] });
    const { result } = renderHook(() => useSkillCatalog(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.get).toHaveBeenCalledWith("/api/v1/skills/catalog");
  });

  it("useRequestSkillReview는 version 경로에 kind를 보내고 검토·카탈로그를 무효화한다", async () => {
    const { wrapper, keys } = setup();
    m.post.mockResolvedValue({ id: "r1" });
    const { result } = renderHook(() => useRequestSkillReview("s1"), { wrapper });
    result.current.mutate({ versionId: "v1", kind: "scripts" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.post).toHaveBeenCalledWith("/api/v1/skills/s1/versions/v1/review-requests", {
      kind: "scripts",
    });
    expect(keys()).toEqual([["skill-reviews", "s1"], ["skill-catalog"]]);
  });

  it("useSkillReviewRequests는 enabled=false면 조회하지 않는다", async () => {
    const { wrapper } = setup();
    m.get.mockResolvedValue([]);
    const off = renderHook(() => useSkillReviewRequests("s1", false), { wrapper });
    expect(off.result.current.fetchStatus).toBe("idle");
    const on = renderHook(() => useSkillReviewRequests("s1"), { wrapper });
    await waitFor(() => expect(on.result.current.isSuccess).toBe(true));
    expect(m.get).toHaveBeenCalledTimes(1);
    expect(m.get).toHaveBeenCalledWith("/api/v1/skills/s1/review-requests");
  });
});

describe("어드민 (#76~#82 + 검토 열람)", () => {
  it("목록·검토 큐 조회", async () => {
    const { wrapper } = setup();
    m.get.mockResolvedValue([]);
    const skills = renderHook(() => useAdminSkills(), { wrapper });
    const queue = renderHook(() => useAdminReviewRequests("approved"), { wrapper });
    await waitFor(() => expect(skills.result.current.isSuccess).toBe(true));
    await waitFor(() => expect(queue.result.current.isSuccess).toBe(true));
    expect(m.get).toHaveBeenCalledWith("/api/v1/admin/skills");
    expect(m.get).toHaveBeenCalledWith("/api/v1/admin/skills/review-requests?status=approved");
  });

  it("승인·거절·철회는 어드민 키 3종을 무효화한다(useAdminSkillMutation)", async () => {
    const expected = [["admin-skills"], ["admin-skill-reviews"], ["skills"]];
    m.post.mockResolvedValue({});

    const a = setup();
    const approve = renderHook(() => useApproveReviewRequest(), { wrapper: a.wrapper });
    approve.result.current.mutate("r1");
    await waitFor(() => expect(approve.result.current.isSuccess).toBe(true));
    expect(m.post).toHaveBeenLastCalledWith("/api/v1/admin/skills/review-requests/r1/approve");
    expect(a.keys()).toEqual(expected);

    const r = setup();
    const reject = renderHook(() => useRejectReviewRequest(), { wrapper: r.wrapper });
    reject.result.current.mutate({ requestId: "r2" });
    await waitFor(() => expect(reject.result.current.isSuccess).toBe(true));
    expect(m.post).toHaveBeenLastCalledWith("/api/v1/admin/skills/review-requests/r2/reject", {
      note: null,
    });
    reject.result.current.mutate({ requestId: "r3", note: "스크립트가 외부로 전송" });
    await waitFor(() =>
      expect(m.post).toHaveBeenLastCalledWith("/api/v1/admin/skills/review-requests/r3/reject", {
        note: "스크립트가 외부로 전송",
      }),
    );
    expect(r.keys().slice(0, 3)).toEqual(expected);

    const p = setup();
    const revoke = renderHook(() => useAdminSetPublishedVersion(), { wrapper: p.wrapper });
    revoke.result.current.mutate({ skillId: "s1", versionId: null });
    await waitFor(() => expect(revoke.result.current.isSuccess).toBe(true));
    expect(m.post).toHaveBeenLastCalledWith("/api/v1/admin/skills/s1/published-version", {
      version_id: null,
    });
    expect(p.keys()).toEqual(expected);
  });

  it("useAdminUpdateSkill은 승격 version_id를 본문에 싣는다", async () => {
    const { wrapper, keys } = setup();
    m.patch.mockResolvedValue({});
    const { result } = renderHook(() => useAdminUpdateSkill(), { wrapper });
    result.current.mutate({ skillId: "s1", body: { visibility: "system", version_id: "v9" } });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(m.patch).toHaveBeenCalledWith("/api/v1/admin/skills/s1", {
      visibility: "system",
      version_id: "v9",
    });
    expect(keys()).toEqual([["admin-skills"], ["admin-skill-reviews"], ["skills"]]);
  });

  it("검토용 파일 트리·원문은 관리자 검토 엔드포인트를 쓴다(P0-03)", async () => {
    const { wrapper } = setup();
    m.get.mockResolvedValue([]);
    m.getText.mockResolvedValue("print(1)");
    const files = renderHook(() => useAdminReviewVersionFiles("v1"), { wrapper });
    await waitFor(() => expect(files.result.current.isSuccess).toBe(true));
    expect(m.get).toHaveBeenCalledWith("/api/v1/admin/skills/versions/v1/files");
    expect(renderHook(() => useAdminReviewVersionFiles(null), { wrapper }).result.current.fetchStatus).toBe(
      "idle",
    );

    await expect(fetchAdminReviewFile("v1", "scripts/a b.py")).resolves.toBe("print(1)");
    expect(m.getText).toHaveBeenCalledWith("/api/v1/admin/skills/versions/v1/files/scripts/a%20b.py");
  });
});
