"use client";

import type { UseQueryResult } from "@tanstack/react-query";
import { AlertTriangle, CircleCheck, RotateCw, ScrollText } from "lucide-react";
import Link from "next/link";
import { useId } from "react";
import { PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { LoadingState } from "@/components/ui/loading-state";
import { Skeleton } from "@/components/ui/skeleton";
import { StatCard } from "@/components/ui/stat-card";
import { StatusBadge, type StatusTone } from "@/components/ui/status-badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  type AdminOverviewQueries,
  countActiveReindexRuns,
  RECENT_AUDIT_LOG_COUNT,
  summarizeIndex,
  summarizeMcpServers,
  summarizeModels,
  summarizeProviders,
  summarizeUsers,
  useAdminOverview,
} from "@/hooks/use-admin-overview";
import { type RetryFocusStatus, useRetryFocusLanding } from "@/hooks/use-retry-focus-landing";
import type { ApiError } from "@/lib/api-client";
import { EMPTY, formatDateTime, formatNumber } from "@/lib/format";
import { auditActionLabel, capabilityLabel } from "@/lib/labels";

type AnyQuery = UseQueryResult<unknown, ApiError>;
type CardState = "loading" | "error" | "success";

/**
 * 파생 상태 규칙(§6.2.6): 카드 값은 그 카드의 쿼리가 **모두 성공했을 때만** 보인다. 하나라도 실패면 실패,
 * 아직 응답이 없으면 로딩 — 실패·로딩을 0이나 「정상」으로 그리지 않는다. 새로고침이 실패하면 이전 값이
 * 남아 있어도 실패로 본다(오래된 값을 이번 기준 시각의 값처럼 보이지 않게).
 */
function cardState(queries: readonly AnyQuery[]): CardState {
  if (queries.some((q) => q.isError)) return "error";
  if (queries.some((q) => q.data === undefined)) return "loading";
  return "success";
}

function refetchAll(queries: readonly AnyQuery[]) {
  for (const q of queries) void q.refetch();
}

const STATE_TEXT: Record<Exclude<CardState, "success">, string> = {
  loading: "불러오는 중",
  error: "불러오지 못했습니다",
};

interface CardModel {
  id: string;
  label: string;
  href: string;
  linkLabel: string;
  queries: AnyQuery[];
  /** 성공 상태에서만 부른다 — 쿼리 데이터가 모두 있다 */
  value: () => { value: string; hint?: string; linkLabel?: string };
}

function count(n: number, unit: string) {
  return `${formatNumber(n)}${unit}`;
}

function buildCards(q: AdminOverviewQueries): CardModel[] {
  return [
    {
      id: "users",
      label: "사용자",
      href: "/admin/users",
      linkLabel: "사용자·그룹에서 보기",
      queries: [q.users],
      value: () => {
        const s = summarizeUsers(q.users.data!);
        return { value: count(s.total, "명"), hint: `활성 ${count(s.active, "명")}` };
      },
    },
    {
      id: "providers",
      label: "LLM 프로바이더",
      href: "/admin/llm",
      linkLabel: "LLM 게이트웨이에서 보기",
      queries: [q.providers, q.providerStatus],
      value: () => {
        const s = summarizeProviders(q.providers.data!, q.providerStatus.data!);
        const parts = [`정상 ${formatNumber(s.healthy)}`, `차단 ${formatNumber(s.blocked.length)}`];
        if (s.disabled > 0) parts.push(`비활성 ${formatNumber(s.disabled)}`);
        return { value: count(s.total, "개"), hint: parts.join(" / ") };
      },
    },
    {
      id: "models",
      label: "활성 모델",
      href: "/admin/llm",
      linkLabel: "LLM 게이트웨이에서 보기",
      queries: [q.models],
      value: () => {
        const s = summarizeModels(q.models.data!);
        return {
          value: `${formatNumber(s.enabled)} / ${formatNumber(s.total)}`,
          hint:
            s.missingDefaults.length > 0
              ? `기본 모델 미지정: ${s.missingDefaults.map(capabilityLabel).join(", ")}`
              : "기본 모델 미지정 능력 없음",
        };
      },
    },
    {
      id: "mcp",
      label: "MCP 서버",
      href: "/admin/mcp",
      linkLabel: "MCP 서버에서 보기",
      queries: [q.mcpServers],
      value: () => {
        const s = summarizeMcpServers(q.mcpServers.data!);
        return {
          value: count(s.total, "개"),
          hint: `정상 ${formatNumber(s.healthy)} / 오류 ${formatNumber(s.error)} / 미확인 ${formatNumber(s.unknown)}`,
        };
      },
    },
    {
      id: "index",
      label: "인덱스",
      href: "/admin/rag",
      linkLabel: "RAG에서 보기",
      queries: [q.indexStatus],
      value: () => {
        const s = summarizeIndex(q.indexStatus.data!);
        return {
          value: `노트북 ${count(s.notebooks, "개")}`,
          hint: `실패 ${formatNumber(s.failed)} · 처리 중 ${formatNumber(s.processing)}`,
        };
      },
    },
    {
      id: "reindex",
      label: "진행 중 재인덱싱",
      href: "/admin/rag",
      linkLabel: "RAG에서 보기",
      queries: [q.activeReindex],
      // 스냅샷이라 진행률을 그리지 않는다 — 실시간 진행률은 RAG 화면에서 본다(§6.2.6·목업 D6).
      value: () => {
        const active = countActiveReindexRuns(q.activeReindex.data!);
        return active > 0
          ? { value: `진행 중 ${count(active, "건")}`, linkLabel: "RAG에서 실시간 진행률 보기" }
          : { value: "없음" };
      },
    },
    {
      id: "reviews",
      label: "스킬 검토 대기",
      href: "/admin/skills",
      linkLabel: "스킬 공개에서 보기",
      queries: [q.pendingReviews],
      value: () => ({ value: count(q.pendingReviews.data!.length, "건") }),
    },
  ];
}

