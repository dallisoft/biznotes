"use client"

import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * 좁은 화면(< 768)에서 표 첫 열을 고정한다 — 가로로 스크롤해도 어느 행인지 남는다(plan §6.7 표 규칙, P5 리뷰 P5-04).
 * 행 hover 색과 섞이지 않도록 불투명 배경을 깔고, 오른쪽에 1px 경계를 그린다.
 */
export const STICKY_FIRST_COLUMN_CLASS =
  "max-md:[&_tr>*:first-child]:sticky max-md:[&_tr>*:first-child]:left-0 max-md:[&_tr>*:first-child]:z-[1] max-md:[&_tr>*:first-child]:bg-background max-md:[&_tr>*:first-child]:shadow-[1px_0_0_var(--border)]"

/** 가로로 더 스크롤할 내용이 오른쪽에 남았는가(1px 오차 허용). */
export function hasMoreToRight(el: Pick<HTMLElement, "scrollWidth" | "clientWidth" | "scrollLeft">): boolean {
  return el.scrollWidth - el.clientWidth - el.scrollLeft > 1
}

/** 단서 그라데이션이 녹아드는 바탕 — 표가 카드 안에 있으면 `card`. */
export type TableScrollSurface = "background" | "card"

const CUE_SURFACE: Record<TableScrollSurface, string> = {
  background: "from-background",
  card: "from-card",
}

/**
 * 좁은 화면 표 규칙(plan §6.7 「표(< 768)」)의 단일 구현 — opt-in. 가로 스크롤은 안쪽 공용 표 컨테이너
 * (`[data-slot=table-container]`, `overflow-x-auto`)가 그대로 맡고, 이 틀은 ① 오른쪽에 더 볼 열이 남은 동안 가장자리
 * 그라데이션 단서(장식 — `aria-hidden`, md 이상 숨김)와 ② (선택) 첫 열 고정을 입힌다. 스크롤·창 크기·표 크기
 * 변화마다 다시 잰다.
 *
 * 쓰는 법: 표 하나는 `<Table scrollCue>`(노트북 표 P3·실행 기록 P4), 관리 표는 `AdminTableFrame`(= 이 틀 + 첫 열 고정,
 * P5). P3 `Table scrollCue`·P5 `AdminTableFrame`·P4 실행 기록의 지역 단서를 P6에서 이것 하나로 합쳤다.
 */
export function TableScrollFrame({
  stickyFirstColumn = false,
  surface = "background",
  className,
  children,
  ...props
}: React.ComponentProps<"div"> & {
  /** 좁은 화면 첫 열 고정(`STICKY_FIRST_COLUMN_CLASS`) — 관리 표 */
  stickyFirstColumn?: boolean
  surface?: TableScrollSurface
}) {
  const frameRef = React.useRef<HTMLDivElement>(null)
  const [moreRight, setMoreRight] = React.useState(false)

  React.useEffect(() => {
    const scroller = frameRef.current?.querySelector<HTMLElement>('[data-slot="table-container"]')
    if (!scroller) return
    const update = () => setMoreRight(hasMoreToRight(scroller))
    update()
    scroller.addEventListener("scroll", update, { passive: true })
    window.addEventListener("resize", update)
    // 행 수·열 폭이 바뀌어도(페이지 이동·데이터 갱신) 다시 잰다
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(update) : null
    observer?.observe(scroller)
    const table = scroller.querySelector("table")
    if (table) observer?.observe(table)
    return () => {
      scroller.removeEventListener("scroll", update)
      window.removeEventListener("resize", update)
      observer?.disconnect()
    }
  }, [])

  return (
    <div
      ref={frameRef}
      data-slot="table-scroll"
      data-scroll-cue={moreRight ? "visible" : "hidden"}
      className={cn("relative w-full min-w-0", stickyFirstColumn && STICKY_FIRST_COLUMN_CLASS, className)}
      {...props}
    >
      {children}
      <div
        aria-hidden
        data-slot="table-scroll-cue"
        data-visible={moreRight ? "true" : "false"}
        className={cn(
          "pointer-events-none absolute inset-y-0 right-0 z-20 w-8 bg-linear-to-l to-transparent transition-opacity md:hidden",
          CUE_SURFACE[surface],
          moreRight ? "opacity-100" : "opacity-0"
        )}
      />
    </div>
  )
}

function Table({
  className,
  scrollCue = false,
  scrollSurface = "background",
  ...props
}: React.ComponentProps<"table"> & {
  /**
   * (선택, 기본 false) 좁은 화면(< 768) 가로 스크롤 단서 — 오른쪽에 더 볼 열이 남아 있는 동안 가장자리에
   * 그라데이션을 보인다(plan §6.7 「표(< 768)」, P3 리뷰 P3-03 — `TableScrollFrame`). 주지 않으면 기존 구조·동작 그대로다.
   */
  scrollCue?: boolean
  /** `scrollCue`의 그라데이션 바탕(기본 페이지 배경, 카드 안 표는 `card`) */
  scrollSurface?: TableScrollSurface
}) {
  const container = (
    <div
      data-slot="table-container"
      className="relative w-full overflow-x-auto"
    >
      <table
        data-slot="table"
        className={cn("w-full caption-bottom text-sm", className)}
        {...props}
      />
    </div>
  )
  if (scrollCue) return <TableScrollFrame surface={scrollSurface}>{container}</TableScrollFrame>
  return container
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("[&_tr]:border-b", className)}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t bg-muted/50 font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "border-b transition-colors hover:bg-muted/50 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted",
        className
      )}
      {...props}
    />
  )
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "h-10 px-2 text-left align-middle font-medium whitespace-nowrap text-foreground [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "p-2 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}
