"use client";

import { Database } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { AdminTableFrame } from "@/components/admin/admin-table-frame";
import { ReindexProgressPanel } from "@/components/admin/reindex-progress-panel";
import { ReindexRequestId } from "@/components/admin/reindex-request-id";
import { RerankerSelect } from "@/components/admin/reranker-select";
import { SourceDigestBackfillSection } from "@/components/admin/source-digest-backfill-section";
import { DangerZone } from "@/components/layout/danger-zone";
import { FormField, FormSection } from "@/components/layout/form-section";
import { StickySaveBar } from "@/components/layout/sticky-save-bar";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmButton } from "@/components/ui/confirm-button";
import { RowActions } from "@/components/ui/row-actions";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { LoadingState } from "@/components/ui/loading-state";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TablePagination } from "@/components/ui/table-pagination";
import { usePagination } from "@/hooks/use-pagination";
import {
  useActiveReindexRuns,
  useIndexStatus,
  useRagSettings,
  useReindexCacheActions,
  useReindexRun,
  useReindexRuns,
  useRerankerStatus,
  useSaveRagSettings,
  useStartReindex,
} from "@/hooks/use-reindex";
import type { RagSettings, ReindexStartOut } from "@/lib/admin-types";
import {
  errorStatus,
  isAuthError,
  isTerminalReindexError,
  selectFocusedRun,
  summarizeRun,
  type KnownReindexScope,
} from "@/lib/reindex-progress";
import { useNarrowViewport } from "@/hooks/use-narrow-viewport";

const BUSY_REASON_ID = "reindex-busy-reason";

/** 설정 응답 전 자리값 — 화면에는 그리지 않는다(응답 전에는 폼 대신 로딩 상태, 기본값으로 저장값을 덮어쓰지 않음). */
const FORM_PLACEHOLDER: RagSettings = {
  chunk_size: 800,
  chunk_overlap: 100,
  top_k: 6,
  rerank_enabled: false,
  rerank_candidates: 20,
  rerank_model: "BAAI/bge-reranker-base",
  verify_lexical_threshold: 0.25,
  verify_embedding_threshold: 0.55,
  insufficient_context_threshold_base: 0.5,
  insufficient_context_threshold_rerank: 0.5,
  embedding_batch_size: 64,
  embedding_timeout_seconds: 60,
  rerank_timeout_seconds: 30,
};

/**
 * 폼이 기준 설정과 같은가(저장 막대 표시 판정 — StickySaveBar는 바뀌었을 때만 보인다, AC-5.5). 키 합집합을 본다 —
 * 구 API 응답의 모르는 키도 그대로 실어 보내므로(원값 보존) 비교에서 빠지지 않게 한다.
 */
function sameSettings(a: RagSettings, b: RagSettings): boolean {
  const left = a as unknown as Record<string, unknown>;
  const right = b as unknown as Record<string, unknown>;
  const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
  for (const key of keys) if (!Object.is(left[key], right[key])) return false;
  return true;
}

