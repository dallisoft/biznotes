"use client";

import { ChevronDown, Info, Lock, Server, Sparkles } from "lucide-react";
import Link from "next/link";
import { useId, useRef, useState } from "react";
import { AdminTableFrame } from "@/components/admin/admin-table-frame";
import { CreateModelDialog } from "@/components/admin/llm/create-model-dialog";
import { CreateProviderDialog } from "@/components/admin/llm/create-provider-dialog";
import { DiscoverModelsDialog } from "@/components/admin/llm/discover-models-dialog";
import { EditModelDialog } from "@/components/admin/llm/edit-model-dialog";
import { EditProviderDialog } from "@/components/admin/llm/edit-provider-dialog";
import { EmbeddingGateButton } from "@/components/admin/llm/embedding-gate-button";
import { FallbackChainSummary } from "@/components/admin/llm/fallback-chain-summary";
import { SyncModelsDialog } from "@/components/admin/llm/sync-models-dialog";
import { Toolbar } from "@/components/layout/toolbar";
import { PageHeader } from "@/components/shell/page-header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { EmptyState, FilteredEmptyState } from "@/components/ui/empty-state";
import { ErrorState } from "@/components/ui/error-state";
import { Input } from "@/components/ui/input";
import { LoadingState } from "@/components/ui/loading-state";
import { RowActions, type RowActionItem } from "@/components/ui/row-actions";
import { SegmentedControl } from "@/components/ui/segmented-control";
import { StatusBadge } from "@/components/ui/status-badge";
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
import { useAdminModels, useAdminProviders } from "@/hooks/use-admin-llm";
import { usePagination } from "@/hooks/use-pagination";
import type { AdminModel, AdminProvider, ProviderCircuit } from "@/lib/admin-types";
import { formatNumber } from "@/lib/format";
import { activeLabel, capabilityLabel } from "@/lib/labels";

type Providers = ReturnType<typeof useAdminProviders>;
type Models = ReturnType<typeof useAdminModels>;

/** 모델 능력 필터(§6.6.2 LLM 행) — 「전체」 외에는 그 능력을 가진 모델만 보인다. */
const CAPABILITY_FILTERS = [
  { value: "all", label: "전체" },
  { value: "llm", label: "텍스트 생성" },
  { value: "embedding", label: "임베딩" },
  { value: "image", label: "이미지 생성" },
] as const;
type CapabilityFilter = (typeof CAPABILITY_FILTERS)[number]["value"];

export default function AdminLLMPage() {
  const providers = useAdminProviders();
  const models = useAdminModels();

  return (
    <>
      <PageHeader
        breadcrumb={[{ label: "관리", href: "/admin" }, { label: "LLM 게이트웨이" }]}
        title="LLM 게이트웨이"
        description="프로바이더 연결·모델·폴백 우선순위를 관리합니다"
        actions={<CreateProviderDialog create={providers.create} />}
      />
      <div className="flex flex-col gap-8">
        <ProvidersSection providers={providers} />
        <ModelsSection providers={providers} models={models} />
      </div>
    </>
  );
}

// ---------------------------------------------------------------- 프로바이더

