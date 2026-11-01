import { expect, test } from "@playwright/test";
import { installRagLoopbackGuard } from "./helpers";

test.beforeEach(async ({ page, baseURL }) => { await installRagLoopbackGuard(page, baseURL); });

// 코어 v0.9.1 프론트 반영(core-v0.9.1-upgrade Phase 2, HCI-6 (a)+(b))의 실행 검증.
// (a) admin/llm 임베딩 모델 교체 안내 5단계, (b) admin/rag 재인덱싱 응답의 skipped 표시.
//
// 실행 전제 (Phase 2 리뷰 P2-02·P2-03 반영):
//  - 아래 (b) 스펙은 **실제 재인덱싱을 발행하는 쓰기 스펙**이다. 전체 스펙 일괄 실행
//    (원격 CI·nightly)에서 대상 스택에 실작업을 큐잉하지 않도록 기본은 skip이며,
//    `E2E_REINDEX_WRITE=1` 을 준 로컬·검증 전용 실행에서만 동작한다.
//    운영 스택에서는 실행하지 않는다.
//  - `skipped`/`run_id`는 코어 v0.9.1의 `ReindexStartOut` 필드다. 실스택 api가 아직
//    구 코어면 계약을 검증할 수 없으므로 **green이 아니라 skip**으로 드러낸다
//    (false green 방지 — AGENTS.md 「라이브 확인」). `E2E_CORE_MIN=0.9.1` 을 주면 skip 대신
//    **실패**하므로 Phase 4 재검증에서는 이 플래그를 함께 준다.

const WRITE_ENABLED = process.env.E2E_REINDEX_WRITE === "1";
const CORE_MIN = process.env.E2E_CORE_MIN;

test("admin/llm: 임베딩 모델 교체 안내가 5단계로 렌더된다", async ({ page }) => {
  await page.goto("/admin/llm");
  const guide = page.getByTestId("embedding-swap-guide");
  await expect(guide).toBeVisible({ timeout: 15_000 });
  // 접이식 info 콜아웃(ui-ux-redesign §6.6.2 LLM 행) — 제목을 눌러 연다
  const summary = guide.getByText("임베딩 모델 교체 순서");
  await expect(summary).toBeVisible();
  await summary.click();
  await expect(guide).toHaveAttribute("open", "");

  const steps = guide.getByRole("listitem");
  await expect(steps).toHaveCount(5);
  await expect(steps.nth(0)).toContainText("비활성화");
  // HTTP 상태 코드(409)는 보이지 않는다(AC-5.4) — 결과만 말한다
  await expect(steps.nth(1)).toContainText("거부됩니다");
  await expect(guide).not.toContainText("409");
  await expect(steps.nth(2)).toContainText("기본 모델로 지정");
  // 4단계는 현행 UI에서 실행 가능한 절차여야 한다 — 존재하지 않는 `stale_only` 버튼이
  // 아니라 '전체 재인덱싱' + '처리 중' 관측을 지시한다(P2-01).
  await expect(steps.nth(3)).toContainText("전체 재인덱싱");
  await expect(steps.nth(3)).toContainText("처리 중");
  await expect(steps.nth(3)).toContainText("실패한 작업 재시도");
  await expect(steps.nth(4)).toContainText("삭제");
  // 구 안내문(전역 1개 고정)은 제거됐다
  await expect(page.getByText("임베딩 모델은 전역 1개로 고정됩니다")).toHaveCount(0);
});

test("admin/rag: 재인덱싱 응답의 enqueued·skipped·run_id가 화면에 표시된다", async ({ page }) => {
  test.skip(
    !WRITE_ENABLED,
    "쓰기 스펙(실제 재인덱싱 발행) — E2E_REINDEX_WRITE=1 로 명시 실행할 때만 동작한다",
  );

  await page.goto("/admin/rag");
  await expect(page.getByRole("heading", { name: "RAG 관리" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByTestId("reindex-result")).toHaveCount(0);

  const response = page.waitForResponse(
    (r) => r.url().includes("/api/v1/admin/rag/reindex") && r.request().method() === "POST",
  );
  // 「실패한 작업 재시도…」는 위험 구역에 있고 확인 다이얼로그를 거친다(ui-ux-redesign §6.5 DangerZone·§6.3.3 라벨,
  // P5 리뷰 P5-05). 이름 부분 일치로 고르고, 확인 전에는 요청이 없다.
  const retry = page.getByRole("button", { name: "실패한 작업 재시도" });
  await expect(retry).toHaveCount(1);
  await expect(retry).toBeEnabled({ timeout: 15_000 }); // 부트스트랩 잠금 해제 = 활성 run 없음
  let posted = 0;
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().includes("/api/v1/admin/rag/reindex")) posted += 1;
  });
  await retry.click();
  const confirm = page.getByRole("alertdialog");
  await expect(confirm).toContainText("실패한 작업을 다시 시도할까요?");
  expect(posted, "확인 전 재인덱싱 요청 0회").toBe(0);
  await confirm.getByRole("button", { name: "재시도" }).click();
  const body = await (await response).json();
  expect(posted, "확인 후 재인덱싱 요청 1회").toBe(1);
  expect(body).toHaveProperty("enqueued");

  const result = page.getByTestId("reindex-result");
  await expect(result).toBeVisible();
  await expect(result).toContainText(`${body.enqueued}건 등록`);

  const hasCoreContract = "skipped" in body && "run_id" in body;
  if (!hasCoreContract) {
    if (CORE_MIN) {
      // 재검증 모드 — 계약 부재는 실패여야 한다.
      expect(
        hasCoreContract,
        `E2E_CORE_MIN=${CORE_MIN} 인데 실스택 api 응답에 skipped/run_id가 없다: ${JSON.stringify(body)}`,
      ).toBe(true);
    }
    test.skip(
      true,
      `실스택 api가 core<0.9.1 응답을 반환함(${JSON.stringify(body)}) — Phase 4 재빌드 후 E2E_CORE_MIN=0.9.1 로 재실행 필요 (BL-60)`,
    );
  }

  expect(typeof body.skipped).toBe("number");
  await expect(result).toContainText(
    body.skipped > 0 ? `${body.skipped}건 건너뜀` : "건너뛴 소스 없음",
  );
  // 원시 run UUID 대신 「요청 번호 앞 8자」 + 복사 버튼(ui-ux-redesign §6.6.2 RAG 행)
  if (body.run_id) {
    await expect(result).toContainText(`요청 번호 ${String(body.run_id).slice(0, 8)}`);
    await expect(result.getByRole("button", { name: "요청 번호 복사" })).toBeVisible();
  } else {
    await expect(result).toContainText("요청 번호 없음");
  }
});
