import type { components } from "@biznotes/shared-types";
import { SKILL_VISIBILITY_LABEL } from "@/lib/labels";

type Schemas = components["schemas"];

export type SkillVisibility = "private" | "org" | "system";
export type SkillStatus = "draft" | "published" | "archived";

export type Skill = Schemas["SkillOut"];

export type SkillVersion = Schemas["SkillVersionOut"];

export type SkillFile = Schemas["SkillFileOut"];

export type SkillFrontmatter = Schemas["SkillFrontmatterOut"];

export type SkillCatalog = Schemas["SkillCatalogOut"];

export type SkillCatalogEntry = Schemas["SkillCatalogEntryOut"];

export type SkillImport = Schemas["SkillImportOut"];

/** #259 GitHub 저장소 스킬 설치 — 채팅 확인 카드의 「설치」 요청·결과. */
export type SkillInstallGithubRequest = Schemas["SkillInstallGithubRequest"];
export type SkillInstallGithubResult = Schemas["SkillInstallGithubOut"];
export type SkillInstallItem = Schemas["SkillInstallItemOut"];

export type SkillImportCandidate = Schemas["SkillImportCandidateOut"];

export type SkillReviewRequest = Schemas["SkillReviewRequestOut"];

export type SkillVersionPublish = Schemas["SkillVersionPublishOut"];

export type AdminSkill = Schemas["AdminSkillOut"];

/** 네 층 검증 보고서(biznotes-core 표준 계획 D5) — JSONB라 스키마상 느슨하다. */
export interface SkillReportFinding {
  code: string;
  message: string;
}

export interface SkillReport {
  standard?: { profile?: string | null; errors?: string[] };
  portability?: { warnings?: SkillReportFinding[] };
  policy?: {
    errors?: SkillReportFinding[];
    warnings?: SkillReportFinding[];
    license?: { field?: string | null; files?: string[] };
  };
  capability?: { runtime_profile?: string | null; warnings?: SkillReportFinding[] };
  /** 가져오기 보고서에만 있다 */
  root_name?: string | null;
  root_mismatch?: SkillReportFinding | null;
}

/** 검토 요청 종류 라벨 — 용어는 plan §6.3.1(「빌트인」 → 「기본 제공」, P5 리뷰 P5-06). */
export const REVIEW_KIND_LABEL: Record<string, string> = {
  expose_org: "조직 공개",
  expose_system: "기본 제공 승격",
  scripts: "스크립트 실행 허가",
};

/**
 * 검토 대기 배너 문구 — 승인이 실제로 무엇을 하는지 kind·origin별로 말한다(계획 P0-16).
 * `scripts`는 실행 허가일 뿐 공개 범위를 바꾸지 않고, authored 스킬의 `expose_org`는 승인해도
 * 공개하지 않는다(코어 `exposure.approval_publishes`, S-HCI-12 — 채팅 카탈로그 등재 요청).
 */
export function reviewPendingMessage(kind: string, origin: string | null | undefined): string {
  switch (kind) {
    case "scripts":
      return "승인되면 이 version의 번들 스크립트를 실행할 수 있습니다(공개 범위는 바뀌지 않습니다).";
    case "expose_system":
      return "승인되면 기본 제공 스킬로 승격되어 모든 사용자에게 공개됩니다.";
    case "expose_org":
      return origin === "authored"
        ? "승인되면 다른 사용자의 채팅 카탈로그에 등재됩니다(공개 자체는 소유자의 공개로 이뤄집니다)."
        : "승인되면 조직에 공개됩니다.";
    default:
      return "관리자 승인 뒤에 반영됩니다.";
  }
}

export interface SkillFreezeResult {
  version_id: string;
  version: number;
  wrapper_agent_id: string;
  status: string;
}

export interface SkillValidationResult {
  ok: boolean;
  report?: SkillReport;
  reason?: string;
  message?: string;
  name?: string;
  description?: string;
  file_count?: number;
  manifest?: Record<string, unknown>;
  references?: string[];
}

/**
 * 스킬 공개 범위 라벨 — `system`은 「기본 제공」(plan §6.3.1, P5 리뷰 P5-06). 사전은 `lib/labels`의
 * `SKILL_VISIBILITY_LABEL` 하나다(P4 사용자 스킬 화면과 P5 관리 스킬 화면이 같은 사전 — P6에서 중복 정의를 합침).
 */
export const VISIBILITY_LABEL: Record<SkillVisibility, string> = SKILL_VISIBILITY_LABEL;