/** 로딩·실패 카드 — StatCard와 같은 틀(라벨 · 본문 · 아래 링크). 값 자리에는 숫자를 두지 않는다. */
function PendingCard({
  card,
  state,
  onRetry,
}: {
  card: CardModel;
  state: Exclude<CardState, "success">;
  onRetry: () => void;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{card.label}</CardDescription>
        {state === "loading" && (
          <div role="status">
            <span className="sr-only">{`${card.label} ${STATE_TEXT.loading}`}</span>
            <Skeleton aria-hidden className="h-8 w-24" />
          </div>
        )}
      </CardHeader>
      {state === "error" && (
        <CardContent className="pt-0">
          <ErrorState
            title={STATE_TEXT.error}
            description=""
            onRetry={onRetry}
            retrying={card.queries.some((q) => q.isFetching)}
            className="items-start gap-2 border-0 p-0 text-left"
          />
        </CardContent>
      )}
      <CardContent className="pt-0 text-sm">
        <Link
          href={card.href}
          aria-label={`${card.linkLabel} — ${card.label}`}
          className="rounded-sm text-primary underline-offset-4 hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          {card.linkLabel}
        </Link>
      </CardContent>
    </Card>
  );
}

/** 그리드 칸 — 같은 줄 카드의 높이를 맞춘다(보조 문구가 없는 카드도 줄 높이만큼). */
const CARD_CELL = "min-w-0 [&>[data-slot=card]]:h-full";

/** 카드 상태 → 「다시 시도」 포커스 경계의 상태(로딩 = pending). */
function retryStatusOf(state: CardState): RetryFocusStatus {
  return state === "loading" ? "pending" : state;
}

function OverviewCard({ card }: { card: CardModel }) {
  const state = cardState(card.queries);
  // 카드 「다시 시도」의 포커스 경계(P2 리뷰 P2-01) — 오류 → 로딩 → 성공·실패 내내 카드 칸 안(공용 규칙 — P6에서 훅으로 합침).
  // 칸은 포커스를 받지 않으므로 로딩 중에도 칸 안 첫 링크로 간다.
  const { regionRef: cellRef, markRetry } = useRetryFocusLanding<HTMLDivElement>(retryStatusOf(state));
  if (state !== "success") {
    return (
      <div ref={cellRef} data-testid={`overview-card-${card.id}`} data-state={state} className={CARD_CELL}>
        <PendingCard
          card={card}
          state={state}
          onRetry={() => {
            markRetry();
            refetchAll(card.queries);
          }}
        />
      </div>
    );
  }
  const shown = card.value();
  return (
    <div ref={cellRef} data-testid={`overview-card-${card.id}`} data-state={state} className={CARD_CELL}>
      <StatCard
        label={card.label}
        value={shown.value}
        hint={shown.hint}
        href={card.href}
        linkLabel={shown.linkLabel ?? card.linkLabel}
      />
    </div>
  );
}

interface AttentionItem {
  id: string;
  tone: StatusTone;
  badge: string;
  text: string;
  href: string;
  linkLabel: string;
}

