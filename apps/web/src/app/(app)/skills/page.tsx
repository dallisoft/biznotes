"use client";

import { Blocks, Upload } from "lucide-react";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { PageContainer } from "@/components/layout/page-container";
import { Toolbar, ToolbarSearch } from "@/components/layout/toolbar";
import { PageHeader } from "@/components/shell/page-header";
import { CreateSkillDialog } from "@/components/skills/create-skill-dialog";
import { SkillCard } from "@/components/skills/skill-card";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { EmptyState, FilteredEmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { LoadingState } from "@/components/ui/loading-state";
import {
  useImportSkillZip,
  useSkillCatalog,
  useSkills,
  useSkillZipCandidates,
} from "@/hooks/use-skills";
import { notifyError, notifySuccess } from "@/lib/notify";
import { catalogStatus } from "@/lib/skill-catalog";
import type { SkillImportCandidate, SkillReport } from "@/lib/skill-types";

export default function SkillsPage() {
  const [q, setQ] = useState("");
  const query = q.trim();
  const skills = useSkills({ q: query || undefined });
  const catalog = useSkillCatalog();
  const catalogEntries = catalog.data?.enabled ? catalog.data.entries : undefined;
  const importZip = useImportSkillZip();
  const inspectZip = useSkillZipCandidates();
  const router = useRouter();
  const fileInput = useRef<HTMLInputElement>(null);
  const [pendingImportFile, setPendingImportFile] = useState<File | null>(null);
  const [importCandidates, setImportCandidates] = useState<SkillImportCandidate[]>([]);

  function importFile(file: File, selectedSkillPath?: string) {
    importZip.mutate({ file, selectedSkillPath }, {
      onSuccess: (s) => {
        const report = s.import_report as SkillReport | undefined;
        const standardErrors = report?.standard?.errors?.length ?? 0;
        const notes = [
          standardErrors > 0 ? `표준 위반 ${standardErrors}건(보고만)` : null,
          report?.root_mismatch ? "루트 폴더 이름이 스킬 이름과 다릅니다" : null,
          report?.policy?.warnings?.some((w) => w.code === "license_restricts_redistribution")
            ? "라이선스에 재배포 제한 표현이 있습니다"
            : null,
        ].filter(Boolean);
        notifySuccess(`‘${s.name}’ 스킬을 가져왔습니다`, {
          description: notes.length > 0 ? notes.join(" · ") : undefined,
        });
        router.push(`/skills/${s.id}/edit`);
      },
      onError: (err) => notifyError("스킬 가져오기를 완료", err),
    });
  }

  function onImport(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    inspectZip.mutate(file, {
      onSuccess: ({ candidates }) => {
        if (candidates.length <= 1) {
          importFile(file);
          return;
        }
        setPendingImportFile(file);
        setImportCandidates(candidates);
      },
      onError: (err) => notifyError("ZIP 파일을 검사", err),
    });
  }

  const list = skills.data ?? [];

  return (
    <div className="h-full overflow-y-auto">
      <PageContainer width="wide">
        <PageHeader
          title="스킬"
          description="파일로 정의한 에이전트 작업(Agent Skill)을 만들고 편집해 조직에 공유합니다"
          actions={
            <>
              <Button
                variant="outline"
                className="coarse:h-11"
                onClick={() => fileInput.current?.click()}
                disabled={importZip.isPending || inspectZip.isPending}
              >
                <Upload aria-hidden />
                ZIP 가져오기
              </Button>
              <CreateSkillDialog />
            </>
          }
        />

        <Toolbar>
          <ToolbarSearch
            label="스킬 검색"
            placeholder="이름·설명으로 검색"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </Toolbar>

        {skills.isPending ? (
          <LoadingState variant="cards" label="스킬 목록을 불러오는 중" />
        ) : skills.isError ? (
          <ErrorState
            title="스킬 목록을 불러오지 못했습니다"
            onRetry={() => void skills.refetch()}
            retrying={skills.isFetching}
          />
        ) : list.length === 0 ? (
          query ? (
            <FilteredEmptyState onClearFilters={() => setQ("")} />
          ) : (
            <EmptyState
              icon={<Blocks />}
              title="아직 스킬이 없습니다"
              description="「새 스킬」로 만들거나 ZIP 파일을 가져오세요."
            />
          )
        ) : (
          <ul
            data-testid="skill-grid"
            className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(260px,1fr))]"
          >
            {list.map((s) => (
              <li key={s.id}>
                <SkillCard skill={s} catalog={catalogStatus(s, catalogEntries)} />
              </li>
            ))}
          </ul>
        )}
      </PageContainer>
      {/* 숨은 파일 입력은 페이지 래퍼 밖에 둔다 — 목록 「다시 시도」가 성공하면 ErrorState가 래퍼 안 첫 포커스 가능
          요소로 포커스를 옮기는데, 그 자리가 보이지 않는 입력이면 포커스가 사라진다(P2 회고 HCI-P2-2) */}
      <input
        ref={fileInput}
        type="file"
        accept=".zip"
        className="hidden"
        onChange={onImport}
      />
      <Dialog
        open={pendingImportFile !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingImportFile(null);
            setImportCandidates([]);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>가져올 스킬 선택</DialogTitle>
            <DialogDescription>
              이 ZIP에는 SKILL.md가 여러 개 있습니다. 하나만 골라 가져옵니다.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-2">
            {importCandidates.map((candidate) => (
              <Button
                key={candidate.path}
                variant="outline"
                className="justify-start font-mono coarse:h-11"
                disabled={importZip.isPending}
                onClick={() => {
                  if (!pendingImportFile) return;
                  importFile(pendingImportFile, candidate.path);
                  setPendingImportFile(null);
                  setImportCandidates([]);
                }}
              >
                {candidate.path}
              </Button>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
