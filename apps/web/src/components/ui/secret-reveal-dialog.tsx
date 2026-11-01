"use client";

import { Copy } from "lucide-react";
import { useEffect, useId, useRef } from "react";
import { Button } from "@/components/ui/button";
import { type FocusTarget, useReturnFocus } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { notifyError, notifySuccess, objectParticle, successMessage } from "@/lib/notify";

/** 결과 다이얼로그에 보일 값 하나. `secret`인 값은 한 번만 보이는 비밀값이다(첫 비밀값의 「복사」가 초기 포커스). */
export interface SecretRevealField {
  label: string;
  value: string;
  secret?: boolean;
  /** 기존 선택자 유지용(D-17) — 예: `webhook-secret`. */
  testId?: string;
}

/** 결과 다이얼로그 내용 — 비밀값을 담으므로 화면의 React 상태(`useSecretScope`)에만 둔다. */
export interface SecretRevealContent {
  /** 예: 「임시 비밀번호를 발급했습니다」 */
  title: string;
  /** 대상 설명(비밀값 아님) — 예: 「‘demo@example.com’ 계정」 */
  description?: string;
  fields: readonly SecretRevealField[];
  /** 아래 안내(비밀값 아님) — 예: 강제 변경 안내, 서명 방법 */
  notice?: string;
}

/** 모든 결과 다이얼로그가 보이는 경고(§6.6.2 사용자 행). */
export const SECRET_REVEAL_WARNING = "이 창을 닫으면 다시 볼 수 없습니다.";

/**
 * 열린 직후 이 시간(ms) 안에 포커스가 다이얼로그 밖으로 끌려가면 「복사」로 되돌린다. 막 닫힌 다른 팝업(다른 행의
 * `⋯` 메뉴·입력 다이얼로그)의 뒤늦은 포커스 복귀가 초기 포커스를 빼앗는 경합을 막는다(P5 통합 — secure R-1).
 * 모달이라 사용자가 키보드로 밖에 나갈 수는 없고, 바깥 클릭은 닫기로 처리된다.
 */
export const INITIAL_FOCUS_SETTLE_MS = 1000;

async function copyValue(field: SecretRevealField) {
  const object = `${field.label}${objectParticle(field.label)}`;
  try {
    if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(field.value);
    // 토스트에는 비밀값을 넣지 않는다(§6.5 토스트) — 「임시 비밀번호를 복사했습니다」
    notifySuccess(successMessage(field.label, "복사"));
  } catch {
    notifyError(`${object} 복사`);
  }
}

function FieldRow({
  field,
  copyRef,
}: {
  field: SecretRevealField;
  copyRef?: React.Ref<HTMLButtonElement>;
}) {
  const labelId = useId();
  return (
    <div className="grid gap-1.5">
      <span id={labelId} className="text-sm font-medium">
        {field.label}
      </span>
      <div className="flex items-start gap-2">
        <code
          aria-labelledby={labelId}
          data-testid={field.testId}
          className="min-w-0 flex-1 rounded-md bg-muted px-2 py-1.5 font-mono text-sm break-all select-all"
        >
          {field.value || "—"}
        </code>
        <Button
          ref={copyRef}
          type="button"
          variant="outline"
          size="sm"
          className="coarse:min-h-11 coarse:min-w-11"
          aria-label={`${field.label} 복사`}
          disabled={!field.value}
          onClick={() => void copyValue(field)}
        >
          <Copy aria-hidden />
          복사
        </Button>
      </div>
    </div>
  );
}

/**
 * 한 번만 보이는 비밀값(임시 비밀번호·웹훅 시크릿)의 결과 다이얼로그(plan §6.5, D-11·D-22). 자동화 화면의 지역 구현을 공용화했다.
 *
 * - 초기 포커스 = 첫 비밀값의 「복사」(§6.5 ConfirmDialog 계약 ④).
 * - 닫기 버튼·Esc·바깥 클릭은 모두 `onClose` 하나로 모인다 — 호출부는 비밀값 상태를 지우고 epoch를 올린다
 *   (`useSecretScope().close`). 닫으면 `returnFocusTo`(행의 `⋯` 등)로, 그것이 사라졌으면 행 대체 후보로 돌아간다.
 * - `content`가 `null`이 되면 닫힌다(로그아웃·경로 이탈 포함). 닫히는 동안에도 비밀값을 다시 그리지 않는다.
 * - 이 컴포넌트는 비밀값을 상태로 들고 있지 않는다 — 받은 `content`를 그릴 뿐이다.
 */
