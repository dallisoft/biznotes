"use client";

import { ArrowLeft, ExternalLink, ShieldAlert, Sparkles, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { MarkdownContent } from "@/components/chat/markdown-content";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSourceDetail, useSourceGuide } from "@/hooks/use-sources";
import { formatTokens } from "@/lib/format";
import { STATUS_LABEL, sourceTypeLucide, type Source } from "@/lib/source-types";
import { locateQuote } from "@/lib/quote-locate";
import { describeSourceWarnings } from "@/lib/source-warnings";

/**
 * 소스 상세 — 목록 아래에 펼치지 않고 소스 패널(우측 패널, 모바일은 소스 화면) 전체를 차지한다(메모 보기·에이전트
 * 실행 상세와 같은 방식). 상단에 「← 목록」·제목·부제(상태·청크·토큰)를 두고, 본문(문서 제목·원본 링크·오류·경고·
 * 소스 가이드·마크다운)은 패널 높이에 맞춰 세로 스크롤한다. 열리면 제목으로 포커스를 옮긴다(돌아갈 때의 포커스
 * 복귀는 SourcePanel이 맡는다). 머리글은 목록의 소스로 바로 그리고, 상세를 받으면 본문과 같은 상세 값으로 바꾼다.
 */
export function SourceDetailInline({
  source,
  onBack,
  highlightQuote = null,
}: {
  source: Source;
  onBack: () => void;
  /** 인용으로 열었을 때의 발췌 — 원문에서 그 구간을 표시하고 스크롤한다(ui-ux-improvements ②). */
  highlightQuote?: string | null;
}) {
  const detail = useSourceDetail(source.id);
  const ready = detail.data?.status === "ready";
  // ready 소스에 한해 가이드(요약+키워드) 온디맨드 생성
  const guide = useSourceGuide(ready ? source.id : null);
  const headingRef = useRef<HTMLHeadingElement>(null);

  // 화면이 바뀐 것을 알린다 — 목록 대신 상세가 열리면 제목(머리글)으로 포커스를 옮긴다.
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  const meta = detail.data ?? source;
  const TypeIcon = sourceTypeLucide(meta.type);

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="source-detail-view">
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2">
        <Button
          variant="ghost"
          size="sm"
          onClick={onBack}
          className="shrink-0 text-muted-foreground"
          data-testid="source-detail-back"
        >
          <ArrowLeft className="size-4" /> 목록
        </Button>
        <div className="ml-1 min-w-0">
          <h2
            ref={headingRef}
            tabIndex={-1}
            className="flex min-w-0 items-start gap-1.5 text-sm font-semibold outline-none"
          >
            <TypeIcon className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
            <span className="line-clamp-2 min-w-0 break-words" title={meta.title}>
              {meta.title}
            </span>
          </h2>
          <p className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
            <span>
              {STATUS_LABEL[meta.status]} · 청크 {meta.chunk_count}개 · 토큰{" "}
              {formatTokens(meta.token_count)}
            </span>
          </p>
        </div>
      </div>

      <div
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3"
        data-testid="source-detail-body"
      >
        <SourceDetailBody detail={detail} guide={guide} ready={ready} highlightQuote={highlightQuote} />
      </div>
    </div>
  );
}

