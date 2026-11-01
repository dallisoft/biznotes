"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api-client";
import { API_BASE_URL } from "@/lib/api";
import type {
  AdminSkill,
  Skill,
  SkillCatalog,
  SkillFile,
  SkillFrontmatter,
  SkillFreezeResult,
  SkillImport,
  SkillImportCandidate,
  SkillInstallGithubRequest,
  SkillInstallGithubResult,
  SkillReviewRequest,
  SkillValidationResult,
  SkillVersion,
  SkillVersionPublish,
} from "@/lib/skill-types";

/** FastAPI `{path:path}`에 넘길 상대 파일 경로를 URL path segment 단위로 인코딩한다. */
export function encodeSkillFilePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

export function useSkills(params?: { q?: string; visibility?: string }) {
  const qs = new URLSearchParams();
  if (params?.q) qs.set("q", params.q);
  if (params?.visibility) qs.set("visibility", params.visibility);
  const suffix = qs.toString() ? `?${qs}` : "";
  return useQuery<Skill[], ApiError>({
    queryKey: ["skills", params?.q ?? "", params?.visibility ?? ""],
    queryFn: () => api.get<Skill[]>(`/api/v1/skills${suffix}`),
    staleTime: 15_000,
  });
}

export function useSkill(skillId: string | null) {
  return useQuery<Skill, ApiError>({
    queryKey: ["skill", skillId],
    queryFn: () => api.get<Skill>(`/api/v1/skills/${skillId}`),
    enabled: !!skillId,
  });
}

export function useSkillVersions(skillId: string | null) {
  return useQuery<SkillVersion[], ApiError>({
    queryKey: ["skill-versions", skillId],
    queryFn: () => api.get<SkillVersion[]>(`/api/v1/skills/${skillId}/versions`),
    enabled: !!skillId,
  });
}

export function useSkillDraftFiles(skillId: string | null) {
  return useQuery<SkillFile[], ApiError>({
    queryKey: ["skill-files", skillId],
    queryFn: () => api.get<SkillFile[]>(`/api/v1/skills/${skillId}/files`),
    enabled: !!skillId,
  });
}

/**
 * draft 파일 내용 단건 로드. 서버가 UTF-8 텍스트로 판정한 파일(`text/plain`)만 문자열로,
 * 바이너리는 `null`로 돌려준다 — 편집기가 바이너리를 텍스트로 덮어쓰지 않게 한다.
 */
export async function fetchSkillFile(skillId: string, path: string): Promise<string | null> {
  const blob = await api.getBlob(`/api/v1/skills/${skillId}/files/${encodeSkillFilePath(path)}`);
  return blob.type.startsWith("text/plain") ? blob.text() : null;
}

export function useSkillFrontmatter(skillId: string | null) {
  return useQuery<SkillFrontmatter, ApiError>({
    queryKey: ["skill-frontmatter", skillId],
    queryFn: () => api.get<SkillFrontmatter>(`/api/v1/skills/${skillId}/frontmatter`),
    enabled: !!skillId,
  });
}

export function useCreateSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { name: string; description: string }) =>
      api.post<Skill>("/api/v1/skills", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useImportSkillZip() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      selectedSkillPath,
    }: {
      file: File;
      selectedSkillPath?: string;
    }) => {
      const fd = new FormData();
      fd.append("file", file);
      if (selectedSkillPath) fd.append("selected_skill_path", selectedSkillPath);
      // 응답에 네 층 보고서(`import_report`)가 실린다 — 표준 위반은 거부가 아니라 보고다.
      return api.postForm<SkillImport>("/api/v1/skills/import-zip", fd);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useSkillZipCandidates() {
  return useMutation({
    mutationFn: (file: File) => {
      const fd = new FormData();
      fd.append("file", file);
      return api.postForm<{ candidates: SkillImportCandidate[] }>(
        "/api/v1/skills/import-zip/candidates",
        fd,
      );
    },
  });
}

/**
 * #259 채팅 확인 카드의 「설치」 — 고른 경로를 요청자 private 스킬로 설치하고 version을 동결한다.
 * 하나도 설치하지 못하면 422이고 `ApiError.body.detail.items`에 경로별 사유가 있다.
 */
export function useInstallGithubSkills() {
  const qc = useQueryClient();
  return useMutation<SkillInstallGithubResult, ApiError, SkillInstallGithubRequest>({
    mutationFn: (body) => api.post<SkillInstallGithubResult>("/api/v1/skill-installs/github", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useUpdateSkill(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Partial<Pick<Skill, "name" | "description" | "tags" | "visibility">>) =>
      api.patch<Skill>(`/api/v1/skills/${skillId}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skill", skillId] });
      qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}

export function useDeleteSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (skillId: string) => api.delete<void>(`/api/v1/skills/${skillId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useWriteSkillFile(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ path, content }: { path: string; content: string }) =>
      api.put<void>(`/api/v1/skills/${skillId}/files/${encodeSkillFilePath(path)}`, { content }),
    onSuccess: (_result, variables) => {
      qc.invalidateQueries({ queryKey: ["skill-files", skillId] });
      if (variables.path === "SKILL.md") {
        qc.invalidateQueries({ queryKey: ["skill-frontmatter", skillId] });
      }
    },
  });
}

export function useWriteSkillBinaryFile(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ path, file }: { path: string; file: File }) =>
      // application/json은 기존 텍스트 write envelope와 구분할 수 없으므로 항상 raw로 보낸다.
      api.putBytes<void>(
        `/api/v1/skills/${skillId}/files/${encodeSkillFilePath(path)}`,
        file,
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skill-files", skillId] }),
  });
}

