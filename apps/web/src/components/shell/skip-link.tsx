"use client";

/**
 * 첫 탭 정지점 「본문으로 건너뛰기」(plan §6.8-3, AC-2.5). 셸 DOM의 맨 앞에 둔다.
 * Enter·클릭 시 대상(`main#main-content`, `tabIndex=-1`)으로 포커스를 옮긴다 — 해시를 URL에 남기지 않도록
 * 기본 이동을 막고 직접 포커스한다(대상이 없을 때만 브라우저 기본 해시 이동).
 */
export function SkipLink({ targetId = "main-content" }: { targetId?: string }) {
  return (
    <a
      href={`#${targetId}`}
      onClick={(event) => {
        const target = document.getElementById(targetId);
        if (!target) return;
        event.preventDefault();
        target.focus();
      }}
      className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-[100] focus:rounded-lg focus:bg-background focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-foreground focus:shadow-md focus:outline-none focus:ring-2 focus:ring-ring"
    >
      본문으로 건너뛰기
    </a>
  );
}
