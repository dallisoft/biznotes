"use client";

import { useQuery } from "@tanstack/react-query";
import { Blocks, Download, StickyNote } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { FlashcardViewer } from "@/components/agents/flashcard-viewer";
import { InfographicViewer } from "@/components/agents/infographic-viewer";
import { MindmapViewer } from "@/components/agents/mindmap-viewer";
import { QuizViewer } from "@/components/agents/quiz-viewer";
import { SlideDeckViewer } from "@/components/agents/slide-deck/slide-deck-viewer";
import { SlideOutlineReview } from "@/components/agents/slide-deck/slide-outline-review";
import { MarkdownContent } from "@/components/chat/markdown-content";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { fetchArtifact, fetchArtifactObjectUrl } from "@/hooks/use-agents";
import { useCreateNote } from "@/hooks/use-notes";
import { useCreateSkillFromArtifact } from "@/hooks/use-skills";
import type { AgentRun, ArtifactRef } from "@/lib/agent-types";
import { isTextyArtifact, resolveArtifactViewer } from "@/lib/artifact-viewers";
import { boundedPrettyJson } from "@/lib/mindmap/bounded";
import { qk } from "@/lib/query-keys";

// JSON 원본 보기·메모 저장도 산출물 소비 경계(bounded)를 거친다 — 상한·깊이 초과 본문은 파싱하지 않는다(T4).
const formatJson = boundedPrettyJson;

/**
 * 실행 산출물 본문 — 실행 상세(RunDetailView)의 「결과」 탭이 우측 패널 안에 그린다(모달 아님).
 * 본문은 남은 높이를 채우며 세로 스크롤하고, 「메모로 저장」 등 동작은 아래에 고정한다.
 */