/** 「주의가 필요한 항목」 — 각 항목은 해당 카드가 성공했을 때만 판정한다. */
function attentionItems(cards: Map<string, CardModel>, q: AdminOverviewQueries): AttentionItem[] {
  const ok = (id: string) => cardState(cards.get(id)!.queries) === "success";
  const items: AttentionItem[] = [];
  if (ok("providers")) {
    const s = summarizeProviders(q.providers.data!, q.providerStatus.data!);
    if (s.blocked.length > 0) {
      items.push({
        id: "providers",
        tone: "danger",
        badge: "차단",
        text: `차단된 LLM 프로바이더 ${count(s.blocked.length, "개")} — ${s.blocked.join(", ")}`,
        href: "/admin/llm",
        linkLabel: "LLM 게이트웨이에서 확인",
      });
    }
  }
  if (ok("mcp")) {
    const s = summarizeMcpServers(q.mcpServers.data!);
    if (s.error > 0) {
      items.push({
        id: "mcp",
        tone: "danger",
        badge: "오류",
        text: `상태 확인에 실패한 MCP 서버 ${count(s.error, "개")}`,
        href: "/admin/mcp",
        linkLabel: "MCP 서버에서 확인",
      });
    }
  }
  if (ok("index")) {
    const s = summarizeIndex(q.indexStatus.data!);
    if (s.failed > 0) {
      items.push({
        id: "index",
        tone: "danger",
        badge: "실패",
        text: `인덱싱에 실패한 소스 ${count(s.failed, "개")}(노트북 ${count(s.notebooksWithFailures, "개")})`,
        href: "/admin/rag",
        linkLabel: "RAG에서 확인",
      });
    }
  }
  if (ok("reviews")) {
    const n = q.pendingReviews.data!.length;
    if (n > 0) {
      items.push({
        id: "reviews",
        tone: "warning",
        badge: "대기",
        text: `검토를 기다리는 스킬 공개 요청 ${count(n, "건")}`,
        href: "/admin/skills",
        linkLabel: "스킬 공개에서 확인",
      });
    }
  }
  if (ok("models")) {
    const s = summarizeModels(q.models.data!);
    if (s.missingDefaults.length > 0) {
      items.push({
        id: "models",
        tone: "warning",
        badge: "미지정",
        text: `기본 모델이 지정되지 않은 능력 — ${s.missingDefaults.map(capabilityLabel).join(", ")}`,
        href: "/admin/llm",
        linkLabel: "LLM 게이트웨이에서 확인",
      });
    }
  }
  return items;
}

/** 주의 항목 판정에 쓰는 카드(§6.2.6 — 차단 프로바이더·MCP 오류·인덱스 실패·검토 대기·기본 모델 미지정). */
const ATTENTION_CARD_IDS = ["providers", "mcp", "index", "reviews", "models"] as const;

function AttentionSection({
  cards,
  queries,
}: {
  cards: Map<string, CardModel>;
  queries: AdminOverviewQueries;
}) {
  const titleId = useId();
  const items = attentionItems(cards, queries);
  const unresolved = ATTENTION_CARD_IDS.map((id) => cards.get(id)!).flatMap((card) => {
    const state = cardState(card.queries);
    return state === "success" ? [] : [{ label: card.label, state }];
  });
  return (
    <section aria-labelledby={titleId} data-testid="overview-attention">
      <h2 id={titleId} className="mb-3 text-lg font-semibold">
        주의가 필요한 항목
      </h2>
      {items.length > 0 && (
        <ul className="flex flex-col divide-y rounded-xl border bg-card">
          {items.map((item) => (
            <li
              key={item.id}
              data-testid={`attention-${item.id}`}
              className="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="flex min-w-0 items-center gap-2">
                <StatusBadge tone={item.tone}>{item.badge}</StatusBadge>
                <span className="min-w-0 text-sm">{item.text}</span>
              </div>
              <Link
                href={item.href}
                className="shrink-0 rounded-sm text-sm text-primary underline-offset-4 hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
              >
                {item.linkLabel}
              </Link>
            </li>
          ))}
        </ul>
      )}
      {unresolved.length > 0 ? (
        <div
          data-testid="attention-unresolved"
          className={
            items.length > 0
              ? "mt-3 flex items-start gap-3 rounded-xl border px-4 py-3"
              : "flex items-start gap-3 rounded-xl border px-4 py-3"
          }
        >
          <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-warning-text" />
          <div className="min-w-0 text-sm">
            <p className="font-medium">일부 상태를 확인하지 못했습니다</p>
            <p className="mt-0.5 text-muted-foreground">
              {unresolved.map((u) => `${u.label}(${STATE_TEXT[u.state]})`).join(", ")}
            </p>
          </div>
        </div>
      ) : (
        items.length === 0 && (
          <EmptyState
            icon={<CircleCheck />}
            title="지금 처리할 항목이 없습니다"
            className="py-8"
          />
        )
      )}
    </section>
  );
}

