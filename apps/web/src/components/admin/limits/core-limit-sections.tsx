"use client";

import { NotebookSourceLimitSection } from "@/components/admin/limits/notebook-source-limit-section";
import { RateLimitsSection } from "@/components/admin/limits/rate-limits-section";
import { SourceUploadSection } from "@/components/admin/limits/source-upload-section";
import { UsageDefaultsSection } from "@/components/admin/limits/usage-defaults-section";

/**
 * 관리 › 사용 제한의 코어 항목(코어 v0.26.0 #386·#387·#388·v0.30.0 #445, 이슈 #228 B·#281) — 「요청당 한도」 아래에 둔다.
 * 섹션마다 따로 불러오고 따로 저장한다(값은 코어 `system_settings` 재정의, 지우면 env 값).
 */
export function CoreLimitSections() {
  return (
    <>
      <RateLimitsSection />
      <UsageDefaultsSection />
      <SourceUploadSection />
      <NotebookSourceLimitSection />
    </>
  );
}
