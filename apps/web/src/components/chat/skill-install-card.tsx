"use client";

import { AlertTriangle, CheckCircle2, Download, XCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { useInstallGithubSkills } from "@/hooks/use-skills";
import { ApiError } from "@/lib/api-client";
import type { SkillInstallCandidate, SkillInstallOffer } from "@/lib/chat-types";
import type { SkillInstallItem } from "@/lib/skill-types";

/** 후보 점검 사유 코드(코어 검증 사유) → 짧은 표시 문구. 모르는 코드는 코드 그대로 보인다. */
const CANDIDATE_ERROR_LABEL: Record<string, string> = {
  symlink: "심볼릭 링크가 있어 설치할 수 없음",
  too_many_files: "파일 수 한도 초과",
  file_too_large: "파일 크기 한도 초과",
  package_too_large: "패키지 크기 한도 초과",
  unsupported_path: "지원하지 않는 폴더 이름",
};

export function candidateErrorLabel(code: string): string {
  return CANDIDATE_ERROR_LABEL[code] ?? code;
}

/** 출처 표기 `owner/repo@sha7`(AC-1). */
export function offerSourceLabel(offer: Pick<SkillInstallOffer, "owner" | "repo" | "sha">): string {
  return `${offer.owner}/${offer.repo}@${offer.sha.slice(0, 7)}`;
}

/** 라이선스 표시 — 없으면 「라이선스 없음」(AC-9). 라이선스로 막지 않는다. */
export function offerLicenseLabel(offer: Pick<SkillInstallOffer, "license">): string {
  const license = offer.license;
  if (!license) return "라이선스 없음";
  if (license.spdx_id && license.spdx_id !== "NOASSERTION") return license.spdx_id;
  return license.name ?? "라이선스 확인 필요";
}

/** 처음 체크할 경로 — 사본이 아니고 점검을 통과한 후보만, 설치 상한까지. */
export function defaultSelection(offer: SkillInstallOffer): string[] {
  return offer.candidates
    .filter((c) => c.copy_of === null && c.ok)
    .slice(0, offer.max_paths)
    .map((c) => c.path);
}

function failedItems(error: unknown): SkillInstallItem[] {
  if (!(error instanceof ApiError)) return [];
  const detail = (error.body as { detail?: { items?: unknown } } | undefined)?.detail;
  return Array.isArray(detail?.items) ? (detail.items as SkillInstallItem[]) : [];
}

function CandidateRow({
  candidate,
  checked,
  locked,
  onToggle,
}: {
  candidate: SkillInstallCandidate;
  checked: boolean;
  locked: boolean;
  onToggle: (checked: boolean) => void;
}) {
  const label = candidate.name ?? (candidate.path || "(저장소 루트)");
  return (
    <li className="flex gap-2 py-2" data-testid="skill-install-candidate" data-path={candidate.path}>
      <Checkbox
        aria-label={candidate.copy_of === null ? `${label} 설치` : `${label} (${candidate.path}) 설치`}
        checked={checked}
        disabled={locked || !candidate.ok}
        onCheckedChange={(value) => onToggle(value === true)}
        className="mt-0.5"
      />
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-sm font-medium">{label}</span>
          <code className="truncate text-xs text-muted-foreground">{candidate.path || "/"}</code>
          {candidate.reinstall && (
            <Badge variant="secondary">
              다시 설치 — v{candidate.reinstall.latest_version ?? 0} 다음 version
            </Badge>
          )}
          {candidate.has_scripts && <Badge variant="outline">번들 스크립트</Badge>}
        </div>
        {candidate.description && (
          <p className="line-clamp-2 text-xs text-muted-foreground">{candidate.description}</p>
        )}
        {!candidate.ok && (
          <p className="flex items-center gap-1 text-xs text-destructive-text">
            <XCircle className="size-3.5 shrink-0" aria-hidden />
            설치할 수 없음: {candidate.errors.map(candidateErrorLabel).join(", ")}
          </p>
        )}
        {candidate.warnings.length > 0 && (
          <ul className="space-y-0.5" aria-label={`${label} 경고`}>
            {candidate.warnings.map((w, i) => (
              <li
                key={`${w.code}-${i}`}
                className="flex gap-1 text-xs text-muted-foreground"
                data-code={w.code}
              >
                <AlertTriangle className="mt-0.5 size-3 shrink-0 text-warning-text" aria-hidden />
                <span>{w.message}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </li>
  );
}

function ResultList({ items }: { items: SkillInstallItem[] }) {
  return (
    <ul className="space-y-1 text-sm" data-testid="skill-install-result">
      {items.map((item) => (
        <li key={item.path} className="flex flex-wrap items-center gap-1.5">
          {item.status === "failed" ? (
            <>
              <XCircle className="size-4 text-destructive-text" aria-hidden />
              <code className="text-xs">{item.path || "/"}</code>
              <span className="text-destructive-text">{item.message}</span>
            </>
          ) : (
            <>
              <CheckCircle2 className="size-4 text-success-text" aria-hidden />
              <Link href={`/skills/${item.skill_id}`} className="font-medium underline-offset-4 hover:underline">
                {item.name}
              </Link>
              <span className="text-muted-foreground">
                v{item.version} {item.status === "reinstalled" ? "(새 version)" : "설치됨"}
              </span>
              {item.has_scripts && (
                <span className="text-xs text-muted-foreground">
                  — 스크립트 실행 허가 요청은 스킬 화면에서 할 수 있습니다
                </span>
              )}
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * #259 GitHub 저장소 스킬 설치 확인 카드. 체크한 후보만 「설치」 버튼으로 설치한다 — 버튼을 누르기
 * 전에는 아무것도 만들지 않는다. 같은 이름의 사본은 접어 두고 체크하지 않는다.
 * 스킬 화면의 URL 가져오기(#60)에서도 다시 쓸 수 있게 채팅 상태에 기대지 않는다.
 */
export function SkillInstallCard({ offer }: { offer: SkillInstallOffer }) {
  const install = useInstallGithubSkills();
  const [selected, setSelected] = useState<Set<string>>(() => new Set(defaultSelection(offer)));
  const representatives = offer.candidates.filter((c) => c.copy_of === null);
  const copies = offer.candidates.filter((c) => c.copy_of !== null);
  const done = install.isSuccess;
  const locked = install.isPending || done;
  const overLimit = selected.size > offer.max_paths;
  const failed = install.isError ? failedItems(install.error) : [];

  function toggle(path: string, checked: boolean) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (checked) next.add(path);
      else next.delete(path);
      return next;
    });
  }

  function submit() {
    install.mutate({
      owner: offer.owner,
      repo: offer.repo,
      sha: offer.sha,
      archive_sha256: offer.archive_sha256,
      archive_root: offer.archive_root,
      // 카드의 후보 순서대로 보낸다
      paths: offer.candidates.map((c) => c.path).filter((p) => selected.has(p)),
    });
  }

  const row = (c: SkillInstallCandidate) => (
    <CandidateRow
      key={c.path}
      candidate={c}
      checked={selected.has(c.path)}
      locked={locked}
      onToggle={(checked) => toggle(c.path, checked)}
    />
  );

  return (
    <section
      aria-label="스킬 설치 확인"
      data-testid="skill-install-card"
      className="mr-auto w-full max-w-[90%] space-y-3 rounded-xl border bg-card p-4 text-card-foreground"
    >
      <header className="space-y-1">
        <h3 className="flex items-center gap-1.5 text-sm font-semibold">
          <Download className="size-4" aria-hidden />
          GitHub 스킬 설치
        </h3>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
          <dt className="text-muted-foreground">출처</dt>
          <dd>
            <a
              href={`https://github.com/${offer.owner}/${offer.repo}/tree/${offer.sha}`}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono underline-offset-4 hover:underline"
              data-testid="skill-install-source"
            >
              {offerSourceLabel(offer)}
            </a>
            <span className="text-muted-foreground"> ({offer.ref})</span>
          </dd>
          <dt className="text-muted-foreground">라이선스</dt>
          <dd data-testid="skill-install-license">{offerLicenseLabel(offer)}</dd>
        </dl>
      </header>

      <ul className="divide-y">{representatives.map(row)}</ul>

      {copies.length > 0 && (
        <details className="text-xs" data-testid="skill-install-copies">
          <summary className="cursor-pointer text-muted-foreground">
            같은 이름의 사본 {copies.length}개 (다른 도구용 사본 — 기본으로 설치하지 않습니다)
          </summary>
          <ul className="divide-y">{copies.map(row)}</ul>
        </details>
      )}

      {offer.truncated > 0 && (
        <p className="text-xs text-muted-foreground">
          후보가 많아 {offer.truncated}개는 보이지 않습니다. 경로를 좁힌 주소(/tree/&lt;ref&gt;/&lt;경로&gt;)로 다시
          요청하세요.
        </p>
      )}

      {done && install.data ? (
        <ResultList items={install.data.items} />
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <Button
            size="sm"
            onClick={submit}
            disabled={locked || selected.size === 0 || overLimit}
            data-testid="skill-install-submit"
          >
            {install.isPending ? "설치 중…" : `설치 (${selected.size})`}
          </Button>
          <span className="text-xs text-muted-foreground">
            {overLimit
              ? `한 번에 ${offer.max_paths}개까지 설치할 수 있습니다.`
              : "누르기 전에는 아무것도 설치하지 않습니다. 설치한 스킬은 나만 씁니다."}
          </span>
        </div>
      )}
      {install.isError &&
        (failed.length > 0 ? (
          <ResultList items={failed} />
        ) : (
          <p role="alert" className="text-sm text-destructive-text">
            설치하지 못했습니다 — {install.error.message}
          </p>
        ))}
    </section>
  );
}
