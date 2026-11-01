"use client";

import { MessageSquare } from "lucide-react";
import Link from "next/link";
import { useId } from "react";
import { useRecentChats } from "@/hooks/use-chat";
import { formatRelative } from "@/lib/format";

/**
 * 홈 「최근 대화 이어가기」(ui-ux-improvements ⑥) — 마지막 대화로 바로 돌아간다.
 * 대화가 없거나 불러오지 못하면 아무것도 그리지 않는다(노트북 목록이 주인공이다).
 */
export function RecentChats() {
  const recent = useRecentChats(3);
  const headingId = useId();
  const items = recent.data ?? [];
  if (items.length === 0) return null;

  return (
    <section aria-labelledby={headingId} className="mb-6">
      <h2 id={headingId} className="mb-2 text-sm font-semibold text-muted-foreground">
        최근 대화 이어가기
      </h2>
      <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item) => (
          <li key={item.session_id}>
            <Link
              href={`/notebooks/${item.notebook_id}?s=${item.session_id}`}
              className="flex h-full flex-col gap-1 rounded-xl border bg-card p-3 text-sm shadow-xs transition-colors hover:border-primary/40 hover:bg-accent/40 focus-visible:ring-3 focus-visible:ring-ring/50 focus-visible:outline-none"
            >
              <span className="flex min-w-0 items-center gap-1.5 font-medium">
                <MessageSquare className="size-3.5 shrink-0 text-primary" aria-hidden />
                <span className="truncate">{item.title}</span>
              </span>
              <span className="flex min-w-0 items-center gap-1 text-xs text-muted-foreground">
                <span className="truncate">
                  {item.notebook_emoji} {item.notebook_title}
                </span>
                <span aria-hidden>·</span>
                <span className="shrink-0">{formatRelative(item.updated_at)}</span>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
