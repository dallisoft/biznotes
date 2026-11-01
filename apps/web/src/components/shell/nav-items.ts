import {
  BadgeCheck,
  Blocks,
  Bot,
  Cpu,
  Database,
  Gauge,
  LayoutDashboard,
  LayoutGrid,
  ScrollText,
  Server,
  Users,
  Workflow,
  type LucideIcon,
} from "lucide-react";

export type NavItem = {
  href: string;
  label: string;
  Icon: LucideIcon;
  /** true면 경로가 href와 정확히 같을 때만 활성(하위 경로의 묶음 항목 — 노트북 홈·관리 개요) */
  exact?: boolean;
};

/**
 * 주 메뉴(plan §6.2.2 ③) — 경로와 무관하게 항목·순서·라벨이 같다(DP-1, AC-2.1).
 */
export const MAIN_NAV: readonly NavItem[] = [
  { href: "/", label: "노트북", Icon: LayoutGrid, exact: true },
  { href: "/skills", label: "스킬", Icon: Blocks },
  { href: "/workflows", label: "워크플로우", Icon: Workflow },
];

/**
 * 「관리」 그룹 하위 항목(plan §6.2.2 ④·§6.2.7 사이트맵) — 관리자에게만 렌더한다(AC-2.2).
 * 관리 페이지 제목은 P5에서 맞추고, P2는 내비 라벨만 바꾼다(§9 Phase 2 작업 6).
 */
export const ADMIN_NAV: readonly NavItem[] = [
  { href: "/admin", label: "개요", Icon: LayoutDashboard, exact: true },
  { href: "/admin/users", label: "사용자·그룹", Icon: Users },
  { href: "/admin/llm", label: "LLM 게이트웨이", Icon: Cpu },
  { href: "/admin/rag", label: "RAG", Icon: Database },
  { href: "/admin/agents", label: "에이전트", Icon: Bot },
  { href: "/admin/skills", label: "스킬 공개", Icon: BadgeCheck },
  { href: "/admin/mcp", label: "MCP 서버", Icon: Server },
  { href: "/admin/limits", label: "사용 제한", Icon: Gauge },
  { href: "/admin/audit", label: "감사 로그", Icon: ScrollText },
];

/** 경로가 항목에 속하는지 — 접두어는 경계(`/`)까지 본다(`/skills`가 `/skillsX`를 활성으로 만들지 않게). */
export function isNavActive(pathname: string, item: Pick<NavItem, "href" | "exact">): boolean {
  if (pathname === item.href) return true;
  if (item.exact) return false;
  return pathname.startsWith(`${item.href}/`);
}

/** `/admin` 또는 그 하위 경로 — 「관리」 그룹의 기본 펼침 판정에 쓴다. */
export function isAdminPath(pathname: string): boolean {
  return pathname === "/admin" || pathname.startsWith("/admin/");
}
