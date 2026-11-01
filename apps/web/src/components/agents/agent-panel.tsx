"use client";

import { Blocks, ChevronDown, FileText, Settings2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { toast } from "sonner";
import { AgentApprovalCard } from "@/components/agents/agent-approval-card";
import { AgentRunDialog } from "@/components/agents/agent-run-dialog";
import { SlideDeckDialog } from "@/components/agents/slide-deck/slide-deck-dialog";
import { RunDetailView } from "@/components/agents/run-detail-view";
import { HiddenRunsSection, HideFailedRunsButton, RunHideMenu } from "@/components/agents/run-hide-actions";
import { StudioRunItem } from "@/components/agents/studio-run-item";
import { StudioStrip } from "@/components/agents/studio-strip";
import { defaultValues } from "@/components/agents/schema-form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusBadge, type StatusTone } from "@/components/ui/status-badge";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAgentRuns, useAgents, useCancelRun, useCreateRun } from "@/hooks/use-agents";
import { useSkills } from "@/hooks/use-skills";
import { useSources } from "@/hooks/use-sources";
import { useStudioCatalog } from "@/hooks/use-studio";
import { visibleArtifacts } from "@/lib/run-coverage";
import { continuedDeckRun } from "@/lib/slide-deck/flow";
import {
  skillToAgent,
  SLIDE_DECK_CARD_ID,
  SLIDE_OUTLINE_CARD_ID,
  studioArtifact,
  studioCardForRun,
  type StudioCard,
} from "@/lib/studio-registry";
import { useSourceSelection } from "@/stores/source-selection";
import {
  KIND_LABEL,
  RUN_STATUS_LABEL,
  triggerLabel,
  type Agent,
  type AgentKind,
  type AgentRun,
  type ArtifactRef,
} from "@/lib/agent-types";
import { formatDate, formatRelative, formatTime } from "@/lib/format";

/** 기본으로 보이는 유형 — 결과물을 만드는 스킬·워크플로우(ui-ux-improvements ④). */
const PRIMARY_KINDS: AgentKind[] = ["skill", "workflow"];
/** 「고급」을 펼쳐야 보이는 유형. */
const ADVANCED_KINDS: AgentKind[] = ["plugin", "tool", "mcp"];

/** 실행 결과를 날짜별로 묶는다 — 머리글은 「오늘」·「어제」·「2026. 10. 10.」. 입력 순서(최신순)를 지킨다. */
function groupRunsByDay(runs: AgentRun[], now: Date = new Date()): { label: string; runs: AgentRun[] }[] {
  const today = formatDate(now);
  const yesterday = formatDate(new Date(now.getTime() - 24 * 60 * 60 * 1000));
  const groups: { label: string; runs: AgentRun[] }[] = [];
  for (const run of runs) {
    const day = formatDate(run.created_at);
    const label = day === today ? "오늘" : day === yesterday ? "어제" : day;
    const last = groups[groups.length - 1];
    if (last?.label === label) last.runs.push(run);
    else groups.push({ label, runs: [run] });
  }
  return groups;
}

const RUN_STATUS_TONE: Record<AgentRun["status"], StatusTone> = {
  queued: "neutral",
  running: "info",
  awaiting_approval: "warning",
  succeeded: "success",
  failed: "danger",
  cancelled: "neutral",
};

/** 실행 상태 배지 — 완료 성공 톤, 실패 위험 톤, 실행 중·승인 대기는 진행 표시. 미지 상태는 원시값·중립 톤. */
export function RunStatusBadge({ run }: { run: AgentRun }) {
  const label = RUN_STATUS_LABEL[run.status] ?? run.status;
  return (
    <StatusBadge
      tone={RUN_STATUS_TONE[run.status] ?? "neutral"}
      pulse={run.status === "running" || run.status === "awaiting_approval"}
    >
      {run.status === "running" ? `${label} ${run.progress}%` : label}
    </StatusBadge>
  );
}

/** 필수 파라미터가 모두 기본값을 갖고 소스 선택이 필요 없으면 다이얼로그 없이 즉시 실행 가능 */
function canInstantRun(agent: Agent): boolean {
  if (agent.input?.scope === "selected") return false; // 소스 피커 노출 필요
  const props = agent.params_schema.properties ?? {};
  return (agent.params_schema.required ?? []).every((k) => props[k]?.default !== undefined);
}

/** 실행 상세를 연 요소의 식별자 — 상세를 닫으면 그 요소로 포커스를 돌린다(모달이 하던 일). */
function runOpenerKey(runId: string, artifact?: ArtifactRef): string {
  return artifact ? `artifact:${runId}:${artifact.key}` : `run:${runId}`;
}

export function AgentPanel({ notebookId, canWrite }: { notebookId: string; canWrite: boolean }) {
  const [kind, setKind] = useState<AgentKind>("skill");
  const [showAdvanced, setShowAdvanced] = useState(false);
  const advancedOpen = showAdvanced || ADVANCED_KINDS.includes(kind);
  const visibleKinds = advancedOpen ? [...PRIMARY_KINDS, ...ADVANCED_KINDS] : PRIMARY_KINDS;
  const agents = useAgents(kind);
  const skills = useSkills();
  const runs = useAgentRuns(notebookId);
  const cancel = useCancelRun(notebookId);
  const createRun = useCreateRun(notebookId);
  const [runTarget, setRunTarget] = useState<Agent | null>(null);
  // 스튜디오 대상 에이전트는 소스 선택기를 보이고, 기본값을 현재 소스 패널 선택으로 둔다(AC-1.9)
  const [studioTarget, setStudioTarget] = useState<StudioCard | null>(null);
  const sources = useSources(notebookId);
  const panelSelection = useSourceSelection((s) => s.selected[notebookId]);
  const studioInitialSources = useMemo(() => {
    const ready = new Set(
      (sources.data ?? []).filter((s) => s.status === "ready").map((s) => s.id),
    );
    return (panelSelection ?? []).filter((id) => ready.has(id));
  }, [sources.data, panelSelection]);

  // 스튜디오 카드에 묶인 시스템 에이전트 — 탭에서 숨기고, 결과 목록은 이 id로 스튜디오 항목을 가린다(#180)
  const studio = useStudioCatalog();

  // 「슬라이드 자료」는 전용 맞춤설정 다이얼로그(형식·길이·소스·설명·개요 검토)로 연다(slide-deck-studio AC-2)
  const [slideDeckOpen, setSlideDeckOpen] = useState(false);

  function openDialog(agent: Agent, card: StudioCard | null = null) {
    if (card?.id === SLIDE_DECK_CARD_ID) {
      setSlideDeckOpen(true);
      return;
    }
    setStudioTarget(card);
    setRunTarget(agent);
  }
  // [리디자인] 실행 결과 상세·산출물은 모달이 아니라 패널 인라인으로 표시한다.
  // 선택된 run id를 보관하고, 폴링되는 runs에서 최신 스냅샷을 찾아 넘긴다.
  // 실행 결과를 열면 상세(결과 탭에 산출물 본문)를 연다. 산출물 칩으로 열었으면 그 산출물을 먼저 보인다.
  const [detail, setDetail] = useState<{ runId: string; artifactKey?: string; opener: string } | null>(
    null,
  );
  const listRef = useRef<HTMLDivElement>(null);
  const restoreFocusRef = useRef<string | null>(null);

  useEffect(() => {
    const key = restoreFocusRef.current;
    if (detail || !key) return;
    restoreFocusRef.current = null;
    const openers = listRef.current?.querySelectorAll<HTMLElement>("[data-run-opener]") ?? [];
    Array.from(openers)
      .find((el) => el.dataset.runOpener === key)
      ?.focus();
  }, [detail]);

  // 스튜디오 항목은 대표 산출물을 먼저 보이지만, 포커스 복귀는 항목(run 단위)으로 한다
  function openRun(run: AgentRun, artifact?: ArtifactRef, opener = runOpenerKey(run.id, artifact)) {
    setDetail({ runId: run.id, artifactKey: artifact?.key, opener });
  }

  // 스킬 탭: 레거시 skill agent + 실행 가능한 파일 기반 스킬(wrapper) 병합.
  // 스튜디오 카드에 묶인 에이전트는 스튜디오로만 연다 — 탭 목록에서 뺀다(#180 결정 2, D-23 「보임」 대체)
  const displayAgents = useMemo<Agent[]>(() => {
    const base = agents.data ?? [];
    const runnable =
      kind === "skill"
        ? (skills.data ?? []).filter((s) => s.can_run && s.wrapper_agent_id).map(skillToAgent)
        : [];
    return [...runnable, ...base].filter((a) => !studio.byAgentId.has(a.id));
  }, [agents.data, skills.data, kind, studio.byAgentId]);

  const detailRun = detail ? ((runs.data ?? []).find((r) => r.id === detail.runId) ?? null) : null;

  function runWithDefaults(agent: Agent) {
    createRun.mutate(
      { agent_id: agent.id, params: defaultValues(agent.params_schema) },
      {
        onSuccess: () => toast.success(`"${agent.name}" 실행을 시작했습니다`),
        onError: (err) => toast.error(`실행 실패: ${err.message}`),
      },
    );
  }

  // 실행 결과 상세 인라인 뷰 — 열려 있으면 패널 전체를 이 뷰로 대체한다.
  // (선택했던 run이 폴링 목록에서 사라지면 자동으로 목록으로 복귀)
  if (detail && detailRun) {
    return (
      <RunDetailView
        run={detailRun}
        runs={runs.data}
        notebookId={notebookId}
        canWrite={canWrite}
        initialArtifactKey={detail.artifactKey}
        studioCard={studioCardForRun(detailRun, studio.byAgentId)}
        onBack={() => {
          restoreFocusRef.current = detail.opener;
          setDetail(null);
        }}
      />
    );
  }

  const gallery = (
    <div className="flex h-full min-h-0 flex-col">
      <StudioStrip onOpen={openDialog} />
      <div className="border-b px-3 py-2">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold">만들기</h2>
          <Link
            href="/skills"
            className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            title="스킬 만들기·편집·공유"
          >
            <Blocks className="size-3" /> 스킬 관리
          </Link>
        </div>
        <div className="flex items-center gap-1">
          <Tabs value={kind} onValueChange={(v) => setKind(v as AgentKind)} className="min-w-0 flex-1">
            {/* 유형 탭: 칸을 같은 폭(grid)으로 나누면 좁은 패널에서 긴 라벨이 옆 칸과 겹친다. 각 탭은 라벨 폭 이상을
                유지하고(flex-1 + nowrap), 패널이 라벨 합보다 좁으면 줄 전체가 가로 스크롤한다(plan §6.4.2 최소 12px). */}
            <TabsList className="w-full justify-start overflow-x-auto overflow-y-hidden">
              {visibleKinds.map((k) => (
                <TabsTrigger key={k} value={k} className="px-1 text-xs">
                  {KIND_LABEL[k]}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
          <button
            type="button"
            aria-expanded={advancedOpen}
            onClick={() => {
              // 고급 유형을 보고 있다가 접으면 기본 유형(스킬)으로 돌아간다
              if (advancedOpen && ADVANCED_KINDS.includes(kind)) setKind("skill");
              setShowAdvanced(!advancedOpen);
            }}
            className="inline-flex shrink-0 items-center gap-0.5 rounded-md px-1.5 py-1 text-xs text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none coarse:min-h-11"
          >
            고급
            <ChevronDown className={`size-3 transition-transform ${advancedOpen ? "rotate-180" : ""}`} aria-hidden />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {agents.isPending ? (
          <div className="flex flex-col gap-2 p-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-16" />
            ))}
          </div>
        ) : displayAgents.length === 0 ? (
          <div className="px-4 py-10 text-center text-sm text-muted-foreground">
            <p>{KIND_LABEL[kind]} 에이전트가 없습니다</p>
            {kind === "skill" && (
              <Link href="/skills" className="mt-2 inline-block text-primary hover:underline">
                스킬 만들기 →
              </Link>
            )}
          </div>
        ) : (
          // [리디자인] 정사각형 → 가로형 직사각형 카드.
          // 패널 폭에 따라 열 수가 1→2→3→4로 자동 반응(auto-fill minmax).
          <ul
            className="grid gap-2 p-3 [grid-template-columns:repeat(auto-fill,minmax(200px,1fr))]"
            data-testid="agent-grid"
          >
            {displayAgents.map((agent) => (
              <li key={agent.id} className="relative">
                <button
                  type="button"
                  onClick={() => (canInstantRun(agent) ? runWithDefaults(agent) : openDialog(agent))}
                  disabled={createRun.isPending}
                  className="flex w-full items-center gap-3 rounded-lg border p-3 text-left transition-colors hover:bg-accent/60 disabled:opacity-60"
                  title={agent.description}
                >
                  <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-accent text-2xl">
                    {agent.icon}
                  </span>
                  <span className="flex min-w-0 flex-1 flex-col">
                    <span className="truncate text-xs font-medium">{agent.name}</span>
                    <span className="line-clamp-1 text-xs text-muted-foreground">
                      {agent.description}
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={`${agent.name} 설정`}
                  title="프롬프트·옵션 설정 후 실행"
                  onClick={() => openDialog(agent)}
                  className="absolute right-1.5 top-1.5 rounded-md p-1 text-muted-foreground/60 transition-colors hover:bg-accent hover:text-foreground"
                  data-testid={`agent-settings-${agent.name}`}
                >
                  <Settings2 className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );

  const results = (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex shrink-0 items-center justify-between gap-2 px-3 py-2">
        <h3 className="text-xs font-semibold text-muted-foreground">최근 결과</h3>
        <HideFailedRunsButton notebookId={notebookId} runs={runs.data ?? []} />
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {(runs.data ?? []).length === 0 ? (
          <p className="px-3 pb-4 text-xs text-muted-foreground">아직 실행 결과가 없습니다</p>
        ) : (
          <div className="flex flex-col gap-2 px-2 pb-2" data-testid="run-list">
            {groupRunsByDay(runs.data ?? []).map((group) => (
              <section key={group.label} aria-label={group.label} className="flex flex-col gap-1">
                <h4 className="px-1 pt-1 text-xs font-medium text-muted-foreground">{group.label}</h4>
                <ul className="flex flex-col gap-1">
                  {group.runs.map((run) => {
                    const card = studioCardForRun(run, studio.byAgentId);
                    if (card) {
                      return (
                        <StudioRunItem
                          key={run.id}
                          run={run}
                          card={card}
                          reviewPending={
                            card.id === SLIDE_OUTLINE_CARD_ID && !continuedDeckRun(run.id, runs.data)
                          }
                          openerKey={runOpenerKey(run.id)}
                          onOpen={() => openRun(run, studioArtifact(run, card), runOpenerKey(run.id))}
                        />
                      );
                    }
                    return (
                      <li
                        key={run.id}
                        className="cursor-pointer rounded-md border p-2 text-xs transition-colors hover:bg-accent/40"
                        onClick={() => openRun(run)}
                        data-testid={`run-item-${run.id}`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="flex min-w-0 items-center gap-1">
                            <span aria-hidden>{run.agent_icon}</span>
                            {/* 키보드로도 열 수 있게 이름을 버튼으로 둔다(항목 전체 클릭은 마우스 편의) */}
                            <button
                              type="button"
                              data-run-opener={runOpenerKey(run.id)}
                              aria-label={`${run.agent_name} 실행 결과 보기`}
                              onClick={(e) => {
                                e.stopPropagation();
                                openRun(run);
                              }}
                              className="truncate rounded-sm text-left font-medium outline-none focus-visible:ring-2 focus-visible:ring-ring/50"
                            >
                              {run.agent_name}
                            </button>
                            {run.trigger !== "manual" && (
                              <Badge variant="outline" className="px-1 py-0 text-xs">
                                {triggerLabel(run.trigger)}
                              </Badge>
                            )}
                          </span>
                          <span className="flex shrink-0 items-center gap-0.5">
                            <RunStatusBadge run={run} />
                            {/* 숨김 인가는 서버가 호출자 기준으로 판정한다(실패·취소 ∧ 취소 규칙, #220) */}
                            <RunHideMenu notebookId={notebookId} run={run} label={run.agent_name ?? "실행 결과"} />
                          </span>
                        </div>
                        {run.status === "running" && (
                          <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-accent">
                            <div
                              className="h-full rounded-full bg-primary transition-all"
                              style={{ width: `${run.progress}%` }}
                            />
                          </div>
                        )}
                        <div className="mt-1 flex items-center justify-between gap-2 text-muted-foreground">
                          {/* 날짜는 묶음 머리글에 있다 — 오늘은 상대 시각, 그 밖의 날은 시각만 */}
                    <span>
                      {group.label === "오늘" ? formatRelative(run.created_at) : formatTime(run.created_at)}
                    </span>
                          {/* 취소 인가는 서버가 호출자 기준으로 판정한다(실행 소유자 ∨ write 이상, #173) */}
                          {run.can_cancel &&
                            (run.status === "queued" ||
                            run.status === "running" ||
                            run.status === "awaiting_approval") && (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-5 px-1 text-xs"
                              onClick={(e) => {
                                e.stopPropagation();
                                cancel.mutate(run.id);
                              }}
                            >
                              취소
                            </Button>
                          )}
                        </div>
                        {run.error && (
                          <p className="mt-1 line-clamp-2 text-destructive">{run.error}</p>
                        )}
                        <AgentApprovalCard run={run} notebookId={notebookId} />
                        {/* 반영 현황(`coverage.json`, #217)은 칩으로 보이지 않는다 — 상세 결과 위에 표시된다 */}
                        {visibleArtifacts(run).length > 0 && (
                          <div className="mt-1 flex flex-wrap gap-1">
                            {visibleArtifacts(run).map((a) => (
                              <button
                                key={a.key}
                                type="button"
                                data-run-opener={runOpenerKey(run.id, a)}
                                onClick={(e) => {
                                  e.stopPropagation();
                                  openRun(run, a);
                                }}
                                className="inline-flex items-center gap-1 rounded bg-primary/10 px-1.5 py-0.5 text-xs text-primary hover:bg-primary/20"
                              >
                                <FileText className="size-3" /> {a.name}
                              </button>
                            ))}
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
        )}
        {/* 숨긴 결과 보기·되돌리기(#221) — 숨긴 run이 있을 때만 */}
        <HiddenRunsSection notebookId={notebookId} />
      </div>
    </div>
  );

  return (
    <div ref={listRef} className="flex h-full min-h-0 flex-col">
      {/* [리디자인] 갤러리(위)/실행 결과(아래)를 세로 리사이즈 핸들로 분할 —
          핸들을 위/아래로 끌어 두 영역 높이를 조절. 폭은 우측 패널 리사이즈로 조절. */}
      <PanelGroup direction="vertical" autoSaveId="agent-panel-split" className="min-h-0 flex-1">
        <Panel id="agent-gallery" order={1} defaultSize={60} minSize={25}>
          {gallery}
        </Panel>
        <PanelResizeHandle className="group relative flex h-2 items-center justify-center border-t outline-none">
          <div className="h-1 w-10 rounded-full bg-border transition-colors group-hover:bg-primary/50 group-data-[resize-handle-state=drag]:bg-primary" />
        </PanelResizeHandle>
        <Panel id="agent-results" order={2} defaultSize={40} minSize={15}>
          {results}
        </Panel>
      </PanelGroup>

      <AgentRunDialog
        agent={runTarget}
        notebookId={notebookId}
        open={runTarget !== null}
        onOpenChange={(o) => !o && setRunTarget(null)}
        studioCard={studioTarget}
        initialSourceIds={studioTarget ? studioInitialSources : undefined}
      />
      <SlideDeckDialog
        notebookId={notebookId}
        open={slideDeckOpen}
        onOpenChange={setSlideDeckOpen}
        initialSourceIds={studioInitialSources}
      />
    </div>
  );
}
