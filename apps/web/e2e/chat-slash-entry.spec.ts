import { expect, test } from "@playwright/test";
import { API_BASE_URL, cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// #66 채팅 `/` 팔레트 진입 — ① 팔레트에서 스킬을 골라 실행하면 run 카드가 끝나고 결과가 대화에 남는다.
// 실스택(api·worker의 PR-1 코드)과 실제 기본 LLM으로 돈다. 관리자 본인의 private legacy 스킬을
// API로 만들고(동결) 끝에서 지운다. ② 승인 카드 → 결과(스크립트 흐름)는 PR-3(#259 런타임 뒤)에서 더한다.
cleanupCreatedNotebooks("chat-slash-entry");

test("① `/` 팔레트에서 스킬을 고르고 질문하면 run 카드가 「완료」되고 결과가 남는다(새로고침 뒤에도)", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const name = `pw-slash-${Date.now()}`;
  const created = await page.request.post(`${API_BASE_URL}/api/v1/skills`, {
    data: { name, description: "e2e 채팅 진입 요약 스킬" },
  });
  expect(created.status(), await created.text()).toBe(201);
  const skill = (await created.json()) as { id: string; slug: string };
  try {
    const frozen = await page.request.post(`${API_BASE_URL}/api/v1/skills/${skill.id}/freeze`, {
      data: { changelog: "v1" },
    });
    expect(frozen.status(), await frozen.text()).toBe(200);

    await gotoDashboard(page);
    await createNotebookAndOpen(page, `PW 슬래시 진입 ${Date.now()}`);

    const input = page.getByTestId("chat-input");
    await input.click();
    await input.pressSequentially(`/${skill.slug}`);
    const palette = page.getByRole("listbox", { name: "실행할 스킬·워크플로" });
    await expect(palette).toBeVisible();
    await palette.getByRole("option", { name: new RegExp(skill.slug) }).click();
    await expect(page.getByTestId("entry-chip")).toContainText(`/${skill.slug}`);

    const question = "이 노트북을 한 문장으로 요약해 줘";
    await input.fill(question);
    const posted = page.waitForResponse(
      (r) => r.url().endsWith("/entry-runs") && r.request().method() === "POST",
    );
    await page.getByRole("button", { name: "전송" }).click();
    const response = await posted;
    expect(response.status(), await response.text()).toBe(202);
    const { run } = (await response.json()) as { run: { id: string; trigger: string } };
    expect(run.trigger).toBe("chat");

    const messages = page.getByTestId("chat-messages");
    const card = page.getByTestId(`entry-run-card-${run.id}`);
    await expect(card).toBeVisible();
    await expect(card.getByTestId("entry-run-status")).toHaveText("완료", { timeout: 120_000 });
    // 결과 행(assistant, run_id)이 대화에 들어오고 입력 잠금(D3)이 풀린다
    await expect(messages.getByTestId("markdown-content")).toHaveCount(1, { timeout: 30_000 });
    await expect(page.getByTestId("entry-locked-hint")).toHaveCount(0);
    // 진입 결과에는 「다시 생성」이 없다
    await expect(messages.getByRole("button", { name: "다시 생성" })).toHaveCount(0);

    await page.reload();
    await expect(page.getByTestId(`entry-run-card-${run.id}`).getByTestId("entry-run-status")).toHaveText("완료");
    await expect(messages.getByText(question, { exact: true })).toHaveCount(1);
    await expect(messages.getByTestId("markdown-content")).toHaveCount(1);
  } finally {
    const status = await page.request
      .delete(`${API_BASE_URL}/api/v1/skills/${skill.id}`)
      .then((r) => r.status(), (err: unknown) => `요청 실패: ${String(err)}`);
    console.log(`chat-slash-entry: skill ${skill.id} 정리 → ${status}`);
    expect.soft(status, `스킬 ${skill.id} 정리`).toBe(204);
  }
});