function SourceDetailBody({
  detail,
  guide,
  ready,
  highlightQuote,
}: {
  detail: ReturnType<typeof useSourceDetail>;
  guide: ReturnType<typeof useSourceGuide>;
  ready: boolean;
  highlightQuote: string | null;
}) {
  const markdownRef = useRef<HTMLDivElement>(null);
  const preview = detail.data?.parsed_preview ?? null;
  const [quoteMissing, setQuoteMissing] = useState(false);

  // 인용 구간 표시 — 원문 블록에 data-cited를 달고 첫 블록을 가운데로 스크롤한다. 못 찾으면 알린다.
  useEffect(() => {
    const root = markdownRef.current;
    if (!root) return;
    root.querySelectorAll("[data-cited]").forEach((el) => el.removeAttribute("data-cited"));
    if (!highlightQuote) {
      setQuoteMissing(false);
      return;
    }
    const blocks = locateQuote(root, highlightQuote);
    setQuoteMissing(blocks === null);
    if (!blocks) return;
    blocks.forEach((el) => el.setAttribute("data-cited", "true"));
    blocks[0].scrollIntoView({ block: "center", behavior: "smooth" });
  }, [highlightQuote, preview]);

  if (detail.isPending) {
    return (
      <div className="flex flex-col gap-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-16" />
      </div>
    );
  }

  if (detail.isError) {
    return <p className="text-sm text-destructive">불러오지 못했습니다: {detail.error.message}</p>;
  }

  const d = detail.data;
  // 문서가 스스로 가진 제목(HWP info·PDF 메타·웹 <title>) — 목록 제목(title = 파일명·중복 키)은
  // 그대로 두고 상세 보조 줄에만 보인다(v7 D-16). 없거나 제목과 같으면 생략한다.
  const documentTitle =
    d.document_title && d.document_title !== d.title ? d.document_title : null;
  // null = 기록 없음, [] = 경고 없음 — 둘 다 영역을 그리지 않는다
  const warnings = describeSourceWarnings(d.warnings);

  return (
    <>
      {/* 문서 제목 + 원본링크 — 제목·상태는 머리글에 있다 */}
      {(documentTitle || d.origin_url) && (
        <div className="flex flex-col gap-1">
          {documentTitle && (
            // 외부가 정한 문자열 — 텍스트로만 렌더하고 2줄로 자른다(점자 빈칸·결합 문자 높이 제한)
            <p
              className="line-clamp-2 text-xs break-words text-muted-foreground"
              title={documentTitle}
              data-testid="source-document-title"
            >
              <span className="font-medium">문서 제목 </span>
              {documentTitle}
            </p>
          )}
          {d.origin_url && (
            <a
              href={d.origin_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex w-fit items-center gap-1 text-xs text-primary hover:underline"
            >
              <ExternalLink className="size-3" />
              원본 링크
            </a>
          )}
        </div>
      )}

      {/* 실패만 오류 스타일. 코어 v0.16.0(#230)부터 `ready`의 error_message는 실패 전용이고 재인덱싱
          안내는 `warnings`(reindex_*)로 온다. 실패가 아닌 error_message는 둘뿐이다 — 신규 소스의
          「임베딩 지연」(pending, v0.12.0 #194)과 v0.16.0 전에 쓴 `ready` 행의 옛 재인덱싱 문구(다음 전체
          파싱까지 남는다 — 전환기라 계속 muted로 보인다) */}
      {d.error_message && (
        <p
          data-testid="source-error-message"
          className={
            d.status === "failed"
              ? "rounded-md bg-destructive/10 p-2 text-xs text-destructive"
              : "rounded-md bg-muted p-2 text-xs text-muted-foreground"
          }
        >
          {d.error_message}
        </p>
      )}

      {/* 인제스트 경고 — 코드별 한국어 문구, 보안 신호는 주의 스타일 */}
      {warnings.length > 0 && (
        <ul className="flex flex-col gap-1" data-testid="source-warnings" aria-label="인제스트 경고">
          {warnings.map((w, i) => (
            <li
              key={`${w.code}-${i}`}
              data-testid="source-warning"
              data-code={w.code}
              data-security={w.security ? "true" : "false"}
              className={`flex items-start gap-1.5 rounded-md p-2 text-xs ${
                w.security
                  ? "bg-warning/10 text-warning-text"
                  : "bg-muted/60 text-muted-foreground"
              }`}
            >
              {w.security ? (
                <ShieldAlert className="mt-px size-3.5 shrink-0" aria-label="보안 신호" />
              ) : (
                <TriangleAlert className="mt-px size-3.5 shrink-0" aria-hidden />
              )}
              <span className="min-w-0 break-words">{w.message}</span>
            </li>
          ))}
        </ul>
      )}

      {/* 요약 + 키워드 (소스 가이드) */}
      {ready && (
        <div className="rounded-lg border bg-muted/40 p-2.5">
          <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
            <Sparkles className="size-3.5" />
            소스 가이드
          </div>
          {guide.isPending ? (
            <div className="flex flex-col gap-1.5">
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-4/5" />
            </div>
          ) : guide.isError ? (
            <p className="text-xs text-muted-foreground">가이드를 생성하지 못했습니다.</p>
          ) : (
            <>
              {guide.data.summary ? (
                <p className="text-xs leading-relaxed text-foreground/90">
                  {guide.data.summary}
                </p>
              ) : (
                <p className="text-xs text-muted-foreground">요약이 없습니다.</p>
              )}
              {(guide.data.keywords ?? []).length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {(guide.data.keywords ?? []).map((kw) => (
                    <Badge key={kw} variant="secondary" className="font-normal">
                      {kw}
                    </Badge>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* 마크다운 (파싱 결과) */}
      <div>
        <div className="mb-1.5 text-xs font-medium text-muted-foreground">마크다운</div>
        {quoteMissing && (
          <p role="status" className="mb-1.5 rounded-md bg-muted p-2 text-xs text-muted-foreground">
            인용 위치를 찾지 못했습니다 — 미리보기(앞 20,000자)에 없는 부분일 수 있습니다.
          </p>
        )}
        {d.parsed_preview ? (
          <div
            ref={markdownRef}
            className="rounded-md border p-2.5 [&_[data-cited]]:-mx-1 [&_[data-cited]]:rounded-sm [&_[data-cited]]:bg-warning/15 [&_[data-cited]]:px-1 [&_[data-cited]]:ring-1 [&_[data-cited]]:ring-warning/40"
          >
            <MarkdownContent
              content={d.parsed_preview}
              className="prose-h1:text-base prose-h2:text-sm prose-h3:text-sm prose-h4:text-sm"
            />
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">(파싱 결과 없음)</p>
        )}
      </div>
    </>
  );
}
