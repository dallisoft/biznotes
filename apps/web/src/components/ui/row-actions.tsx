"use client";

import { MoreHorizontal } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useNarrowViewport } from "@/hooks/use-narrow-viewport";

interface RowActionBase {
  label: string;
  icon?: React.ReactNode;
  disabled?: boolean;
  /** 비활성일 때 항목 아래에 보일 사유(선택) — 예: 「자기 계정은 비활성화할 수 없습니다」 */
  disabledReason?: string;
}

/** 행 동작 확인 문구 — 제목은 질문형, 확인 버튼은 동작 동사(§6.3.3). */
export interface RowActionConfirm {
  title: string;
  description?: React.ReactNode;
  actionLabel: string;
  /**
   * (선택) true면 확인 동작(위험 항목 `onConfirm`)이 돌려준 Promise가 끝날 때까지 확인 다이얼로그를 연 채 기다리고,
   * 실패(거부)하면 열린 채 다시 시도할 수 있다 — ConfirmDialog `awaitConfirm`(P3 리뷰 P3-01). 기본은 기존처럼 확인 즉시 닫는다.
   */
  awaitConfirm?: boolean;
  /** (선택) `awaitConfirm` 대기 중 확인 버튼 문구 — 예: 「삭제하는 중…」 */
  pendingLabel?: string;
}

/**
 * 일반 동작 — 선택 즉시 실행한다. `confirm`을 주면 위험 동작이 아니어도 확인을 받은 뒤 실행한다
 * (예: 사용자 행의 「비밀번호 초기화…」 — §6.6.2, D-10). 확인 버튼은 빨강이 아니다(§6.5 「빨강은 위험 동작만」).
 * `onSelect`가 다이얼로그를 열어도 따로 표시할 것이 없다 — 다이얼로그가 초기 포커스를 가져가고, 닫히면 ⋯로 돌아온다
 * (아래 RowActions 「메뉴가 닫힐 때의 포커스」).
 */
export interface RowAction extends RowActionBase {
  destructive?: false;
  onSelect: () => void;
  confirm?: RowActionConfirm;
}

/** 위험 동작 — 선택하면 ConfirmDialog를 열고, 확인해야 실행한다(DP-5). */
export interface RowDangerAction extends RowActionBase {
  destructive: true;
  confirm: RowActionConfirm;
  /** `confirm.awaitConfirm`이면 Promise를 돌려준다(이행 = 닫음, 거부 = 다이얼로그 유지). */
  onConfirm: () => void | Promise<unknown>;
}

export type RowActionItem = RowAction | RowDangerAction;

export interface RowActionsProps {
  /** 행 이름 — 더보기 버튼 이름 「{행 이름} 동작 더보기」에 쓴다. */
  rowLabel: string;
  /** 주 동작 1개(선택) — ghost 버튼으로 바깥에 보인다(예: 「수정」). */
  primary?: { label: string; onClick: () => void; icon?: React.ReactNode; disabled?: boolean };
  /** 더보기 메뉴 항목. 일반 동작 → 구분선 → 위험 동작 순으로 정렬해 보인다. */
  items: RowActionItem[];
  /** `⋯` 트리거 ref(선택) — 확인 뒤 이어지는 결과 다이얼로그가 같은 복귀 대상을 쓸 때(계약 ④). */
  triggerRef?: React.Ref<HTMLButtonElement>;
  /**
   * 좁은 화면(< 768px)에서 주 동작을 더보기 메뉴의 첫 항목으로 접는다 — 표 행 동작은 `⋯` 하나(plan §6.7 표 규칙).
   * 선택(opt-in, 기본 false): 넓은 화면과 `matchMedia`가 없는 환경에서는 지금처럼 주 동작 버튼을 그린다.
   */
  foldPrimaryOnNarrow?: boolean;
  className?: string;
}

interface PendingConfirm {
  confirm: RowActionConfirm;
  run: () => void | Promise<unknown>;
  destructive: boolean;
}

function assignRef<T>(ref: React.Ref<T> | undefined, value: T | null) {
  if (typeof ref === "function") ref(value);
  else if (ref) (ref as React.RefObject<T | null>).current = value;
}

function ItemContent({ item }: { item: RowActionItem }) {
  return (
    <>
      {item.icon}
      {item.disabled && item.disabledReason ? (
        <span className="flex flex-col">
          <span>{item.label}</span>
          <span className="text-xs text-muted-foreground">{item.disabledReason}</span>
        </span>
      ) : (
        item.label
      )}
    </>
  );
}

