import Link from "next/link";
import { Fragment } from "react";

export interface BreadcrumbItem {
  label: string;
  /** 없으면 현재 위치(마지막 항목)로 보고 `aria-current="page"`를 붙인다. */
  href?: string;
}

/**
 * 콘텐츠 페이지 공통 헤더(§6.5) — 브레드크럼(선택) · 제목(단일 `h1`) · 설명(선택) · 액션(오른쪽).
 * 액션은 「보조 버튼들 → 주 버튼 1개」 순서로 넘긴다. 640 미만에서는 액션이 제목 아래로 내려가고 마지막(주) 버튼이
 * 전체 폭이 된다. 기존 `children`은 `actions`의 별칭으로 유지한다(하위 호환).
 */
export function PageHeader({
  title,
  description,
  breadcrumb,
  actions,
  children,
}: {
  title: string;
  description?: React.ReactNode;
  breadcrumb?: BreadcrumbItem[];
  actions?: React.ReactNode;
  /** `actions`의 별칭(하위 호환). 둘 다 주면 `actions`를 먼저, `children`을 뒤에 둔다. */
  children?: React.ReactNode;
}) {
  const hasActions = Boolean(actions) || Boolean(children);
  return (
    <div className="mb-6 flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-start sm:justify-between">
      <div className="min-w-0">
        {breadcrumb && breadcrumb.length > 0 && (
          <nav aria-label="위치" className="mb-1">
            <ol className="flex flex-wrap items-center gap-1 text-sm text-muted-foreground">
              {breadcrumb.map((item, i) => (
                <Fragment key={`${item.label}-${i}`}>
                  {i > 0 && (
                    <li aria-hidden className="select-none">
                      /
                    </li>
                  )}
                  <li className="min-w-0">
                    {item.href ? (
                      <Link
                        href={item.href}
                        className="rounded-sm underline-offset-4 hover:text-foreground hover:underline focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none coarse:inline-flex coarse:min-h-11 coarse:items-center"
                      >
                        {item.label}
                      </Link>
                    ) : (
                      <span aria-current="page" className="text-foreground">
                        {item.label}
                      </span>
                    )}
                  </li>
                </Fragment>
              ))}
            </ol>
          </nav>
        )}
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      </div>
      {hasActions && (
        <div
          data-slot="page-header-actions"
          className="flex flex-wrap items-center gap-2 max-sm:[&>:last-child]:w-full"
        >
          {actions}
          {children}
        </div>
      )}
    </div>
  );
}
