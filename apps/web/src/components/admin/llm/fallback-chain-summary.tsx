"use client";

import { ChevronRight, ListOrdered } from "lucide-react";
import { useId } from "react";
import { StatusBadge } from "@/components/ui/status-badge";
import type { AdminModel, AdminProvider } from "@/lib/admin-types";

export interface FallbackChain {
  /** 1차 모델 — 활성 기본 텍스트 생성 모델. 없으면 null(게이트웨이는 표시명 순 첫 활성 모델을 쓴다). */
  primary: AdminModel | null;
  /** 1차 다음에 시도하는 후보 — 폴백 순서 오름차순. */
  fallbacks: AdminModel[];
  /** 후보 사이에서 겹친 폴백 순서 번호 — 이 번호를 쓰는 모델끼리는 시도 순서가 정해지지 않는다. */
  duplicatePriorities: number[];
  /** 쓸 수 있는(모델·프로바이더 모두 활성) 텍스트 생성 모델이 하나라도 있는지. */
  hasUsable: boolean;
}

/**
 * 기본 모델로 시작하는 폴백 요청의 텍스트 생성 시도 순서. 코어 게이트웨이 `resolve_chain`
 * (`biznotes_core/services/gateway/llm.py`)과 같은 규칙이다 — 1차는 활성 기본 모델이고, 그 뒤는 활성 모델
 * (프로바이더도 활성) 가운데 폴백 순서가 있는 모델을 오름차순으로 잇는다. 1차 모델은 폴백 순서가 있어도
 * 뒤에서 뺀다. 도구 호출 턴(`complete_with_tools`)은 이 체인을 쓰지 않고 1차 모델만 쓴다.
 */
export function buildFallbackChain(models: AdminModel[], providers: AdminProvider[]): FallbackChain {
  const providerEnabled = new Map(providers.map((p) => [p.id, p.enabled]));
  const usable = models.filter(
    (m) =>
      m.capabilities.includes("llm") && m.enabled && providerEnabled.get(m.provider_id) === true,
  );
  const primary = usable.find((m) => m.is_default) ?? null;
  const fallbacks = usable
    .filter((m) => m.fallback_priority != null && m.id !== primary?.id)
    .sort((a, b) => (a.fallback_priority ?? 0) - (b.fallback_priority ?? 0));

  const seen = new Set<number>();
  const duplicates = new Set<number>();
  for (const m of fallbacks) {
    const priority = m.fallback_priority ?? 0;
    if (seen.has(priority)) duplicates.add(priority);
    seen.add(priority);
  }
  return { primary, fallbacks, duplicatePriorities: [...duplicates], hasUsable: usable.length > 0 };
}

/**
 * 텍스트 생성 실행 순서 미리보기 — 표의 「폴백 순서」 숫자만으로는 1차 모델이 항상 먼저 시도된다는 점이
 * 보이지 않아, 기본 모델에 적은 번호가 실제 시도 순서로 오해되기 쉽다.
 */
export function FallbackChainSummary({
  models,
  providers,
}: {
  models: AdminModel[];
  providers: AdminProvider[];
}) {
  const titleId = useId();
  if (!models.some((m) => m.capabilities.includes("llm"))) return null;

  const { primary, fallbacks, duplicatePriorities, hasUsable } = buildFallbackChain(
    models,
    providers,
  );

  return (
    <section
      aria-labelledby={titleId}
      data-testid="fallback-chain"
      className="mt-4 rounded-xl border border-border bg-muted/30 p-3"
    >
      <h3 id={titleId} className="flex items-center gap-2 text-sm font-medium">
        <ListOrdered aria-hidden className="size-4 shrink-0 text-muted-foreground" />
        텍스트 생성 실행 순서
      </h3>
      {primary ? (
        <ol
          aria-label="기본 모델로 시작하는 폴백 시도 순서"
          className="mt-2 flex flex-wrap items-center gap-x-1 gap-y-2 text-sm"
        >
          {[primary, ...fallbacks].map((m, i) => (
            <li key={m.id} className="flex items-center gap-1">
              {i > 0 && <ChevronRight aria-hidden className="size-4 shrink-0 text-muted-foreground" />}
              <span className="font-medium">{m.display_name}</span>
              {i === 0 ? (
                <StatusBadge tone="info">기본</StatusBadge>
              ) : (
                <StatusBadge>{`폴백 ${m.fallback_priority}`}</StatusBadge>
              )}
            </li>
          ))}
        </ol>
      ) : (
        <p className="mt-2 text-sm text-warning-text">
          {hasUsable
            ? "활성 기본 모델이 없어, 모델을 고르지 않은 요청은 표시명 순서상 첫 활성 모델로 처리됩니다. 행의 더보기 메뉴 「기본 모델로 지정」으로 기본 모델을 정하세요."
            : "활성 텍스트 생성 모델이 없어 요청이 실패합니다."}
        </p>
      )}
      {primary && fallbacks.length === 0 && (
        <p className="mt-2 text-xs text-warning-text">
          폴백 후보가 없어 기본 모델이 실패하면 요청도 실패합니다.
        </p>
      )}
      {duplicatePriorities.length > 0 && (
        <p className="mt-2 text-xs text-warning-text">
          {`같은 폴백 순서 번호(${duplicatePriorities.join(", ")})를 여러 모델이 쓰고 있어 그 모델들 사이의 시도 순서가 정해지지 않습니다. 서로 다른 번호를 적으세요.`}
        </p>
      )}
      <p className="mt-2 text-xs text-muted-foreground">
        폴백을 쓰는 요청(소스 요약, 도구 없는 「LLM 생성」 노드 등)은 기본 모델부터 이 순서로 시도합니다. 1차
        모델은 폴백 순서와 상관없이 가장 먼저 시도하므로, 기본 모델에 적은 폴백 순서는 다른 모델이 1차일 때만
        쓰입니다. 비활성 모델과, 비활성이거나 차단된 프로바이더의 모델은 건너뜁니다.
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        채팅의 도구 호출 단계와 도구를 쓰는 「LLM 생성」 노드는 폴백 없이 1차 모델만 씁니다. 1차 모델이 실패하면
        다음 후보로 넘어가지 않고 요청이 실패합니다.
      </p>
    </section>
  );
}
