"use client";

import { Monitor, Moon, Palette, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";
import {
  COLOR_THEMES,
  useColorTheme,
  type ColorTheme,
} from "@/components/theme/color-theme-provider";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const MODE_OPTIONS = [
  { value: "light", label: "라이트", icon: Sun },
  { value: "dark", label: "다크", icon: Moon },
  { value: "system", label: "시스템", icon: Monitor },
] as const;

const COLOR_LABEL: Record<ColorTheme, string> = {
  indigo: "인디고",
  teal: "틸",
  neutral: "뉴트럴",
};

// 프리셋 미리보기 점 색상 (토큰과 무관한 고정 표시용)
const COLOR_SWATCH: Record<ColorTheme, string> = {
  indigo: "oklch(0.51 0.23 277)",
  teal: "oklch(0.55 0.12 192)",
  neutral: "oklch(0.4 0 0)",
};

export function ThemeSwitcher() {
  const { theme, setTheme, resolvedTheme } = useTheme();
  const { colorTheme, setColorTheme } = useColorTheme();
  const [mounted, setMounted] = useState(false);

  // next-themes는 클라이언트 마운트 전 테마를 알 수 없음 — hydration mismatch 방지
  useEffect(() => setMounted(true), []);

  const ModeIcon = !mounted ? Palette : resolvedTheme === "dark" ? Moon : Sun;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        render={
          <Button
            variant="ghost"
            size="icon"
            aria-label="테마 설정"
            data-testid="theme-switcher"
            // 셸 사이드바·드로어 바닥 전용 — 사이드바 면 위 포커스 표시는 sidebar-ring(P2 리뷰 P2-02, nav-link
            // SIDEBAR_BUTTON_FOCUS_CLASS와 같은 값)
            className="coarse:size-11 focus-visible:border-sidebar-ring focus-visible:ring-sidebar-ring/50"
          >
            <ModeIcon className="size-4" />
          </Button>
        }
      />
      <DropdownMenuContent align="end" className="w-44">
        {/* GroupLabel은 Group/RadioGroup 내부에서만 허용 (Base UI #31) */}
        <DropdownMenuRadioGroup value={theme} onValueChange={(v) => setTheme(v as string)}>
          <DropdownMenuLabel>화면 모드</DropdownMenuLabel>
          {MODE_OPTIONS.map((opt) => (
            <DropdownMenuRadioItem key={opt.value} value={opt.value}>
              <opt.icon className="size-4 text-muted-foreground" />
              {opt.label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuRadioGroup
          value={colorTheme}
          onValueChange={(v) => setColorTheme(v as ColorTheme)}
        >
          <DropdownMenuLabel>브랜드 컬러</DropdownMenuLabel>
          {COLOR_THEMES.map((c) => (
            <DropdownMenuRadioItem key={c} value={c}>
              <span
                className="size-3 shrink-0 rounded-full"
                style={{ backgroundColor: COLOR_SWATCH[c] }}
              />
              {COLOR_LABEL[c]}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