export function useWriteSkillFrontmatter(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      fields,
      sourceHash,
    }: {
      fields: Record<string, unknown>;
      sourceHash: string;
    }) =>
      api.put<SkillFrontmatter>(`/api/v1/skills/${skillId}/frontmatter`, {
        fields,
        source_hash: sourceHash,
      }),
    onSuccess: () => {
      // 원문 Textarea는 이 mutation이 pending인 동안 잠긴다. invalidate를 기다려야
      // refetch 중 잠금이 풀려 저장되지 않은 원문을 덮는 경합이 생기지 않는다.
      return Promise.all([
        qc.invalidateQueries({ queryKey: ["skill-frontmatter", skillId] }),
        qc.invalidateQueries({ queryKey: ["skill-file-content", skillId, "SKILL.md"] }),
        qc.invalidateQueries({ queryKey: ["skill", skillId] }),
        qc.invalidateQueries({ queryKey: ["skills"] }),
      ]);
    },
  });
}

export function useValidateSkillFrontmatter(skillId: string) {
  return useMutation({
    mutationFn: ({
      fields,
      sourceHash,
    }: {
      fields: Record<string, unknown>;
      sourceHash: string;
    }) =>
      api.post<SkillValidationResult>(`/api/v1/skills/${skillId}/frontmatter/validate`, {
        fields,
        source_hash: sourceHash,
      }),
  });
}