function ProvidersSection({ providers }: { providers: Providers }) {
  const titleId = useId();
  const list = providers.list.data ?? [];
  const pagination = usePagination(list);
  const circuitById = new Map((providers.circuits.data ?? []).map((c) => [c.id, c]));

  return (
    <section aria-labelledby={titleId}>
      <h2
        id={titleId}
        tabIndex={-1}
        data-focus-fallback=""
        className="mb-3 text-lg font-semibold outline-none"
      >
        프로바이더
      </h2>
      {providers.list.isPending ? (
        <LoadingState variant="table" count={3} label="프로바이더 목록을 불러오는 중" />
      ) : providers.list.isError ? (
        <ErrorState
          title="프로바이더 목록을 불러오지 못했습니다"
          onRetry={() => void providers.list.refetch()}
          retrying={providers.list.isFetching}
        />
      ) : list.length === 0 ? (
        <EmptyState
          icon={<Server />}
          title="아직 프로바이더가 없습니다"
          description="「프로바이더 추가」로 LLM 프로바이더를 연결하세요."
        />
      ) : (
        <>
          <AdminTableFrame>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>이름</TableHead>
                  <TableHead>유형</TableHead>
                  <TableHead>API 키</TableHead>
                  <TableHead className="text-right">모델 수</TableHead>
                  <TableHead>상태</TableHead>
                  <TableHead className="text-right">동작</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pagination.pageItems.map((p) => (
                  <ProviderRow
                    key={p.id}
                    provider={p}
                    circuit={circuitById.get(p.id)}
                    circuitsKnown={providers.circuits.isSuccess}
                    circuitsLoading={providers.circuits.isPending}
                    providers={providers}
                  />
                ))}
              </TableBody>
            </Table>
          </AdminTableFrame>
          <TablePagination pagination={pagination} />
        </>
      )}
    </section>
  );
}

/**
 * 프로바이더 상태(§6.6.2): 정상 / 실패 n회 / 차단 n초 + 「차단 해제」. 차단 상태를 아직 못 받았거나 받지 못하면
 * 「정상」으로 단정하지 않는다(확인 중 / 미확인).
 */
function ProviderStatus({
  provider,
  circuit,
  circuitsKnown,
  circuitsLoading,
  providers,
}: {
  provider: AdminProvider;
  circuit: ProviderCircuit | undefined;
  circuitsKnown: boolean;
  circuitsLoading: boolean;
  providers: Providers;
}) {
  if (circuit?.open) {
    return (
      <>
        <StatusBadge tone="danger">
          {`차단 ${formatNumber(circuit.open_remaining_seconds, { maximumFractionDigits: 0 })}초`}
        </StatusBadge>
        <Button
          variant="ghost"
          size="xs"
          className="coarse:min-h-11"
          aria-label={`${provider.name} 차단 해제`}
          disabled={providers.resetCircuit.isPending}
          onClick={() => providers.resetCircuit.mutate(provider.id)}
        >
          차단 해제
        </Button>
      </>
    );
  }
  if ((circuit?.recent_failures ?? 0) > 0) {
    return <StatusBadge tone="warning">{`실패 ${circuit?.recent_failures}회`}</StatusBadge>;
  }
  if (circuit || circuitsKnown) return <StatusBadge tone="success">정상</StatusBadge>;
  return <StatusBadge>{circuitsLoading ? "확인 중" : "미확인"}</StatusBadge>;
}

