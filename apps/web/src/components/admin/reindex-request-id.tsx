"use client";

import { Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { notifyError, notifySuccess } from "@/lib/notify";

/** 화면에 보이는 요청 번호 길이 — UUID 앞 8자(§6.6.2 RAG 행: 「run ‹UUID›」 → 「요청 번호 앞 8자」 + 복사). */
export const REQUEST_ID_VISIBLE_LENGTH = 8;

/** 재인덱싱 요청(run) 식별자의 앞 8자. */
export function shortRequestId(runId: string): string {
  return runId.slice(0, REQUEST_ID_VISIBLE_LENGTH);
}

async function copyRequestId(runId: string) {
  try {
    if (!navigator.clipboard?.writeText) throw new Error("이 브라우저는 클립보드 복사를 지원하지 않습니다");
    await navigator.clipboard.writeText(runId);
    notifySuccess("요청 번호를 복사했습니다");
  } catch (error) {
    notifyError("요청 번호를 복사", error);
  }
}

/**
 * 재인덱싱 요청 번호 — 「요청 번호 abcd1234」(앞 8자, 전체 값은 `title`) + 복사 버튼(전체 UUID를 복사).
 * 원시 UUID를 문장 안에 그대로 늘어놓지 않는다(P-19 ④).
 */
export function ReindexRequestId({ runId, className }: { runId: string; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1 ${className ?? ""}`}>
      <span>
        요청 번호{" "}
        <span className="font-mono" title={runId}>
          {shortRequestId(runId)}
        </span>
      </span>
      <Button
        type="button"
        variant="ghost"
        size="icon-xs"
        className="coarse:size-11"
        aria-label="요청 번호 복사"
        title="전체 요청 번호 복사"
        onClick={() => void copyRequestId(runId)}
      >
        <Copy aria-hidden />
      </Button>
    </span>
  );
}