export function useDeleteSkillFile(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (path: string) =>
      api.delete<void>(`/api/v1/skills/${skillId}/files/${encodeSkillFilePath(path)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skill-files", skillId] }),
  });
}

export function useValidateSkill(skillId: string) {
  return useMutation({
    mutationFn: () => api.post<SkillValidationResult>(`/api/v1/skills/${skillId}/validate`),
  });
}

export function useFreezeSkill(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { changelog?: string }) =>
      api.post<SkillFreezeResult>(`/api/v1/skills/${skillId}/freeze`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skill", skillId] });
      qc.invalidateQueries({ queryKey: ["skill-versions", skillId] });
      qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}

export function usePublishSkill(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { changelog?: string }) =>
      api.post<SkillFreezeResult>(`/api/v1/skills/${skillId}/publish`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skill", skillId] });
      qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}

/** version publish — 200 `published` 또는 202 `review_pending`(관리자 검토 대기). */
export function usePublishSkillVersion(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (versionId: string) =>
      api.post<SkillVersionPublish>(`/api/v1/skills/${skillId}/versions/${versionId}/publish`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skill", skillId] });
      qc.invalidateQueries({ queryKey: ["skill-versions", skillId] });
      qc.invalidateQueries({ queryKey: ["skill-reviews", skillId] });
      qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}

/** 이 사용자의 채팅·에이전트 스킬 카탈로그(biznotes-core D6-1). 가려진 항목은 `shadowed_by`가 있다. */
export function useSkillCatalog() {
  return useQuery<SkillCatalog, ApiError>({
    queryKey: ["skill-catalog"],
    queryFn: () => api.get<SkillCatalog>("/api/v1/skills/catalog"),
  });
}

/**
 * 소유자의 검토 요청 — `expose_org`는 authored 스킬이면 "채팅 카탈로그 등재" 요청이다(S-HCI-12).
 * `scripts`는 그 version의 번들 스크립트 실행 허가 요청이다(biznotes-core 표준 계획 D7·D8).
 */
export function useRequestSkillReview(skillId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ versionId, kind }: { versionId: string; kind: "expose_org" | "scripts" }) =>
      api.post<SkillReviewRequest>(
        `/api/v1/skills/${skillId}/versions/${versionId}/review-requests`,
        { kind },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skill-reviews", skillId] });
      qc.invalidateQueries({ queryKey: ["skill-catalog"] });
    },
  });
}

export function useSkillReviewRequests(skillId: string | null, enabled = true) {
  return useQuery<SkillReviewRequest[], ApiError>({
    queryKey: ["skill-reviews", skillId],
    queryFn: () => api.get<SkillReviewRequest[]>(`/api/v1/skills/${skillId}/review-requests`),
    enabled: !!skillId && enabled,
  });
}

/** 내보내기 URL — 브라우저 다운로드는 쿠키 인증으로 직접 연다. */
export function skillExportUrl(
  versionId: string,
  options?: { layout?: "wrapped" | "flat"; portable?: boolean },
): string {
  const qs = new URLSearchParams();
  if (options?.layout) qs.set("layout", options.layout);
  if (options?.portable) qs.set("portable", "true");
  const suffix = qs.toString() ? `?${qs}` : "";
  return `${API_BASE_URL}/api/v1/skills/versions/${versionId}/export${suffix}`;
}

export function useCreateSkillFromArtifact() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { run_id: string; artifact_name: string }) =>
      api.post<{ skill_id: string; status: string }>("/api/v1/skills/from-artifact", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

export function useCloneSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (skillId: string) =>
      api.post<{ skill_id: string; status: string }>(`/api/v1/skills/${skillId}/clone`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["skills"] }),
  });
}

// ---------- 어드민 ----------

export function useAdminSkills() {
  return useQuery<AdminSkill[], ApiError>({
    queryKey: ["admin-skills"],
    queryFn: () => api.get<AdminSkill[]>("/api/v1/admin/skills"),
  });
}

export function useAdminReviewRequests(status = "pending") {
  return useQuery<SkillReviewRequest[], ApiError>({
    queryKey: ["admin-skill-reviews", status],
    queryFn: () =>
      api.get<SkillReviewRequest[]>(`/api/v1/admin/skills/review-requests?status=${status}`),
  });
}

/** 검토 대상 version의 파일 트리 — 비공개 version도 관리자는 검토용으로 읽는다(코어 D8). */
export function useAdminReviewVersionFiles(versionId: string | null) {
  return useQuery<SkillFile[], ApiError>({
    queryKey: ["admin-review-files", versionId],
    queryFn: () => api.get<SkillFile[]>(`/api/v1/admin/skills/versions/${versionId}/files`),
    enabled: !!versionId,
  });
}

/**
 * 검토 대상 파일 원문(스크립트 포함). 서버는 항상 `text/plain`으로 돌려주므로 바이너리 여부는
 * 호출자가 파일 트리의 `media_type`으로 판단한다. 코어가 이 열람을 감사 로그
 * (`skill.review.read`)로 남긴다.
 */
export function fetchAdminReviewFile(versionId: string, path: string): Promise<string> {
  return api.getText(
    `/api/v1/admin/skills/versions/${versionId}/files/${encodeSkillFilePath(path)}`,
  );
}

function useAdminSkillMutation<TVars>(fn: (vars: TVars) => Promise<unknown>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-skills"] });
      qc.invalidateQueries({ queryKey: ["admin-skill-reviews"] });
      qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}

/** 검토 승인 — `expose_*`는 같은 트랜잭션에서 공개까지 끝낸다. */
export function useApproveReviewRequest() {
  return useAdminSkillMutation((requestId: string) =>
    api.post(`/api/v1/admin/skills/review-requests/${requestId}/approve`),
  );
}

export function useRejectReviewRequest() {
  return useAdminSkillMutation(({ requestId, note }: { requestId: string; note?: string }) =>
    api.post(`/api/v1/admin/skills/review-requests/${requestId}/reject`, { note: note ?? null }),
  );
}

/** 공개 version 지정·철회 — `null`은 철회이고 보류(exposure_hold)를 세운다. */
export function useAdminSetPublishedVersion() {
  return useAdminSkillMutation(
    ({ skillId, versionId }: { skillId: string; versionId: string | null }) =>
      api.post(`/api/v1/admin/skills/${skillId}/published-version`, { version_id: versionId }),
  );
}

export function useAdminUpdateSkill() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      skillId,
      body,
    }: {
      skillId: string;
      // 승격(→system, private→org)에는 공개할 version_id가 필요하다(관문 경로 3)
      body: { enabled?: boolean; visibility?: string; version_id?: string };
    }) => api.patch<AdminSkill>(`/api/v1/admin/skills/${skillId}`, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["admin-skills"] });
      qc.invalidateQueries({ queryKey: ["admin-skill-reviews"] });
      qc.invalidateQueries({ queryKey: ["skills"] });
    },
  });
}