function RecentAuditLogs({ query }: { query: AdminOverviewQueries["auditLogs"] }) {
  const titleId = useId();
  const rows = query.data?.slice(0, RECENT_AUDIT_LOG_COUNT) ?? [];
  return (
    <section aria-labelledby={titleId} data-testid="overview-audit">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={titleId} className="text-lg font-semibold">
          최근 감사 로그
        </h2>
        <Link
          href="/admin/audit"
          className="rounded-sm text-sm text-primary underline-offset-4 hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          감사 로그에서 모두 보기
        </Link>
      </div>
      {query.isError ? (
        <ErrorState
          title="감사 로그를 불러오지 못했습니다"
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        />
      ) : query.data === undefined ? (
        <LoadingState variant="table" count={RECENT_AUDIT_LOG_COUNT} label="감사 로그 불러오는 중" />
      ) : rows.length === 0 ? (
        <EmptyState icon={<ScrollText />} title="아직 감사 로그가 없습니다" className="py-8" />
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>시각</TableHead>
              <TableHead>사용자</TableHead>
              <TableHead>작업</TableHead>
              <TableHead>대상</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((log) => {
              const label = auditActionLabel(log.action);
              return (
                <TableRow key={log.id}>
                  <TableCell className="whitespace-nowrap tabular-nums">
                    {formatDateTime(log.created_at, { seconds: true })}
                  </TableCell>
                  <TableCell>{log.user_email ?? EMPTY}</TableCell>
                  <TableCell>
                    {label}
                    {label !== log.action && (
                      <span className="block font-mono text-xs text-muted-foreground">
                        {log.action}
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs break-all">{log.resource || EMPTY}</TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      )}
    </section>
  );
}

/** 성공한 쿼리 중 가장 이른 갱신 시각 — 화면의 값은 적어도 이 시각 기준이다. 성공한 쿼리가 없으면 null. */
function snapshotTime(queries: readonly AnyQuery[]): number | null {
  const times = queries.filter((q) => q.isSuccess && q.dataUpdatedAt > 0).map((q) => q.dataUpdatedAt);
  return times.length > 0 ? Math.min(...times) : null;
}

/**
 * 관리 개요(§6.2.6, 목업 D6) — 관리자가 처리할 일과 시스템 상태를 한 화면에서 본다. `AdminLayout`(관리자
 * 가드) 안에서만 렌더된다. 폴링하지 않는 스냅샷이며 「새로고침」으로 모든 쿼리를 다시 읽는다.
 */
export default function AdminOverviewPage() {
  const queries = useAdminOverview();
  const all: AnyQuery[] = Object.values(queries);
  const cards = buildCards(queries);
  const cardMap = new Map(cards.map((c) => [c.id, c]));
  const fetching = all.some((q) => q.isFetching);
  const snapshot = snapshotTime(all);
  const statusTitleId = useId();

  return (
    <div className="flex flex-col gap-8">
      <PageHeader
        breadcrumb={[{ label: "관리" }]}
        title="관리 개요"
        description="시스템 상태와 처리할 일을 한눈에 확인합니다"
        actions={
          <>
            {snapshot !== null && (
              <p className="text-sm text-muted-foreground" data-testid="overview-snapshot">
                기준 시각{" "}
                <time dateTime={new Date(snapshot).toISOString()}>
                  {formatDateTime(snapshot, { seconds: true })}
                </time>
              </p>
            )}
            <Button
              variant="outline"
              onClick={() => refetchAll(all)}
              disabled={fetching}
              // 다시 읽는 동안에도 키보드 포커스를 잃지 않게 한다(비활성 버튼은 포커스가 body로 빠진다)
              focusableWhenDisabled
              className="coarse:h-11"
            >
              <RotateCw aria-hidden className={fetching ? "motion-safe:animate-spin" : undefined} />
              새로고침
            </Button>
          </>
        }
      />
      <AttentionSection cards={cardMap} queries={queries} />
      <section aria-labelledby={statusTitleId}>
        <h2 id={statusTitleId} className="mb-3 text-lg font-semibold">
          현황
        </h2>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-4">
          {cards.map((card) => (
            <OverviewCard key={card.id} card={card} />
          ))}
        </div>
      </section>
      <RecentAuditLogs query={queries.auditLogs} />
    </div>
  );
}