export default function AdminRagPage() {
  const settings = useRagSettings();
  const cache = useReindexCacheActions();
  // P1-02: 확인 직후 트리거가 비활성화되므로 포커스를 사유(상태) 영역으로 옮긴다.
  const busyReasonRef = useRef<HTMLDivElement>(null);
  const runsQuery = useReindexRuns();
  // 활성 run의 진실 공급원 — 최근 100건 밖 활성 run도 찾는다(core-v0.12.0 `active_only`)
  const activeQuery = useActiveReindexRuns();

  // D-4: 내가 시작한 run · 404로 사라진 run · POST 응답(후보 줄) · 내가 아는 요청 범위
  const [startedRunId, setStartedRunId] = useState<string | null>(null);
  const [dismissedRunIds, setDismissedRunIds] = useState<ReadonlySet<string>>(() => new Set());
  const [notFound, setNotFound] = useState(false);
  const [lastRun, setLastRun] = useState<ReindexStartOut | null>(null);
  const [lastStart, setLastStart] = useState<ReindexStartOut | null>(null);
  const [knownScopes, setKnownScopes] = useState<Record<string, KnownReindexScope>>({});
  // D-6: run별 마지막으로 본 queued(종결 전이 판정)와 완료 토스트 1회 보장
  const observedQueued = useRef(new Map<string, number>());
  const toastedRunIds = useRef(new Set<string>());

  // 활성 질의 결과를 우선하고 나머지는 이력 목록에서 채운다. 활성 질의에서 빠진 run의 이력 값은
  // 아래 효과가 이력 목록을 곧바로 다시 받아 종결 카운트로 바뀐다(D-6 종결 토스트 유지).
  const listRuns = useMemo(() => {
    const active = activeQuery.data ?? [];
    const activeIds = new Set(active.map((run) => run.id));
    const history = (runsQuery.data ?? []).filter((run) => !activeIds.has(run.id));
    return [...active, ...history].filter((run) => !dismissedRunIds.has(run.id));
  }, [activeQuery.data, runsQuery.data, dismissedRunIds]);
  const activeIdsRef = useRef<ReadonlySet<string> | null>(null);
  useEffect(() => {
    if (!activeQuery.data) return;
    const ids = new Set(activeQuery.data.map((run) => run.id));
    const previous = activeIdsRef.current;
    activeIdsRef.current = ids;
    if (previous && [...previous].some((id) => !ids.has(id))) cache.refreshAfterRunFinished();
  }, [activeQuery.data, cache]);
  // 방금 시작한 run이 아직 목록에 없으면(목록 갱신 전) 그 run을 먼저 상세 조회한다.
  const startedOutsideList =
    startedRunId !== null &&
    !dismissedRunIds.has(startedRunId) &&
    !listRuns.some((run) => run.id === startedRunId);
  const focusedRunId = startedOutsideList
    ? startedRunId
    : (selectFocusedRun(listRuns, startedRunId, dismissedRunIds)?.id ?? null);
  const runQuery = useReindexRun(focusedRunId);
  const focusedDetail =
    runQuery.data && runQuery.data.id === focusedRunId ? runQuery.data : undefined;

  // 목록 + 포커스 run 상세(1초 폴링이 더 최신) 병합. 목록에 없는 시작 run도 합친다(D-4).
  const runs = useMemo(() => {
    if (!focusedDetail) return listRuns;
    const inList = listRuns.some((run) => run.id === focusedDetail.id);
    return inList
      ? listRuns.map((run) => (run.id === focusedDetail.id ? focusedDetail : run))
      : [focusedDetail, ...listRuns];
  }, [listRuns, focusedDetail]);
  const activeRuns = runs.filter((run) => run.queued > 0);
  const focusedRun = focusedRunId ? (runs.find((run) => run.id === focusedRunId) ?? null) : null;
  const otherActive = activeRuns.filter((run) => run.id !== focusedRunId);

  // PR2-03: POST가 run_id를 돌려준 뒤 첫 상세 응답(활성/종결/404/401/403) 전까지는 활성으로 취급
  const startedUnconfirmed =
    startedRunId !== null &&
    !dismissedRunIds.has(startedRunId) &&
    !runs.some((run) => run.id === startedRunId) &&
    !(focusedRunId === startedRunId && isTerminalReindexError(runQuery.error));
  const indexStatus = useIndexStatus(activeRuns.length > 0 || startedUnconfirmed);
  const pg = usePagination(indexStatus.data ?? []);

  // 폼과 기준 설정(서버에서 받은 값 · 방금 저장한 값). 둘이 다를 때만 저장 막대가 보인다.
  const [form, setForm] = useState<RagSettings>(FORM_PLACEHOLDER);
  const [baseline, setBaseline] = useState<RagSettings | null>(null);
  useEffect(() => {
    if (!settings.data) return;
    setForm(settings.data);
    setBaseline(settings.data);
  }, [settings.data]);
  const dirty = baseline !== null && !sameSettings(form, baseline);
  const formRef = useRef<HTMLFormElement>(null);
  const settingsTitleId = useId();
  const opsTitleId = useId();
  const indexTitleId = useId();
  // 신 web × 구 API(v0.13.0) — 생성 타입은 필수지만 실제 응답에 키가 없을 수 있다(plan-amendment-1)
  const hasRerankTimeout =
    settings.data !== undefined && "rerank_timeout_seconds" in settings.data;
  // 리랭커 선택은 이 화면이 뜬 뒤 **현재 서버**에서 받은 설정이 새 계약(v0.15.0+)일 때만 연다.
  // 롤백 전에 받은 캐시는 근거가 될 수 없다(리뷰 P2-09)
  const rerankerChoiceSupported = settings.isFetchedAfterMount && hasRerankTimeout;
  const rerankerStatus = useRerankerStatus(form.rerank_enabled);

  const save = useSaveRagSettings();
  const reindex = useStartReindex({
    // 새 요청을 보내는 순간 직전 결과 줄을 지운다 — 실패·대기 중에 옛 결과가 남아
    // 방금 누른 요청의 결과로 오인되는 것을 막는다.
    onMutate: () => setLastRun(null),
    onResult: (result, request) => {
      setLastRun(result);
      const runId = result.run_id;
      if (runId) {
        setKnownScopes((current) => ({
          ...current,
          [runId]: request.notebook_id ? { notebook_id: request.notebook_id } : "all",
        }));
      }
    },
    onStarted: (runId, counts) => {
      // non-null run_id는 queued 항목이 원자적으로 생성됐다는 뜻 — 종결 전이 판정의 기준값.
      if (!observedQueued.current.has(runId)) observedQueued.current.set(runId, counts.enqueued);
      setStartedRunId(runId);
      setNotFound(false);
      setLastStart({ run_id: runId, ...counts });
    },
  });

  // D-6 기준값: 목록에서 처음 본 run의 queued(덮어쓰지 않음 — 전이 판정은 상세 응답으로만).
  useEffect(() => {
    for (const run of listRuns) {
      if (!observedQueued.current.has(run.id)) observedQueued.current.set(run.id, run.queued);
    }
  }, [listRuns]);

  // D-6: 포커스 run의 queued >0 → 0 전이에 run별 1회 토스트 + index-status·runs invalidate.
  // 직전 포커스 run도 함께 본다 — 목록 갱신(2초)이 상세 폴링(1초)보다 먼저 종결을 반영해 포커스가
  // 다음 run으로 넘어가도 그 run의 종결 토스트를 놓치지 않는다.
  const previousFocusRef = useRef<string | null>(null);
  useEffect(() => {
    const ids = new Set([focusedRunId, previousFocusRef.current]);
    previousFocusRef.current = focusedRunId;
    for (const id of ids) {
      const run = id ? runs.find((item) => item.id === id) : undefined;
      if (!run) continue;
      const previous = observedQueued.current.get(run.id);
      observedQueued.current.set(run.id, run.queued);
      if (previous === undefined || previous === 0 || run.queued !== 0) continue;
      if (toastedRunIds.current.has(run.id)) continue;
      toastedRunIds.current.add(run.id);
      const summary = summarizeRun(run, Date.now());
      const message = `재인덱싱 종료 — 완료 ${run.done}건 · 실패 ${run.failed}건${
        summary.removed > 0 ? ` · 삭제로 제외 ${summary.removed}건` : ""
      }${summary.missing > 0 ? ` · 집계 제외 ${summary.missing}건` : ""}`;
      if (run.failed > 0 || summary.missing > 0) toast.warning(message);
      else toast.success(message);
      cache.refreshAfterRunFinished();
    }
  }, [focusedRunId, runs, cache]);

  // 다른 run에 포커스가 생기면 직전 404 안내를 지운다.
  useEffect(() => {
    if (focusedRunId !== null) setNotFound(false);
  }, [focusedRunId]);

  // D-2 404: 종결 — dismissed 등록(재선택 차단), startedRunId 해제, 목록 캐시에서 제거 후 invalidate(훅).
  const detailError = runQuery.error;
  useEffect(() => {
    if (focusedRunId === null || errorStatus(detailError) !== 404) return;
    const goneId = focusedRunId;
    setDismissedRunIds((current) => new Set(current).add(goneId));
    setNotFound(true);
    setStartedRunId((current) => (current === goneId ? null : current));
    cache.forgetRun(goneId);
  }, [detailError, focusedRunId, cache]);

  // D-5: 활성 여부를 모르거나 활성 run이 하나라도 있으면 세 버튼을 잠근다.
  const authError = isAuthError(runQuery.error) || isAuthError(activeQuery.error);
  const bootstrapping = activeQuery.data === undefined && !activeQuery.isError;
  const listUnavailable = activeQuery.isError;
  const narrow = useNarrowViewport();
  const busy =
    reindex.isPending ||
    startedUnconfirmed ||
    bootstrapping ||
    listUnavailable ||
    activeRuns.length > 0 ||
    authError;
  const busyReason = authError
    ? "세션 또는 권한 오류 — 다시 로그인하세요"
    : startedUnconfirmed
      ? "방금 시작한 run의 상태를 확인하는 중입니다"
      : bootstrapping
        ? "활성 run을 확인하는 중입니다"
        : listUnavailable
          ? "활성 run을 확인할 수 없어 요청을 잠갔습니다"
          : activeRuns.length > 0
            ? `재인덱싱 ${activeRuns.length}건이 진행 중이라 새 요청을 받을 수 없습니다`
            : reindex.isPending
              ? "재인덱싱 요청을 등록하는 중입니다"
              : null;
  const describedBy = busy ? BUSY_REASON_ID : undefined;
  const notebookTitles = useMemo(
    () => new Map((indexStatus.data ?? []).map((item) => [item.notebook_id, item.title])),
    [indexStatus.data],
  );
  const panelError = errorStatus(runQuery.error) === 404 ? undefined : runQuery.error;


  /** 숫자 입력 갱신 — 입력 문자열을 숫자로 바꿔 해당 키에 넣는다(기존 동작과 같다). */
  const setNumber = (key: keyof RagSettings) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [key]: +event.target.value });

  return (
    <>
      <PageHeader
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "RAG" }]}
        title="RAG 관리"
        description="검색 파라미터를 조정하고 노트북별 인덱스를 관리합니다"
      />
      <div className="flex flex-col gap-10">
        {/* 섹션 1 — 검색 설정(설정 저장). 운영 작업(재인덱싱)은 아래 섹션·위험 구역에 따로 둔다(DP-5, AC-5.5). */}
        <section aria-labelledby={settingsTitleId}>
          <h2 id={settingsTitleId} className="text-lg font-semibold">
            검색 설정
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            청킹·검색·인용 검증 기준을 정합니다. 바꾼 값은 「변경 사항 저장」을 눌러야 적용됩니다.
          </p>
          <div className="mt-6">
            {settings.data === undefined ? (
              settings.isError ? (
                <ErrorState
                  title="검색 설정을 불러오지 못했습니다"
                  onRetry={() => void settings.refetch()}
                  retrying={settings.isFetching}
                />
              ) : (
                <LoadingState variant="form" count={4} label="검색 설정을 불러오는 중" />
              )
            ) : (
              <form
                ref={formRef}
                aria-labelledby={settingsTitleId}
                onSubmit={(e) => {
                  e.preventDefault();
                  // 저장한 값이 새 기준이다 — 서버 재조회 전에도 저장 막대를 바로 닫는다.
                  save.mutate(form, { onSuccess: (_saved, submitted) => setBaseline(submitted) });
                }}
                className="divide-y"
              >
                <FormSection
                  headingLevel={3}
                  title="청킹"
                  description="소스를 나누는 단위입니다. 바꾸면 새로 인덱싱하는 소스부터 적용됩니다."
                >
                  <FormField label="청크 크기" unit="토큰" range="100~4000">
                    {(field) => (
                      <Input
                        {...field}
                        type="number"
                        min={100}
                        max={4000}
                        value={form.chunk_size}
                        onChange={setNumber("chunk_size")}
                      />
                    )}
                  </FormField>
                  <FormField label="청크 겹침" unit="토큰" range="0~1000">
                    {(field) => (
                      <Input
                        {...field}
                        type="number"
                        min={0}
                        max={1000}
                        value={form.chunk_overlap}
                        onChange={setNumber("chunk_overlap")}
                      />
                    )}
                  </FormField>
                </FormSection>

                <FormSection
                  headingLevel={3}
                  title="검색과 리랭킹"
                  description="질문마다 가져올 근거 수와 재정렬 방식입니다."
                >
                  <FormField label="검색 결과 수(top-k)" unit="개" range="1~30">
                    {(field) => (
                      <Input
                        {...field}
                        type="number"
                        min={1}
                        max={30}
                        value={form.top_k}
                        onChange={setNumber("top_k")}
                      />
                    )}
                  </FormField>
                  <div className="flex items-center justify-between gap-4 rounded-lg border p-3">
                    <div>
                      <Label htmlFor="rerank-enabled">리랭킹 (cross-encoder)</Label>
                      <p id="rerank-enabled-help" className="mt-1 text-xs text-muted-foreground">
                        후보 상위 N개를 다시 정렬해 정확도를 높입니다 — 첫 토큰 지연이 다소 늘어납니다
                      </p>
                    </div>
                    <Switch
                      id="rerank-enabled"
                      aria-describedby="rerank-enabled-help"
                      checked={form.rerank_enabled}
                      onCheckedChange={(c) => setForm({ ...form, rerank_enabled: c })}
                    />
                  </div>
                  {form.rerank_enabled && (
                    <>
                      <FormField label="리랭킹 후보 수" unit="개" range="5~50">
                        {(field) => (
                          <Input
                            {...field}
                            type="number"
                            min={5}
                            max={50}
                            value={form.rerank_candidates}
                            onChange={setNumber("rerank_candidates")}
                          />
                        )}
                      </FormField>
                      <div className="flex flex-col gap-1.5">
                        <RerankerSelect
                          value={form.rerank_model}
                          onChange={(rerank_model) => setForm({ ...form, rerank_model })}
                          status={rerankerStatus}
                          supported={rerankerChoiceSupported}
                        />
                      </div>
                      {/* 구 API 응답에는 이 키가 없다 — 그때는 입력을 숨기고, 폼이 GET 원본이라 PUT에도 넣지 않는다 */}
                      {hasRerankTimeout && (
                        <FormField
                          label="리랭크 타임아웃"
                          unit="초"
                          range="5~180"
                          help="게이트웨이 리랭커 호출 한도입니다."
                        >
                          {(field) => (
                            <Input
                              {...field}
                              type="number"
                              min={5}
                              max={180}
                              value={form.rerank_timeout_seconds}
                              onChange={setNumber("rerank_timeout_seconds")}
                            />
                          )}
                        </FormField>
                      )}
                    </>
                  )}
                </FormSection>

                <FormSection
                  headingLevel={3}
                  title="인용 검증 기준"
                  description={
                    <>
                      1차 어휘 겹침이 기준에 못 미치면 2차로 임베딩 코사인 유사도를 확인합니다.
                    </>
                  }
                >
                  <FormField label="어휘 겹침 기준" range="0~1">
                    {(field) => (
                      <Input
                        {...field}
                        type="number"
                        step={0.01}
                        min={0}
                        max={1}
                        value={form.verify_lexical_threshold}
                        onChange={setNumber("verify_lexical_threshold")}
                      />
                    )}
                  </FormField>
                  <FormField label="임베딩 코사인 유사도 기준" range="0~1">
                    {(field) => (
                      <Input
                        {...field}
                        type="number"
                        step={0.01}
                        min={0}
                        max={1}
                        value={form.verify_embedding_threshold}
                        onChange={setNumber("verify_embedding_threshold")}
                      />
                    )}
                  </FormField>
                </FormSection>

                <FormSection
                  headingLevel={3}
                  title="근거 부족 판정 기준"
                  description="가장 높은 검색 점수가 이 값보다 낮으면 「출처에서 찾지 못함」 응답을 유도합니다. 리랭킹을 켜고 끌 때 점수 분포가 달라 따로 관리합니다."
                >
                  <FormField label="기본 검색 (리랭킹 꺼짐)" range="0~1">
                    {(field) => (
                      <Input
                        {...field}
                        type="number"
                        step={0.01}
                        min={0}
                        max={1}
                        value={form.insufficient_context_threshold_base}
                        onChange={setNumber("insufficient_context_threshold_base")}
                      />
                    )}
                  </FormField>
                  <FormField label="리랭킹 켜짐" range="0~1">
                    {(field) => (
                      <Input
                        {...field}
                        type="number"
                        step={0.01}
                        min={0}
                        max={1}
                        value={form.insufficient_context_threshold_rerank}
                        onChange={setNumber("insufficient_context_threshold_rerank")}
                      />
                    )}
                  </FormField>
                </FormSection>
              </form>
            )}
          </div>
          {/* 폼 밖에 둔다 — 「되돌리기」가 폼을 제출하지 않게. 저장은 requestSubmit으로 입력 범위 검증을 거친다. */}
          <StickySaveBar
            dirty={dirty}
            saving={save.isPending}
            onSave={() => formRef.current?.requestSubmit()}
            onReset={() => {
              if (baseline) setForm(baseline);
            }}
          />
        </section>

        {/* 섹션 2 — 인덱스 운영: 노트북별 현황 + 진행 패널. 노트북 단위 재인덱싱은 표의 행 동작이다. */}
        <section aria-labelledby={opsTitleId}>
          <h2 id={opsTitleId} className="text-lg font-semibold">
            인덱스 운영
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            노트북별 인덱스 상태를 보고 재인덱싱합니다. 재인덱싱이 진행 중이면 새 요청을 받지 않습니다.
          </p>
          {/* P1-11: 라이브 영역은 항상 마운트하고 문구만 바꾼다(새로 생긴 영역의 첫 문구는 읽히지 않을 수 있음).
              P1-02: 확인 직후 포커스 대상이라 tabIndex=-1. */}
          <div
            ref={busyReasonRef}
            id={BUSY_REASON_ID}
            role="status"
            tabIndex={-1}
            className="mt-3 flex items-center gap-2 text-xs text-muted-foreground"
          >
            {busyReason && <span>{busyReason}</span>}
            {busyReason && listUnavailable && !authError && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  void activeQuery.refetch();
                  void runsQuery.refetch();
                }}
              >
                다시 확인
              </Button>
            )}
          </div>
          {lastRun && (
            <p
              data-testid="reindex-result"
              className="mt-2 flex flex-wrap items-center gap-x-1 text-xs text-muted-foreground"
            >
              <span>
                최근 재인덱싱 요청: {lastRun.enqueued}건 등록
                {lastRun.skipped > 0
                  ? ` · ${lastRun.skipped}건 건너뜀 (이미 대기·처리 중)`
                  : " · 건너뛴 소스 없음"}
                {" · "}
              </span>
              {lastRun.run_id ? <ReindexRequestId runId={lastRun.run_id} /> : <span>요청 번호 없음</span>}
            </p>
          )}
          <div className="mt-4 max-w-3xl">
            <ReindexProgressPanel
              run={focusedRun}
              error={panelError}
              notFound={notFound && focusedRun === null}
              unconfirmed={startedUnconfirmed && focusedRunId === startedRunId}
              knownScope={focusedRun ? knownScopes[focusedRun.id] : undefined}
              notebookTitles={notebookTitles}
              lastStart={lastStart}
              otherActive={otherActive}
            />
          </div>

          <h3
            id={indexTitleId}
            tabIndex={-1}
            data-focus-fallback=""
            className="mt-6 mb-3 text-base font-semibold outline-none"
          >
            노트북별 인덱스 현황
          </h3>
          {indexStatus.data === undefined ? (
            indexStatus.isError ? (
              <ErrorState
                title="인덱스 현황을 불러오지 못했습니다"
                onRetry={() => void indexStatus.refetch()}
                retrying={indexStatus.isFetching}
              />
            ) : (
              <LoadingState variant="table" count={3} label="인덱스 현황을 불러오는 중" />
            )
          ) : indexStatus.data.length === 0 ? (
            <EmptyState
              icon={<Database />}
              title="아직 노트북이 없습니다"
              description="노트북을 만들고 소스를 추가하면 여기에 인덱스 현황이 보입니다."
            />
          ) : (
            <>
              <AdminTableFrame>
                <Table aria-labelledby={indexTitleId}>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="max-md:sticky max-md:left-0 max-md:z-[1] max-md:bg-background">
                        노트북
                      </TableHead>
                      <TableHead className="text-right">소스</TableHead>
                      <TableHead className="text-right">준비</TableHead>
                      <TableHead className="text-right">실패</TableHead>
                      <TableHead className="text-right">처리 중</TableHead>
                      <TableHead className="text-right">재인덱싱 대기</TableHead>
                      <TableHead className="text-right">청크</TableHead>
                      <TableHead className="text-right">동작</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {pg.pageItems.map((s) => (
                      <TableRow key={s.notebook_id}>
                        <TableCell className="font-medium max-md:sticky max-md:left-0 max-md:z-[1] max-md:bg-background">
                          {s.title}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{s.source_count}</TableCell>
                        <TableCell className="text-right tabular-nums">{s.ready}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {s.failed > 0 ? <Badge variant="destructive">{s.failed}</Badge> : 0}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{s.processing}</TableCell>
                        <TableCell className="text-right tabular-nums">
                          {s.reindex_pending > 0 ? (
                            <Badge variant="secondary">{s.reindex_pending}</Badge>
                          ) : (
                            0
                          )}
                        </TableCell>
                        <TableCell className="text-right tabular-nums">{s.chunk_total}</TableCell>
                        <TableCell className="text-right">
                          {narrow ? (
                            // 좁은 화면(< 768)에서는 행 동작을 ⋯ 하나로 접는다(plan §6.7 — P5 재리뷰 P5-R01).
                            // 확인 없이 바로 요청하는 동작·잠금은 넓은 화면 버튼과 같다(잠긴 이유는 항목 아래 글자로).
                            <RowActions
                              rowLabel={s.title}
                              items={[
                                {
                                  label: "재인덱싱",
                                  disabled: busy,
                                  disabledReason: busy ? (busyReason ?? undefined) : undefined,
                                  onSelect: () => reindex.mutate({ notebook_id: s.notebook_id }),
                                },
                              ]}
                            />
                          ) : (
                            <Button
                              variant="ghost"
                              size="sm"
                              className="coarse:min-h-11"
                              onClick={() => reindex.mutate({ notebook_id: s.notebook_id })}
                              disabled={busy}
                              aria-describedby={describedBy}
                            >
                              재인덱싱
                            </Button>
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </AdminTableFrame>
              <TablePagination pagination={pg} />
            </>
          )}
        </section>

        {/* 소스 요약 백필(코어 v0.30.0 #454, #285) — 재인덱싱 잠금과 무관하다(다른 큐·다른 작업) */}
        <SourceDigestBackfillSection
          notebooks={(indexStatus.data ?? []).map((s) => ({ notebook_id: s.notebook_id, title: s.title }))}
        />

        {/* 섹션 3 — 위험 구역: 전체에 영향을 주는 운영 작업. 설정 저장과 같은 폼·같은 줄에 두지 않는다(AC-5.5). */}
        <DangerZone
          className="mt-0"
          // 잠긴 이유를 버튼 가까이에도 보인다(라이브 영역은 위 「인덱스 운영」의 상태 영역 하나다).
          description={
            busyReason
              ? `모든 소스에 영향을 주는 운영 작업입니다. 지금은 잠겨 있습니다 — ${busyReason}`
              : "모든 소스에 영향을 주는 운영 작업입니다. 진행 중인 재인덱싱이 있으면 새 요청을 받지 않습니다."
          }
        >
          <div className="flex flex-col gap-3 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="text-sm font-medium">전체 재인덱싱</p>
              <p className="text-sm text-muted-foreground">
                모든 소스를 다시 청킹·임베딩합니다. 소스 수에 따라 오래 걸립니다.
              </p>
            </div>
            <ConfirmButton
              type="button"
              variant="outline"
              className="shrink-0 text-destructive-text hover:text-destructive-text coarse:min-h-11 coarse:min-w-11"
              title="전체 소스를 재인덱싱할까요?"
              description="모든 소스를 다시 청킹·임베딩합니다. 소스 수에 따라 오래 걸리고 임베딩 모델에 따라 비용이 들 수 있습니다."
              actionLabel="재인덱싱"
              onConfirm={() => reindex.mutate({})}
              focusAfterConfirm={busyReasonRef}
              confirmDisabled={busy}
              confirmDisabledReason={busyReason ?? ""}
              disabled={busy}
              aria-describedby={describedBy}
            >
              전체 재인덱싱…
            </ConfirmButton>
          </div>
          <div className="flex flex-col gap-3 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <p className="text-sm font-medium">실패한 작업 재시도</p>
              <p className="text-sm text-muted-foreground">
                인덱싱에 실패한 소스만 다시 처리합니다. 실패한 소스가 없으면 아무것도 등록하지 않습니다.
              </p>
            </div>
            {/* 위험 구역의 버튼은 ConfirmDialog를 거친다(§6.5 DangerZone — P5 리뷰 P5-05, Orchestrator 판정).
                재시도는 파괴적 작업이 아니라 확인 버튼은 빨강이 아니다(§6.5 「빨강은 위험 동작만」). */}
            <ConfirmButton
              type="button"
              variant="outline"
              className="shrink-0 text-destructive-text hover:text-destructive-text coarse:min-h-11 coarse:min-w-11"
              title="실패한 작업을 다시 시도할까요?"
              description="인덱싱에 실패한 소스만 다시 처리합니다. 실패한 소스가 없으면 아무것도 등록하지 않습니다."
              actionLabel="재시도"
              destructive={false}
              onConfirm={() => reindex.mutate({ failed_only: true })}
              focusAfterConfirm={busyReasonRef}
              confirmDisabled={busy}
              confirmDisabledReason={busyReason ?? ""}
              disabled={busy}
              aria-describedby={describedBy}
            >
              실패한 작업 재시도…
            </ConfirmButton>
          </div>
        </DangerZone>
      </div>
    </>
  );
}
