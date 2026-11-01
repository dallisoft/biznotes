"use client";

import type { UseQueryResult } from "@tanstack/react-query";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { RerankerStatus } from "@/lib/admin-types";
import { normalizeRerankKey } from "@/lib/reranker";

// 자동은 저장값 `""`이다. Select는 빈 문자열을 「선택 없음」으로 다루므로 센티널로 바꿔 쓴다
const AUTO = "__auto__";
const AUTO_LABEL = "자동(기본 리랭커, 없으면 로컬)";

/**
 * RAG 리랭커 드롭다운(코어 v0.15.0 #209) — 선택지는 `GET /admin/rag/reranker`가 준다.
 *
 * 선택 표시는 정규화 키로 맞추지만 **사용자가 고르지 않으면 원값을 그대로 둔다**(계획 D-4).
 * 무접두 옛 값을 `fastembed:` 키로 다시 쓰면 구 코어(v0.13.0)로 롤백했을 때 읽히지 않는다.
 * 목록을 **정상으로 받기 전**(응답 대기·실패 — 구 API는 404)에는 선택을 잠그고 원값을 유지한다.
 * 대기 중에 고르게 두면 구 API의 404가 오기 전에 자동(`""`)·`gateway:` 값이 폼에 들어가 구 코어가
 * 읽지 못하는 값이 저장된다(리뷰 P2-01).
 *
 * 성공 여부는 **이 화면이 뜬 뒤의 응답**으로만 판단한다. 롤백 전에 받은 목록이 캐시에 남아 있어도
 * 근거가 되지 않는다(리뷰 P2-09). `supported`는 현재 서버의 설정 응답이 새 계약(v0.15.0+)인지다.
 * 마운트 뒤 한 번 받은 다음의 배경 재조회(저장 뒤 invalidate 등) 중에는 계속 고를 수 있다.
 */
export function RerankerSelect({
  value,
  onChange,
  status,
  supported,
}: {
  value: string;
  onChange: (value: string) => void;
  status: UseQueryResult<RerankerStatus>;
  supported: boolean;
}) {
  // 이 서버가 리랭커 선택지를 아는(코어 v0.15.0+) 것이 확인된 뒤에만 바꿀 수 있다
  const ready = supported && status.isSuccess && status.isFetchedAfterMount;
  const normalized = normalizeRerankKey(value);
  const selected = normalized === "" ? AUTO : normalized;
  const options = status.data?.options ?? [];
  // 서버는 선택지에 없는 저장값도 돌려준다. 응답 전·실패 시에만 원값 항목을 직접 둔다
  const missingCurrent = selected !== AUTO && !options.some((o) => o.key === selected);
  const labelOf = (key: string) =>
    key === AUTO ? AUTO_LABEL : (options.find((o) => o.key === key)?.display_name ?? value);

  return (
    <>
      <Label htmlFor="rerank-model">리랭커</Label>
      <Select
        value={selected}
        disabled={!ready}
        onValueChange={(next) => {
          // 같은 항목을 다시 골라도 원값을 바꾸지 않는다(PUT 재기록 금지)
          if (!ready || !next || next === selected) return;
          onChange(next === AUTO ? "" : next);
        }}
      >
        <SelectTrigger id="rerank-model" className="w-full">
          <SelectValue>{(key: string) => labelOf(key)}</SelectValue>
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={AUTO}>{AUTO_LABEL}</SelectItem>
          {options.map((o) => (
            <SelectItem key={o.key} value={o.key} disabled={!o.available}>
              {o.display_name}
              {o.available ? "" : " — 사용 불가"}
            </SelectItem>
          ))}
          {missingCurrent && <SelectItem value={selected}>{value}</SelectItem>}
        </SelectContent>
      </Select>
      {!ready && !status.isError && (
        <p role="status" className="text-xs text-muted-foreground">
          리랭커 목록을 불러오는 중 — 확인될 때까지 바꿀 수 없습니다.
        </p>
      )}
      {status.isError && (
        <p role="status" className="text-xs text-muted-foreground">
          리랭커 목록을 불러올 수 없음 — 현재 값({value || "자동"})을 그대로 저장합니다.
        </p>
      )}
      {status.data?.error && (
        <p role="alert" className="text-xs text-destructive">
          현재 설정으로는 리랭크할 수 없습니다: {status.data.error}
        </p>
      )}
      {status.data?.active_display_name && (
        <p className="text-xs text-muted-foreground">
          사용 중: {status.data.active_display_name}
        </p>
      )}
    </>
  );
}
