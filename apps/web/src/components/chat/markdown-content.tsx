"use client";

import type { ComponentProps, ReactElement, ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CitationChip } from "@/components/chat/citation-text";
import { MermaidDiagram } from "@/components/chat/mermaid-diagram";
import type { Citation } from "@/lib/chat-types";
import { cn } from "@/lib/utils";

// hast 노드 최소 구조 타입 (hast 패키지 직접 의존 회피)
type MdNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: MdNode[];
};

const CITATION_TAG = "citation-ref";
// 코드/링크 내부의 [n]은 인용 마커로 취급하지 않음
const SKIP_TAGS = new Set(["code", "pre", "a"]);

/** 텍스트 노드의 [n] 인용 마커를 <citation-ref data-n> 요소로 치환하는 rehype 플러그인 */
function rehypeCitations() {
  return (tree: MdNode) => {
    const walk = (parent: MdNode) => {
      if (!parent.children || (parent.tagName && SKIP_TAGS.has(parent.tagName))) return;
      parent.children = parent.children.flatMap((child): MdNode[] => {
        if (child.type === "text" && child.value && /\[\d+\]/.test(child.value)) {
          return child.value
            .split(/(\[\d+\])/g)
            .filter((part) => part !== "")
            .map((part): MdNode => {
              const m = part.match(/^\[(\d+)\]$/);
              return m
                ? {
                    type: "element",
                    tagName: CITATION_TAG,
                    properties: { dataN: m[1] },
                    children: [],
                  }
                : { type: "text", value: part };
            });
        }
        walk(child);
        return [child];
      });
    };
    walk(tree);
  };
}

/** 마크다운 뷰어 — GFM + mermaid + (옵션) [n] 인용 칩. 채팅 답변·아티팩트 보고서 공용 */
export function MarkdownContent({
  content,
  citations = [],
  className,
}: {
  content: string;
  citations?: Citation[];
  /** 추가 클래스 — 소스 미리보기처럼 좁은 곳에서 제목 크기를 줄일 때 쓴다. */
  className?: string;
}) {
  const byN = new Map(citations.map((c) => [c.n, c]));

  const components = {
    [CITATION_TAG]: ({ node }: { node?: MdNode }) => {
      const n = Number(node?.properties?.dataN);
      const citation = byN.get(n);
      // 매칭되는 인용이 없으면 원문 그대로 노출
      if (!citation) return <span>[{Number.isNaN(n) ? "?" : n}]</span>;
      return <CitationChip n={n} citation={citation} />;
    },
    pre: (props: ComponentProps<"pre">) => {
      const child = props.children as ReactElement<{ className?: string; children?: ReactNode }>;
      const className = child?.props?.className;
      if (typeof className === "string" && className.includes("language-mermaid")) {
        return <MermaidDiagram chart={String(child.props.children ?? "")} />;
      }
      return <pre className={props.className}>{props.children}</pre>;
    },
  } as Components;

  return (
    <div
      className={cn(
        "prose prose-sm dark:prose-invert max-w-none break-words prose-headings:font-semibold prose-pre:bg-muted prose-pre:text-foreground prose-code:before:content-none prose-code:after:content-none [&>*:first-child]:mt-0 [&>*:last-child]:mb-0",
        className,
      )}
      data-testid="markdown-content"
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeCitations]}
        components={components}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