function ProviderRow({
  provider: p,
  circuit,
  circuitsKnown,
  circuitsLoading,
  providers,
}: {
  provider: AdminProvider;
  circuit: ProviderCircuit | undefined;
  circuitsKnown: boolean;
  circuitsLoading: boolean;
  providers: Providers;
}) {
  // 더보기 메뉴로 연 다이얼로그는 닫힐 때 이 행의 `⋯`로 포커스를 돌린다(메뉴 항목은 이미 사라졌다).
  const moreRef = useRef<HTMLButtonElement>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [discoverOpen, setDiscoverOpen] = useState(false);
  const [syncOpen, setSyncOpen] = useState(false);
  const discoverable = p.type !== "mock";

  const items: RowActionItem[] = [
    {
      label: "연결 테스트",
      onSelect: () => providers.test.mutate(p.id),
      disabled: providers.test.isPending,
    },
    ...(discoverable ? [{ label: "모델 가져오기", onSelect: () => setDiscoverOpen(true) }] : []),
    ...(discoverable && p.model_count > 0
      ? [{ label: "모델 동기화 확인", onSelect: () => setSyncOpen(true) }]
      : []),
    {
      destructive: true,
      label: "삭제",
      confirm: {
        title: `‘${p.name}’ 프로바이더를 삭제할까요?`,
        description:
          (p.model_count > 0 ? `연결된 모델 ${p.model_count}개도 함께 삭제됩니다. ` : "") +
          "되돌릴 수 없습니다.",
        actionLabel: "삭제",
      },
      onConfirm: () => providers.remove.mutate(p.id),
    },
  ];

  return (
    <TableRow>
      <TableCell className="font-medium">{p.name}</TableCell>
      <TableCell>
        <span className="font-mono text-xs">{p.type}</span>
      </TableCell>
      <TableCell>
        {p.has_api_key ? (
          <span className="flex items-center gap-1 text-muted-foreground">
            <Lock aria-hidden className="size-3" />
            저장됨
          </span>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell className="text-right tabular-nums">{formatNumber(p.model_count)}</TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-1">
          <ProviderStatus
            provider={p}
            circuit={circuit}
            circuitsKnown={circuitsKnown}
            circuitsLoading={circuitsLoading}
            providers={providers}
          />
          <EmbeddingGateButton provider={p} />
        </div>
      </TableCell>
      <TableCell>
        <RowActions
          foldPrimaryOnNarrow
          rowLabel={p.name}
          primary={{ label: "수정", onClick: () => setEditOpen(true) }}
          items={items}
          triggerRef={moreRef}
        />
        <EditProviderDialog
          provider={p}
          update={providers.update}
          open={editOpen}
          onOpenChange={setEditOpen}
        />
        {discoverable && (
          <DiscoverModelsDialog
            provider={p}
            open={discoverOpen}
            onOpenChange={setDiscoverOpen}
            returnFocusTo={moreRef}
          />
        )}
        {discoverable && p.model_count > 0 && (
          <SyncModelsDialog
            provider={p}
            open={syncOpen}
            onOpenChange={setSyncOpen}
            returnFocusTo={moreRef}
          />
        )}
      </TableCell>
    </TableRow>
  );
}

// -------------------------------------------------------------------- 모델

function ModelsSection({ providers, models }: { providers: Providers; models: Models }) {
  const titleId = useId();
  const [capability, setCapability] = useState<CapabilityFilter>("all");
  const all = models.list.data ?? [];
  const visible = capability === "all" ? all : all.filter((m) => m.capabilities.includes(capability));
  const pagination = usePagination(visible);

  return (
    <section aria-labelledby={titleId}>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2
          id={titleId}
          tabIndex={-1}
          data-focus-fallback=""
          className="text-lg font-semibold outline-none"
        >
          모델
        </h2>
        <CreateModelDialog providers={providers.list.data ?? []} create={models.create} />
      </div>
      <Toolbar>
        <SegmentedControl
          label="능력"
          options={CAPABILITY_FILTERS}
          value={capability}
          onValueChange={(next) => {
            setCapability(next);
            pagination.setPage(1);
          }}
        />
      </Toolbar>
      {models.list.isPending ? (
        <LoadingState variant="table" label="모델 목록을 불러오는 중" />
      ) : models.list.isError ? (
        <ErrorState
          title="모델 목록을 불러오지 못했습니다"
          onRetry={() => void models.list.refetch()}
          retrying={models.list.isFetching}
        />
      ) : all.length === 0 ? (
        <EmptyState
          icon={<Sparkles />}
          title="아직 모델이 없습니다"
          description="「모델 추가」로 프로바이더의 모델을 등록하세요."
        />
      ) : visible.length === 0 ? (
        <FilteredEmptyState onClearFilters={() => setCapability("all")} />
      ) : (
        <>
          <AdminTableFrame>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>표시명</TableHead>
                  <TableHead>프로바이더</TableHead>
                  <TableHead>능력</TableHead>
                  <TableHead>기본</TableHead>
                  <TableHead title="1차 모델(기본 모델 또는 채팅에서 고른 모델) 다음에 시도할 순서 — 낮을수록 먼저, 비워 두면 폴백에서 제외">
                    폴백 순서
                  </TableHead>
                  <TableHead>상태</TableHead>
                  <TableHead className="text-right">동작</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pagination.pageItems.map((m) => (
                  <ModelRow key={m.id} model={m} models={models} />
                ))}
              </TableBody>
            </Table>
          </AdminTableFrame>
          <TablePagination pagination={pagination} />
        </>
      )}
      {(capability === "all" || capability === "llm") && providers.list.isSuccess && (
        <FallbackChainSummary models={all} providers={providers.list.data} />
      )}
      <EmbeddingSwapGuide />
    </section>
  );
}

function ModelRow({ model: m, models }: { model: AdminModel; models: Models }) {
  const switchRef = useRef<HTMLElement>(null);
  const fallbackHintId = useId();
  const [editOpen, setEditOpen] = useState(false);
  const [confirmOff, setConfirmOff] = useState(false);
  const isEmbedding = m.capabilities.includes("embedding");
  // 활성 기본 모델은 1차로 먼저 시도되므로 자기 폴백 순서는 다른 모델이 1차일 때만 쓰인다(코어 resolve_chain).
  const fallbackOnlyWhenOtherPrimary = m.is_default && m.enabled;
  const toggling = models.toggle.isPending && models.toggle.variables?.id === m.id;

  const items: RowActionItem[] = [
    ...(m.is_default
      ? []
      : [{ label: "기본 모델로 지정", onSelect: () => models.setDefault.mutate(m.id) }]),
    {
      destructive: true,
      label: "삭제",
      confirm: {
        title: `‘${m.display_name}’ 모델을 삭제할까요?`,
        description: isEmbedding
          ? "이 모델로 만든 벡터는 다른 모델로 재인덱싱이 끝날 때까지 검색할 수 없습니다. 되돌릴 수 없습니다."
          : "되돌릴 수 없습니다.",
        actionLabel: "삭제",
      },
      onConfirm: () => models.remove.mutate(m.id),
    },
  ];

  return (
    <TableRow>
      <TableCell>
        <span className="block font-medium">{m.display_name}</span>
        <span className="block font-mono text-xs text-muted-foreground">{m.model_name}</span>
      </TableCell>
      <TableCell>{m.provider_name}</TableCell>
      <TableCell>
        <div className="flex flex-wrap gap-1">
          {m.capabilities.map((c) => (
            <Badge key={c} variant="secondary">
              {capabilityLabel(c)}
            </Badge>
          ))}
        </div>
      </TableCell>
      <TableCell>
        {m.is_default ? (
          <StatusBadge tone="info">기본</StatusBadge>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell>
        {m.capabilities.includes("llm") ? (
          <>
            <Input
              // 서버 값이 바뀌면 입력을 다시 심는다(비제어 입력)
              key={`${m.id}:${m.fallback_priority ?? ""}`}
              type="number"
              min={1}
              aria-label={`${m.display_name} 폴백 순서`}
              aria-describedby={fallbackOnlyWhenOtherPrimary ? fallbackHintId : undefined}
              className="h-7 w-16 text-xs coarse:h-11"
              defaultValue={m.fallback_priority ?? ""}
              placeholder="—"
              onBlur={(e) => {
                const v = e.target.value === "" ? null : Number(e.target.value);
                if (v !== m.fallback_priority) {
                  models.setFallback.mutate({ id: m.id, fallback_priority: v });
                }
              }}
            />
            {fallbackOnlyWhenOtherPrimary && (
              <p id={fallbackHintId} className="mt-1 text-xs text-muted-foreground">
                다른 모델이 1차일 때만 적용
              </p>
            )}
          </>
        ) : (
          <span className="text-muted-foreground">—</span>
        )}
      </TableCell>
      <TableCell>
        <div className="flex items-center gap-2">
          <Switch
            ref={switchRef}
            checked={m.enabled}
            disabled={toggling}
            aria-label={`${m.display_name} 활성`}
            onCheckedChange={(checked) => {
              // 활성 임베딩 모델 끄기는 검색·새 소스 임베딩에 영향 — 확인을 받는다(DP-5, D-10).
              if (!checked && m.enabled && isEmbedding) {
                setConfirmOff(true);
                return;
              }
              models.toggle.mutate({ id: m.id, enabled: checked });
            }}
          />
          <span className="text-xs text-muted-foreground">{activeLabel(m.enabled)}</span>
        </div>
        {isEmbedding && (
          <ConfirmDialog
            open={confirmOff}
            onOpenChange={setConfirmOff}
            title={`‘${m.display_name}’ 임베딩 모델을 끌까요?`}
            description="끄면 이 모델은 새 소스의 임베딩에 쓰이지 않습니다(활성 기본 임베딩 모델이 없으면 로컬 기본 모델을 씁니다). 이 모델로 이미 만든 벡터는 계속 검색됩니다. 모델을 교체하는 중이라면 아래 「임베딩 모델 교체 순서」대로 진행하세요."
            actionLabel="비활성화"
            onConfirm={() => models.toggle.mutate({ id: m.id, enabled: false })}
            returnFocusTo={switchRef}
          />
        )}
      </TableCell>
      <TableCell>
        <RowActions
          foldPrimaryOnNarrow
          rowLabel={m.display_name}
          primary={{ label: "수정", onClick: () => setEditOpen(true) }}
          items={items}
        />
        <EditModelDialog
          model={m}
          update={models.update}
          open={editOpen}
          onOpenChange={setEditOpen}
        />
      </TableCell>
    </TableRow>
  );
}

/**
 * 임베딩 모델 교체 안내 — 접이식 info 콜아웃(§6.6.2 LLM 행). 코어 v0.9.1의 5단계 절차를 그대로 따른다
 * (`biznotes_core/services/embedding_spaces.py` 모듈 설명). HTTP 상태 코드는 보이지 않는다.
 */
function EmbeddingSwapGuide() {
  return (
    <details
      data-testid="embedding-swap-guide"
      className="group mt-4 rounded-xl border border-info/40 bg-info/5 p-3"
    >
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-md text-sm font-medium outline-none focus-visible:ring-3 focus-visible:ring-ring/50 coarse:min-h-11 [&::-webkit-details-marker]:hidden">
        <Info aria-hidden className="size-4 shrink-0 text-info-text" />
        임베딩 모델 교체 순서
        <ChevronDown
          aria-hidden
          className="ml-auto size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180"
        />
      </summary>
      <p className="mt-2 text-sm text-muted-foreground">
        소스는 자기 벡터를 만든 모델의 전용 공간에서 검색됩니다. 아래 순서를 지키면 교체 도중에도 기존 소스가
        검색에서 빠지지 않습니다. 기존 모델을 먼저 삭제하면 재인덱싱이 끝날 때까지 그 모델로 만든 벡터를 질의할
        수 없습니다.
      </p>
      <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
        <li>기존 임베딩 모델을 비활성화합니다 (위 표의 활성 스위치 끄기).</li>
        <li>새 임베딩 모델을 등록합니다 — 활성 임베딩 모델이 남아 있으면 등록이 거부됩니다.</li>
        <li>새 모델을 기본 모델로 지정합니다 (행의 더보기 메뉴 「기본 모델로 지정」).</li>
        <li>
          <Link href="/admin/rag" className="text-primary underline-offset-4 hover:underline">
            RAG 관리
          </Link>
          에서 <strong>전체 재인덱싱</strong>을 실행하고, 노트북별 인덱스 현황의 &apos;처리 중&apos;이 0이 되고
          &apos;실패&apos;가 남지 않을 때까지 기다립니다 (실패가 남으면 &apos;실패한 작업 재시도&apos;로 해소).
          모델이 바뀐 소스만 골라내는 재인덱싱은 준비 중이라 지금은 전체 재인덱싱으로 대체하며, 진행률은 RAG
          관리의 진행 패널에서 확인합니다.
        </li>
        <li>기존 모델을 삭제합니다.</li>
      </ol>
    </details>
  );
}