export function ArtifactContent({
  run,
  artifact,
  notebookId,
  canWrite,
}: {
  run: AgentRun;
  artifact: ArtifactRef;
  notebookId: string;
  /** 노트북 write 이상 — 메모 생성은 write 이상이라 read 공유자에게는 숨긴다(#174). */
  canWrite: boolean;
}) {
  const viewer = resolveArtifactViewer(artifact.name, artifact.content_type);
  const isAudio = viewer === "audio";
  const isImage = viewer === "image";
  const isBinary = isAudio || isImage;
  const content = useQuery({
    queryKey: qk.artifact.body(run.id, artifact.name),
    queryFn: () => fetchArtifact(run.id, artifact.name),
    enabled: !isBinary,
    // 성공 run의 산출물은 바뀌지 않는다 — 스튜디오 항목 제목 조회와 같은 키·캐시를 쓴다(AC-1.10)
    staleTime: Infinity,
  });
  const audioUrl = useQuery({
    queryKey: ["artifact-audio", run.id, artifact.name],
    queryFn: () => fetchArtifactObjectUrl(run.id, artifact.name),
    enabled: isAudio,
    staleTime: Infinity,
  });
  // 슬라이드 뷰어(useSlideImages)와 같은 키 — 같은 run의 삽화를 한 번만 받는다
  const imageUrl = useQuery({
    queryKey: ["artifact-image", run.id, artifact.name],
    queryFn: () => fetchArtifactObjectUrl(run.id, artifact.name),
    enabled: isImage,
    staleTime: Infinity,
    retry: 0,
  });
  const createNote = useCreateNote(notebookId);
  const createSkill = useCreateSkillFromArtifact();
  const router = useRouter();

  const isJson = artifact.content_type.includes("json");
  const isTexty = isTextyArtifact(artifact.name, artifact.content_type);
  // 스킬 생성 도우미 산출물 — 1클릭으로 draft 스킬 생성
  const isSkillPackage = artifact.name === "skill-package.json";

  function createSkillFromPackage() {
    createSkill.mutate(
      { run_id: run.id, artifact_name: artifact.name },
      {
        onSuccess: (r) => {
          toast.success("스킬 draft를 만들었습니다");
          router.push(`/skills/${r.skill_id}/edit`);
        },
        onError: (err) => toast.error(`생성 실패: ${err.message}`),
      },
    );
  }

  function saveAsNote() {
    if (!content.data) return;
    createNote.mutate(
      {
        title: `${run.agent_name} — ${artifact.name}`.slice(0, 255),
        content: isJson ? formatJson(content.data) : content.data,
        origin: "agent",
        origin_ref: run.id,
        artifact_name: artifact.name,
      },
      {
        onSuccess: () => toast.success("메모에 저장했습니다"),
        onError: (err) => toast.error(`저장 실패: ${err.message}`),
      },
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="artifact-content">
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <p className="mb-2 truncate text-xs text-muted-foreground">
          {artifact.name} · {artifact.content_type}
        </p>
        {isImage ? (
          imageUrl.isPending ? (
            <Skeleton className="h-60" />
          ) : imageUrl.isError ? (
            <p className="text-sm text-destructive">이미지를 불러오지 못했습니다</p>
          ) : (
            <div className="flex flex-col gap-2" data-testid="artifact-image">
              {/* eslint-disable-next-line @next/next/no-img-element -- 인증 요청으로 받은 blob URL */}
              <img src={imageUrl.data} alt={artifact.name} className="max-h-[70vh] w-full rounded-md object-contain" />
              <a
                href={imageUrl.data}
                download={artifact.name}
                className="inline-flex items-center gap-1 self-end text-xs text-primary hover:underline"
              >
                <Download className="size-3" />
                다운로드
              </a>
            </div>
          )
        ) : isAudio ? (
          audioUrl.isPending ? (
            <Skeleton className="h-16" />
          ) : audioUrl.isError ? (
            <p className="text-sm text-destructive">오디오를 불러오지 못했습니다</p>
          ) : (
            <div className="flex flex-col gap-2" data-testid="audio-player">
              <audio controls src={audioUrl.data} className="w-full" />
              <a
                href={audioUrl.data}
                download={artifact.name}
                className="inline-flex items-center gap-1 self-end text-xs text-primary hover:underline"
              >
                <Download className="size-3" />
                다운로드
              </a>
            </div>
          )
        ) : content.isPending ? (
          <Skeleton className="h-60" />
        ) : content.isError ? (
          <p className="text-sm text-destructive">불러오지 못했습니다</p>
        ) : viewer === "mindmap" ? (
          <MindmapViewer content={content.data} run={run} notebookId={notebookId} />
        ) : viewer === "flashcards" ? (
          <FlashcardViewer content={content.data} runId={run.id} artifactName={artifact.name} />
        ) : viewer === "quiz" ? (
          <QuizViewer content={content.data} />
        ) : viewer === "infographic" ? (
          <InfographicViewer content={content.data} />
        ) : viewer === "slide-deck" ? (
          <SlideDeckViewer content={content.data} run={run} />
        ) : viewer === "slide-outline" ? (
          <SlideOutlineReview content={content.data} run={run} notebookId={notebookId} />
        ) : viewer === "markdown" ? (
          <MarkdownContent content={content.data} />
        ) : (
          <pre className="whitespace-pre-wrap text-sm leading-relaxed">
            {isJson ? formatJson(content.data) : content.data}
          </pre>
        )}
      </div>

      {isTexty && content.isSuccess && (isSkillPackage || canWrite) && (
        <div className="flex shrink-0 flex-wrap justify-end gap-2 border-t px-3 py-2">
          {isSkillPackage && (
            <Button
              size="sm"
              onClick={createSkillFromPackage}
              disabled={createSkill.isPending}
              data-testid="artifact-create-skill"
            >
              <Blocks className="size-3.5" />
              이 패키지로 스킬 만들기
            </Button>
          )}
          {canWrite && (
            <Button
              variant="outline"
              size="sm"
              onClick={saveAsNote}
              disabled={createNote.isPending}
              data-testid="artifact-save-to-note"
            >
              <StickyNote className="size-3.5" />
              메모로 저장
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
