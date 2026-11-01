"use client";

import { useVersions } from "@/hooks/use-versions";

/**
 * 브랜드 「biznotes」 아래의 작은 버전 줄 — `v0.6.0`.
 * 읽는 동안에도 한 줄 높이를 잡아 두어 제목이 뒤늦게 밀려 올라가지 않는다. 조회가 실패하면 줄 자체를 그리지 않는다.
 * 좁은 사이드바(220px)에서는 말줄임하고, 전체 문구는 `title`로 보인다.
 */
export function AppVersions() {
  const { data, isError } = useVersions();
  if (isError) return null;
  if (!data) return <span aria-hidden className="block h-4" />;
  const text = `v${data.app_version}`;
  return (
    <span
      data-testid="app-versions"
      title={`biznotes ${text}`}
      className="block truncate text-[11px] leading-4 text-muted-foreground"
    >
      {text}
    </span>
  );
}
