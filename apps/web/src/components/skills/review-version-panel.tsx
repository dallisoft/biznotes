"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchAdminReviewFile, useAdminReviewVersionFiles } from "@/hooks/use-skills";
import type { SkillFile } from "@/lib/skill-types";

const TEXT_MEDIA = /^(text\/|application\/(json|x-yaml|yaml|xml|javascript|toml|x-sh|x-python))/;

/** 원문을 텍스트로 보여 줄 수 있는 파일인가 — 검토 엔드포인트는 항상 text/plain으로 준다. */
export function isTextLike(file: Pick<SkillFile, "media_type" | "role">): boolean {
  return file.role === "script" || TEXT_MEDIA.test(file.media_type ?? "");
}

/**
 * 승인 전에 열람해야 하는 파일. 조직 공개·기본 제공 승격은 지시문(SKILL.md)을, 스크립트 실행 허가는
 * 실행될 `scripts/` 원문 전부를 본다(코어 threat-model-runtime T1·threat-model-scripts §승인).
 */
export function requiredReviewPaths(files: Pick<SkillFile, "path">[], kind: string): string[] {
  if (kind === "scripts") {
    const scripts = files.map((f) => f.path).filter((p) => p.startsWith("scripts/"));
    if (scripts.length > 0) return scripts;
  }
  return files.some((f) => f.path === "SKILL.md") ? ["SKILL.md"] : [];
}

/**
 * 관리자 검토 상세 — 검토 대상 version의 파일 트리와 원문을 연다(계획 P0-03). 승인 버튼은
 * 필수 파일을 모두 연 뒤에만 켜진다: 내용 확인 없이 코드 실행·조직 공개를 승인하지 않게 한다.
 */
export function ReviewVersionPanel({
  versionId,
  kind,
  onReviewed,
}: {
  versionId: string;
  kind: string;
  onReviewed: (reviewed: boolean) => void;
}) {
  const files = useAdminReviewVersionFiles(versionId);
  const [opened, setOpened] = useState<Set<string>>(new Set());
  const [current, setCurrent] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const tree = useMemo(() => files.data ?? [], [files.data]);
  const required = useMemo(() => requiredReviewPaths(tree, kind), [tree, kind]);

  const open = useCallback(
    async (file: SkillFile) => {
      setCurrent(file.path);
      setError(null);
      if (!isTextLike(file)) {
        setContent(null);
        setOpened((prev) => new Set(prev).add(file.path));
        return;
      }
      try {
        setContent(await fetchAdminReviewFile(versionId, file.path));
        setOpened((prev) => new Set(prev).add(file.path));
      } catch (e) {
        setContent(null);
        setError(e instanceof Error ? e.message : "파일을 열지 못했습니다");
      }
    },
    [versionId],
  );

  // 지시문부터 연다 — 대부분의 검토는 SKILL.md 한 장으로 끝난다
  const skillMd = tree.find((f) => f.path === "SKILL.md");
  useEffect(() => {
    if (skillMd && current === null) void open(skillMd);
  }, [skillMd, current, open]);

  // 확인할 파일이 없는 version(빈 트리)은 열람할 것이 없으므로 승인을 막지 않는다
  const reviewed =
    files.isSuccess &&
    required.every((p) => opened.has(p)) &&
    (required.length > 0 || tree.length === 0);
  useEffect(() => onReviewed(reviewed), [reviewed, onReviewed]);

  if (files.isPending) return <Skeleton className="h-24" />;
  if (files.isError) {
    return <p className="text-xs text-destructive">파일 목록을 불러오지 못했습니다.</p>;
  }

  return (
    <div className="grid gap-3 md:grid-cols-[14rem_1fr]" data-testid="review-version-panel">
      <ul className="space-y-1 text-xs">
        {tree.map((f) => (
          <li key={f.path}>
            <button
              type="button"
              onClick={() => void open(f)}
              className={`flex w-full items-center gap-1 rounded px-1.5 py-1 text-left font-mono hover:bg-accent ${
                current === f.path ? "bg-accent" : ""
              }`}
            >
              <span className="truncate">{f.path}</span>
              {required.includes(f.path) && (
                <Badge variant={opened.has(f.path) ? "secondary" : "outline"}>
                  {opened.has(f.path) ? "확인" : "필수"}
                </Badge>
              )}
            </button>
          </li>
        ))}
      </ul>
      <div className="min-w-0">
        {error ? (
          <p className="text-xs text-destructive">{error}</p>
        ) : current && content === null ? (
          <p className="text-xs text-muted-foreground">바이너리 파일 — 원문 미리보기가 없습니다.</p>
        ) : current ? (
          <pre
            className="max-h-80 overflow-auto rounded border bg-muted/40 p-2 text-xs whitespace-pre-wrap"
            data-testid="review-file-content"
          >
            {content}
          </pre>
        ) : (
          <p className="text-xs text-muted-foreground">파일을 선택하세요.</p>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          {reviewed
            ? "필수 파일을 모두 확인했습니다 — 승인할 수 있습니다."
            : `승인 전 확인할 파일: ${required.join(", ") || "(없음)"}`}
        </p>
      </div>
    </div>
  );
}
