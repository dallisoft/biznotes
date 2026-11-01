"use client";

import { useQuery } from "@tanstack/react-query";
import { EllipsisVertical } from "lucide-react";
import { useMemo } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { StatusBadge } from "@/components/ui/status-badge";
import { fetchArtifact } from "@/hooks/use-agents";
import { useModels } from "@/hooks/use-chat";
import type { AgentRun } from "@/lib/agent-types";
import type { LLMModelInfo } from "@/lib/chat-types";
import { formatRelative } from "@/lib/format";
import { bounded } from "@/lib/mindmap/bounded";
import { exportMindmapJson } from "@/lib/mindmap/export";
import {
  parseLenientJson,
  parseMindmap,
  sanitizeLabel,
  truncateChars,
  type MindmapParseResult,
} from "@/lib/mindmap/parse";
import { qk } from "@/lib/query-keys";
import type { StudioCard } from "@/lib/studio-registry";

const TITLE_CHARS = 80;
/** 마크다운 첫 제목은 본문 앞부분에서만 찾는다 — 긴 문서를 통째로 훑지 않는다 */
const HEADING_SCAN_CHARS = 4000;

/** 마크다운 본문의 첫 ATX 제목(`# 제목`) — 없거나 비면 null. */
export function markdownFirstHeading(body: string): string | null {
  const m = /^ {0,3}#{1,6}[ \t]+(.+?)[ \t#]*$/m.exec(body.slice(0, HEADING_SCAN_CHARS));
  if (!m) return null;
  const t = sanitizeLabel(m[1].replace(/[*_`]/g, ""));
  return t ? truncateChars(t, TITLE_CHARS) : null;
}

/** JSON 산출물의 최상위 `title`(슬라이드 자료·개요) — 없거나 비면 null. 상한 초과 본문은 파싱하지 않는다. */
export function jsonTitle(body: string): string | null {
  const b = bounded(body);
  if (b.kind !== "ok") return null;
  const parsed = parseLenientJson(b.text);
  if (!parsed.ok) return null;
  const v = parsed.value;
  const raw = typeof v === "object" && v !== null && !Array.isArray(v) ? (v as { title?: unknown }).title : null;
  const t = typeof raw === "string" ? sanitizeLabel(raw) : "";
  return t ? truncateChars(t, TITLE_CHARS) : null;
}

/** 결과 항목 제목(D-6): 본문 제목 → `params.topic` → 카드 이름. 조회 중·실패·상한 초과는 대체값. */
export function studioRunTitle(run: AgentRun, bodyTitle: string | null, card: StudioCard): string {
  if (bodyTitle) return bodyTitle;
  const topic = (run.params as Record<string, unknown> | null)?.topic;
  if (typeof topic === "string") {
    const t = sanitizeLabel(topic);
    if (t) return truncateChars(t, TITLE_CHARS);
  }
  return card.label;
}

export function studioSourceCountLabel(run: AgentRun): string {
  const ids = run.input_source_ids;
  return ids ? `소스 ${ids.length}개` : "전체 소스";
}

/**
 * 실제로 응답한 모델 이름(#234) — 목록 응답의 `used_models`(처음 응답한 순서). 서버는 모델 id로 겹침을 없애므로
 * 표시 이름이 같은 다른 모델은 여기서 한 번만 남긴다 — 상세 머리 「사용 모델」(`usedModels`)과 같다(PR-2 독립 리뷰 Low).
 * 필드가 없는 응답(웹만 먼저 배포된 경우)은 빈 목록이다.
 */
export function studioModelNames(run: AgentRun): string[] {
  return [...new Set((run.used_models ?? []).map((m) => m.display_name))];
}

/**
 * 「모델 X」 — 둘까지는 쉼표로 잇고 셋 이상이면 첫 이름 「외 N개」다. 없으면(진행 중·실패·옛 run) null — 줄을 그리지
 * 않는다.
 */
export function studioModelLabel(run: AgentRun): string | null {
  const names = studioModelNames(run);
  if (names.length === 0) return null;
  if (names.length <= 2) return `모델 ${names.join(", ")}`;
  return `모델 ${names[0]} 외 ${names.length - 1}개`;
}

/** 폴백한 run의 요청 모델 이름 — run의 `model_id`로 켜진 모델 목록에서 찾는다. 지정 안 함·목록에 없음은 null. */
export function requestedModelName(
  modelId: string | null | undefined,
  models: LLMModelInfo[] | undefined,
): string | null {
  if (!modelId) return null;
  return models?.find((m) => m.id === modelId)?.display_name ?? null;
}

/** 요청 모델 이름 — 폴백한 run에만 그려 모델 목록(`GET /api/v1/models`)도 그때만 읽는다. */
function RequestedModel({ modelId }: { modelId: string }) {
  const models = useModels();
  const name = requestedModelName(modelId, models.data);
  if (!name) return null;
  return (
    <span className="truncate" title={`요청 모델 ${name}`} data-testid="studio-run-requested-model">
      요청 {name}
    </span>
  );
}

/**
 * 스튜디오 결과 항목(mindmap-agent-skill AC-1.10 · #180) — 아이콘 · 제목 · 「소스 N개 · 상대 시각 · 모델 X」 · 케밥.
 * 모델은 실제로 응답한 모델이고, 폴백했으면 「폴백」 배지와 요청 모델 이름을 붙인다(#234).
 * 제목은 대표 산출물 본문을 뷰어와 같은 키로 한 번만 읽는다(성공 run — 재조회 없음, 실패는 재시도 없음).
 * 본문에 제목이 없는 카드(퀴즈·플래시카드·오디오)는 본문을 읽지 않는다. 슬라이드 자료·개요는 JSON `title`을 쓴다.
 * 케밥은 「열기」, 마인드맵만 「JSON 다운로드」를 더한다. 「메모로 저장」은 쓰기 동작이라 넣지 않는다(D-20).
 */
export function StudioRunItem({
  run,
  card,
  openerKey,
  onOpen,
  reviewPending = false,
}: {
  run: AgentRun;
  card: StudioCard;
  openerKey: string;
  onOpen: () => void;
  /** 슬라이드 개요 run이 아직 「계속」되지 않았다 — 「검토」 버튼을 보인다(slide-deck-studio AC-4). */
  reviewPending?: boolean;
}) {
  const isMindmap = card.title === "mindmap";
  const body = useQuery({
    queryKey: qk.artifact.body(run.id, card.artifactName),
    queryFn: () => fetchArtifact(run.id, card.artifactName),
    enabled: card.title !== "params",
    staleTime: Infinity,
    retry: 0,
    refetchOnWindowFocus: false,
  });
  const parsed = useMemo<MindmapParseResult | null>(
    () => (isMindmap && body.data !== undefined ? parseMindmap(body.data) : null),
    [isMindmap, body.data],
  );
  const bodyTitle = useMemo(() => {
    if (isMindmap) return parsed?.ok ? parsed.doc.root.label : null;
    if (body.data === undefined) return null;
    if (card.title === "markdown-heading") return markdownFirstHeading(body.data);
    return card.title === "json-title" ? jsonTitle(body.data) : null;
  }, [isMindmap, parsed, card.title, body.data]);
  const title = studioRunTitle(run, bodyTitle, card);
  const modelLabel = studioModelLabel(run);

  function downloadJson() {
    if (parsed?.ok) exportMindmapJson(parsed.doc, parsed.doc.root.title);
    else toast.error("마인드맵 데이터를 내려받지 못했습니다");
  }

  return (
    <li
      className="flex cursor-pointer items-center gap-2 rounded-md border p-2 text-xs transition-colors hover:bg-accent/40"
      onClick={onOpen}
      data-testid={`studio-run-item-${run.id}`}
    >
      <span aria-hidden className="flex size-8 shrink-0 items-center justify-center rounded-md bg-accent text-lg">
        {card.icon}
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <button
          type="button"
          data-run-opener={openerKey}
          aria-label={`${title} 열기`}
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
          className="truncate rounded-sm text-left font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
          title={title}
          data-testid="studio-run-title"
        >
          {title}
        </button>
        <span className="flex min-w-0 items-center gap-1 text-muted-foreground">
          <span className="truncate">
            {studioSourceCountLabel(run)} · {formatRelative(run.created_at)}
            {modelLabel && (
              <span
                title={studioModelNames(run).join(", ")}
                data-testid="studio-run-model"
              >
                {" "}
                · {modelLabel}
              </span>
            )}
          </span>
          {modelLabel && run.fallback_used && (
            <>
              <StatusBadge
                tone="warning"
                title="요청 모델이 실패해 폴백 모델이 응답했습니다"
                data-testid="studio-run-fallback"
              >
                폴백
              </StatusBadge>
              {run.model_id && <RequestedModel modelId={run.model_id} />}
            </>
          )}
        </span>
      </span>
      {reviewPending && (
        <Button
          size="sm"
          variant="outline"
          className="h-7 shrink-0 px-2 text-xs"
          onClick={(e) => {
            e.stopPropagation();
            onOpen();
          }}
          aria-label={`${title} 개요 검토`}
          data-testid="studio-run-review"
        >
          검토
        </Button>
      )}
      <span onClick={(e) => e.stopPropagation()}>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <button
                type="button"
                aria-label={`${title} 메뉴`}
                className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                data-testid="studio-run-menu"
              >
                <EllipsisVertical className="size-3.5" />
              </button>
            }
          />
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={onOpen} data-testid="studio-run-open">
              열기
            </DropdownMenuItem>
            {isMindmap && (
              <DropdownMenuItem
                onClick={downloadJson}
                disabled={!parsed?.ok}
                data-testid="studio-run-download-json"
              >
                JSON 다운로드
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </span>
    </li>
  );
}
