import { expect, type Page, test } from "@playwright/test";
import { API_BASE_URL, assertLoopbackUrl } from "./helpers";

// 관리 「사용 제한」의 코어 항목(이슈 #228 B, 코어 v0.26.0 #386, core-v0.26.0-upgrade AC-15) — 실 API.
//
// 분당 레이트 리밋 하나(「소스 원문 조회」 `rate_limit_source_content`)를 화면에서 재정의해 저장하고, 다시 읽은 화면과 API에
// 적용 값으로 보이는지 확인한 뒤 「지우기」 → 저장으로 env 기본값으로 되돌린다. 저장은 바꾼 키만 보낸다(부분 갱신).
//
// 쓰기 스펙 — 시스템 전체 설정(`system_settings["rate_limits"]`)을 바꾼다. `E2E_LIMITS_WRITE=1`일 때만 돈다. 시작할 때 그 키에
// 재정의가 이미 있으면 덮지 않고 실패한다. 끝나면(실패해도) API로 재정의를 지운다(`null` — env 값).

const WRITE_ENABLED = process.env.E2E_LIMITS_WRITE === "1";
const RATE_LIMITS = "/api/v1/admin/rate-limits";
const KEY = "rate_limit_source_content";
const LABEL = "소스 원문 조회";
// env 기본값(60)보다 큰 값 — 실행 중 다른 사용자를 더 막지 않는다
const OVERRIDE = 4321;

type RateLimitItem = { key: string; env_value: number; override: number | null; effective: number };

async function readItem(page: Page): Promise<RateLimitItem> {
  const res = await page.request.get(`${API_BASE_URL}${RATE_LIMITS}`);
  expect(res.status(), "레이트 리밋 조회").toBe(200);
  const items = ((await res.json()) as { items: RateLimitItem[] }).items;
  const item = items.find((i) => i.key === KEY);
  expect(item, `${KEY} 항목`).toBeDefined();
  return item!;
}

const isRateLimitPut = (url: string, method: string) =>
  method === "PUT" && new URL(url).pathname === RATE_LIMITS;

test.describe("관리 사용 제한 — 코어 레이트 리밋 저장·되돌리기(#228 AC-15)", () => {
  test.skip(!WRITE_ENABLED, "쓰기 스펙(시스템 레이트 리밋 재정의) — E2E_LIMITS_WRITE=1 로 명시 실행할 때만 동작한다");
  test.describe.configure({ retries: 0 });

  test("레이트 리밋 하나를 재정의해 저장하면 적용 값이 바뀌고, 지우면 env 기본값으로 돌아간다", async ({ page, baseURL }) => {
    assertLoopbackUrl(baseURL, "Playwright baseURL");
    assertLoopbackUrl(API_BASE_URL, "E2E API baseURL");
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(err.message));
    page.on("console", (msg) => {
      if (msg.type() === "error" && /Content Security Policy|CSP/i.test(msg.text())) errors.push(msg.text());
    });

    const before = await readItem(page);
    expect(before.override, `${KEY}에 이미 재정의가 있다 — 덮지 않는다`).toBeNull();
    const envText = before.env_value <= 0 ? "꺼짐" : before.env_value.toLocaleString("ko-KR");

    try {
      await page.goto("/admin/limits");
      await expect(page.getByRole("heading", { level: 1, name: "사용 제한", exact: true })).toBeVisible({
        timeout: 15_000,
      });
      const section = page.getByRole("region", { name: "분당 레이트 리밋" });
      const row = section.getByTestId(`rate-limit-${KEY}`);
      const input = section.getByLabel(`${LABEL} 재정의`, { exact: true });
      await expect(input).toHaveValue("");
      await expect(row.getByTestId("env-value")).toHaveText(envText);
      await expect(row.getByTestId("effective-value")).toContainText("env");

      // ① 재정의 저장 — 바꾼 키만 보낸다
      await input.fill(String(OVERRIDE));
      const saved = page.waitForResponse((r) => isRateLimitPut(r.url(), r.request().method()));
      await section.getByRole("button", { name: "레이트 리밋 저장" }).click();
      const saveRes = await saved;
      expect(saveRes.status()).toBe(200);
      expect(saveRes.request().postDataJSON()).toEqual({ [KEY]: OVERRIDE });
      await expect(page.getByText("분당 레이트 리밋을 저장했습니다")).toBeVisible();
      await expect(row.getByTestId("effective-value")).toContainText(OVERRIDE.toLocaleString("ko-KR"));
      await expect(row.getByTestId("effective-value")).toContainText("재정의");

      // 다시 읽어도(새로 고침·API) 적용 값이 재정의다 — env 값은 그대로
      await page.reload();
      await expect(input).toHaveValue(String(OVERRIDE));
      await expect(row.getByTestId("env-value")).toHaveText(envText);
      await expect(row.getByTestId("effective-value")).toContainText(OVERRIDE.toLocaleString("ko-KR"));
      expect(await readItem(page)).toEqual({ ...before, override: OVERRIDE, effective: OVERRIDE });

      // ② 되돌리기 — 「지우기」 뒤 저장하면 null을 보내 env 기본값으로 돌아간다
      await row.getByRole("button", { name: `${LABEL} 재정의 지우기` }).click();
      await expect(input).toHaveValue("");
      await expect(row.getByText("저장하면 env 값으로 돌아갑니다")).toBeVisible();
      const cleared = page.waitForResponse((r) => isRateLimitPut(r.url(), r.request().method()));
      await section.getByRole("button", { name: "레이트 리밋 저장" }).click();
      const clearRes = await cleared;
      expect(clearRes.status()).toBe(200);
      expect(clearRes.request().postDataJSON()).toEqual({ [KEY]: null });
      await expect(row.getByTestId("effective-value")).toContainText("env");
      await expect(row.getByTestId("effective-value")).toContainText(envText);
      expect(await readItem(page)).toEqual(before);
      expect(errors, errors.join("\n")).toEqual([]);
    } finally {
      // 실패해도 재정의를 남기지 않는다(soft — 본문의 원래 실패를 덮지 않는다)
      const status = await page.request
        .put(`${API_BASE_URL}${RATE_LIMITS}`, { data: { [KEY]: null } })
        .then((res) => res.status(), (err: unknown) => `요청 실패: ${String(err)}`);
      console.log(`admin-limits: ${KEY} 재정의 정리 → ${status}`);
      expect.soft(status, `${KEY} 재정의 정리`).toBe(200);
    }
  });
});
