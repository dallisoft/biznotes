"use client";

import { useTheme } from "next-themes";
import { useEffect, useId, useState } from "react";

/**
 * mermaid 코드 블록을 SVG 다이어그램으로 렌더링.
 * 스트리밍 중 불완전한 차트는 파싱에 실패하므로 코드 폴백을 유지하다가
 * 완성되는 시점에 다이어그램으로 전환된다.
 */
export function MermaidDiagram({ chart }: { chart: string }) {
  // mermaid.render는 DOM id를 요구 — useId의 특수문자 제거
  const renderId = useId().replace(/[^a-zA-Z0-9]/g, "");
  const { resolvedTheme } = useTheme();
  const [svg, setSvg] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mermaid = (await import("mermaid")).default;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: resolvedTheme === "dark" ? "dark" : "default",
        });
        if (!(await mermaid.parse(chart, { suppressErrors: true }))) return;
        const result = await mermaid.render(`mermaid-${renderId}`, chart);
        if (!cancelled) setSvg(result.svg);
      } catch {
        // 잘못된 차트는 코드 폴백 유지
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [chart, renderId, resolvedTheme]);

  if (!svg) {
    return (
      <pre className="overflow-x-auto rounded-md bg-muted p-3 text-xs text-muted-foreground">
        {chart}
      </pre>
    );
  }
  return (
    <div
      className="my-3 flex justify-center overflow-x-auto rounded-md border bg-card p-3 [&_svg]:h-auto [&_svg]:max-w-full"
      data-testid="mermaid-diagram"
      // securityLevel: "strict"로 mermaid가 내부 sanitize한 SVG만 주입
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