/**
 * 표 행 동작(§6.5): 주 동작 1개 + `⋯` 더보기 메뉴. 위험 항목(과 `confirm`을 준 일반 항목)은 ConfirmDialog를
 * 거치고, 다이얼로그의 복귀 대상은 그 행의 `⋯` 트리거다(메뉴는 선택 뒤 닫히므로 메뉴 항목은 복귀 대상이 아니다 — r1 PR-12).
 * `⋯` 트리거에는 `data-row-actions-trigger`(ROW_ACTIONS_TRIGGER_ATTR)를 붙여, 행이 사라질 때 ConfirmDialog가
 * 다음·이전 행의 트리거를 대체 포커스로 찾게 한다(계약 ③).
 *
 * 메뉴가 닫힐 때의 포커스 — 규칙 하나(P6 정리, HCI-P3-4·HCI-P5-7):
 * - 확인을 받는 항목(위험 항목·`confirm` 일반 항목): 메뉴는 포커스를 돌려주지 않는다(`finalFocus={false}`). 확인
 *   다이얼로그가 「취소」로 초기 포커스를 가져가고, 닫히면 그 다이얼로그가 ⋯로 돌려준다.
 * - 그 밖의 일반 항목(접힌 주 동작 포함): 고르는 즉시 `onSelect`를 실행하고, 메뉴는 Base UI 기본 복귀(`true`)를 쓴다 —
 *   메뉴가 다 닫히는 순간 포커스가 아직 메뉴 안이거나 사라졌으면(body) ⋯로 돌리고, 항목이 연 모달·다른 요소로 이미
 *   옮겨졌으면 건드리지 않는다. 모달은 연 시점의 포커스 기록(⋯)으로 닫힐 때 돌아온다.
 *
 * 예전 문제(메뉴 → 모달 포커스)의 원인은 `finalFocus`를 함수로 준 것이었다 — Base UI는 명시 복귀(함수·요소)일 때
 * 다른 곳으로 옮겨진 포커스를 존중하지 않아, 닫히는 메뉴가 모달의 초기 포커스를 ⋯로 덮었다. P3는 `opensDialog`(닫힘 뒤
 * 실행), P5는 P5-02(복귀를 끄고 사라졌을 때만 ⋯로)로 같은 문제를 따로 우회했는데, 불리언 복귀로 원인을 없애 두 경로를
 * 하나로 합쳤다(`reports/phase-6/merge-report.md` §2.1, `impl-clean.md` ③).
 */
export function RowActions({
  rowLabel,
  primary,
  items,
  triggerRef,
  foldPrimaryOnNarrow = false,
  className,
}: RowActionsProps) {
  const ownTriggerRef = useRef<HTMLButtonElement | null>(null);
  // 메뉴가 닫힐 때 ⋯로 포커스를 돌려줄지 — 확인을 받는 항목을 고르면 false(확인 다이얼로그가 초기 포커스를 가져간다).
  // 불리언이어야 한다: 함수형 finalFocus는 Base UI가 「이미 다른 곳으로 옮겨진 포커스」를 존중하지 않는다(위 설명).
  const [returnFocusOnClose, setReturnFocusOnClose] = useState(true);
  // 확인 대기 중인 항목 — 위험 항목, 또는 `confirm`을 준 일반 항목
  const [pending, setPending] = useState<PendingConfirm | null>(null);
  const [open, setOpen] = useState(false);

  const setTriggerRef = useCallback(
    (el: HTMLButtonElement | null) => {
      ownTriggerRef.current = el;
      assignRef(triggerRef, el);
    },
    [triggerRef],
  );

  function requestConfirm(next: PendingConfirm) {
    setReturnFocusOnClose(false);
    setPending(next);
    setOpen(true);
  }

  const narrow = useNarrowViewport();
  // 접힌 주 동작은 메뉴의 첫 일반 항목이 된다(이름·비활성·아이콘 그대로)
  const folded = foldPrimaryOnNarrow && narrow && primary !== undefined;
  const menuItems: RowActionItem[] =
    folded && primary
      ? [
          { label: primary.label, icon: primary.icon, disabled: primary.disabled, onSelect: primary.onClick },
          ...items,
        ]
      : items;

  const normal = menuItems.filter((i): i is RowAction => !i.destructive);
  const danger = menuItems.filter((i): i is RowDangerAction => i.destructive === true);

  return (
    <div className={`flex items-center justify-end gap-1 ${className ?? ""}`}>
      {primary && !folded && (
        <Button
          variant="ghost"
          size="sm"
          className="coarse:min-h-11 coarse:min-w-11"
          onClick={primary.onClick}
          disabled={primary.disabled}
        >
          {primary.icon}
          {primary.label}
        </Button>
      )}
      {menuItems.length > 0 && (
        <DropdownMenu
          onOpenChange={(next) => {
            if (next) setReturnFocusOnClose(true);
          }}
        >
          <DropdownMenuTrigger
            render={
              <Button
                ref={setTriggerRef}
                variant="ghost"
                size="icon-sm"
                className="coarse:size-11"
                aria-label={`${rowLabel} 동작 더보기`}
                data-row-actions-trigger=""
              />
            }
          >
            <MoreHorizontal aria-hidden />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-auto min-w-40" finalFocus={returnFocusOnClose}>
            {normal.map((item) => (
              <DropdownMenuItem
                key={item.label}
                className="coarse:min-h-11"
                disabled={item.disabled}
                onClick={() => {
                  if (item.confirm) {
                    requestConfirm({ confirm: item.confirm, run: item.onSelect, destructive: false });
                    return;
                  }
                  item.onSelect();
                }}
              >
                <ItemContent item={item} />
              </DropdownMenuItem>
            ))}
            {normal.length > 0 && danger.length > 0 && <DropdownMenuSeparator />}
            {danger.map((item) => (
              <DropdownMenuItem
                key={item.label}
                variant="destructive"
                className="coarse:min-h-11"
                disabled={item.disabled}
                onClick={() => requestConfirm({ confirm: item.confirm, run: item.onConfirm, destructive: true })}
              >
                <ItemContent item={item} />
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
      {pending && (
        <ConfirmDialog
          open={open}
          onOpenChange={setOpen}
          title={pending.confirm.title}
          description={pending.confirm.description}
          actionLabel={pending.confirm.actionLabel}
          destructive={pending.destructive}
          onConfirm={() => pending.run()}
          returnFocusTo={ownTriggerRef}
          awaitConfirm={pending.confirm.awaitConfirm}
          pendingLabel={pending.confirm.pendingLabel}
        />
      )}
    </div>
  );
}