export function SecretRevealDialog({
  content,
  onClose,
  returnFocusTo,
}: {
  content: SecretRevealContent | null;
  onClose: () => void;
  returnFocusTo: FocusTarget;
}) {
  const copyRef = useRef<HTMLButtonElement>(null);
  // 닫는 중(onOpenChange(false) 뒤)에는 아래 초기 포커스 보호를 멈춘다 — 복귀 대상으로 가는 포커스를 막지 않게
  const closingRef = useRef(false);
  const { capture, resolve } = useReturnFocus(returnFocusTo);
  const open = content !== null;

  useEffect(() => {
    if (open) capture();
  }, [open, capture]);

  // 초기 포커스 보호(secure R-1): 열린 뒤 잠깐 동안 다이얼로그 밖으로 옮겨진 포커스를 「복사」로 되돌린다.
  useEffect(() => {
    if (!open) return;
    closingRef.current = false;
    const openedAt = Date.now();
    function onFocusIn(event: FocusEvent) {
      if (closingRef.current || Date.now() - openedAt > INITIAL_FOCUS_SETTLE_MS) return;
      const copy = copyRef.current;
      const popup = copy?.closest('[data-slot="dialog-content"]');
      const target = event.target;
      if (!copy || !popup || !(target instanceof Node) || popup.contains(target)) return;
      copy.focus();
    }
    document.addEventListener("focusin", onFocusIn);
    return () => document.removeEventListener("focusin", onFocusIn);
  }, [open]);

  // 바깥 클릭으로 닫으면 Base UI는 스크롤 방지를 확인할 수 없는 환경에서 포커스 복귀를 건너뛴다(FloatingFocusManager).
  // 계약 ②(바깥 클릭 → 복귀 대상)를 환경과 무관하게 지키려고, 클릭이 끝난 뒤 포커스가 문서에 떨어져 있으면 직접 돌려준다.
  function restoreAfterOutsidePress() {
    const doc = copyRef.current?.ownerDocument ?? document;
    doc.defaultView?.setTimeout(() => {
      const active = doc.activeElement;
      if (active && active !== doc.body) return;
      const target = resolve();
      if (target !== true) target.focus();
    }, 0);
  }

  const fields = content?.fields ?? [];
  const focusIndex = Math.max(
    0,
    fields.findIndex((f) => f.secret),
  );

  return (
    <Dialog
      open={open}
      onOpenChange={(next, details) => {
        if (next) return;
        closingRef.current = true;
        if (details?.reason === "outside-press") restoreAfterOutsidePress();
        onClose();
      }}
    >
      <DialogContent
        showCloseButton={false}
        initialFocus={copyRef}
        finalFocus={() => resolve()}
        className="sm:max-w-lg"
        data-testid="secret-reveal-dialog"
      >
        <DialogHeader>
          <DialogTitle>{content?.title ?? ""}</DialogTitle>
          <DialogDescription>
            {content?.description ? `${content.description} ` : ""}
            <strong className="font-medium text-foreground">{SECRET_REVEAL_WARNING}</strong>
          </DialogDescription>
        </DialogHeader>
        {fields.length > 0 && (
          <div className="grid gap-3">
            {fields.map((field, i) => (
              <FieldRow
                key={field.label}
                field={field}
                copyRef={i === focusIndex ? copyRef : undefined}
              />
            ))}
          </div>
        )}
        {content?.notice && <p className="text-sm text-muted-foreground">{content.notice}</p>}
        <DialogFooter>
          <DialogClose render={<Button variant="outline" className="coarse:min-h-11" />}>
            닫기
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
