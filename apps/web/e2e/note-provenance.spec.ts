import { expect, test, type Page } from "@playwright/test";
import { setMessageProvenanceNull } from "./db";
import { cleanupCreatedNotebooks, createNotebookAndOpen, gotoDashboard } from "./helpers";

// 테스트가 만든 노트북(「PW 」 접두)은 테스트마다 생성 응답의 id로 API 삭제하고, 파일 실행 전후 접두 개수가 같음을
// 확인한다(ui-ux-redesign P6 — BL-183 잔여, AC-6.2).
cleanupCreatedNotebooks("note-provenance");

async function registerTextSource(page: Page, title: string, body: string) {
  await page.getByTestId("add-source").click();
  await page.getByRole("tab", { name: "텍스트" }).click();
  await page.getByPlaceholder("제목 (선택)").fill(title);
  await page.getByPlaceholder("텍스트를 붙여넣으세요").fill(body);
  await page.getByRole("button", { name: "등록" }).click();
  await expect(page.getByRole("checkbox", { name: new RegExp(`${title} 선택`) })).toBeEnabled({
    timeout: 60_000,
  });
}

/** 전송 응답(SSE 전체 본문)에서 `done.message_id`를 캡처한다 — 리듀서(sse.ts)는 버린다(R3-04ⓒ). */
async function sendAndCaptureMessageId(page: Page, question: string): Promise<string> {
  const responsePromise = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      /\/api\/v1\/chat\/sessions\/[^/]+\/messages$/.test(r.url()),
  );
  await page.getByTestId("chat-input").fill(question);
  await page.getByRole("button", { name: "전송" }).click();
  const response = await responsePromise;
  const body = await response.text();

  let messageId: string | null = null;
  for (const frame of body.split("\n\n")) {
    const line = frame.split("\n").find((l) => l.startsWith("data: "));
    if (!line) continue;
    try {
      const event = JSON.parse(line.slice(6)) as { type: string; message_id?: string };
      if (event.type === "done" && typeof event.message_id === "string") {
        messageId = event.message_id;
      }
    } catch {
      // 잘못된 프레임은 건너뛴다 (sse.ts::drainSseBuffer와 동일 원칙)
    }
  }
  if (!messageId || !/^[0-9a-f-]{36}$/.test(messageId)) {
    throw new Error(`전송 응답에서 유효한 message_id를 찾지 못함: ${String(messageId)}`);
  }

  // UI가 스트리밍 완료를 반영할 때까지 대기 (저장 버튼은 STREAMING_ID가 아닌 메시지에만 붙는다)
  await expect(page.getByTestId("save-to-note").first()).toBeAttached({ timeout: 30_000 });
  return messageId;
}

test("자율 채팅 배지 복원 + 메모 생성 근거 + 편집 후 표시", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 근거 ${Date.now()}`;
  await createNotebookAndOpen(page, title);
  // 사전 지식으로 답할 수 없는 가상의 구체적 수치를 등록해 rag_search 도구 호출을
  // 사실상 강제한다(LLM이 조회 없이 추측해 버리면 evidence가 비어 배지 검증이
  // 흔들리는 것을 막기 위함 — 실측으로 확인된 결함).
  await registerTextSource(
    page,
    "가상 장비 큐제타-7 매뉴얼",
    "가상 장비 큐제타-7의 권장 보관 습도는 정확히 41.5%이다.",
  );

  // (1) 자율 채팅 후 배지 — react 루프가 rag_search 도구를 호출하면 배지가 뜬다.
  // [BL-22] 라이브 배지는 이제 done.provenance.items(= 영속화된 provenance)로 그려지므로
  // 구 도구-호출 배지 전제는 사라졌다. 다만 items 구성은 LLM의 도구 선택(recall)에
  // 좌우되므로 여기서는 컨테이너 가시성만 단언하고, evidence-badge 자체는 안정적인
  // reload 단계(아래 (2))에서 단언한다(phase-3-review.md P3-06).
  await sendAndCaptureMessageId(
    page,
    "가상 장비 큐제타-7의 권장 보관 습도가 몇 %야? 등록된 소스를 검색해서 정확한 수치로만 답해줘. 모르면 추측하지 말고 반드시 검색해.",
  );
  await expect(page.getByTestId("agent-evidence").first()).toBeVisible({ timeout: 15_000 });

  // (2) reload 복원 — 영속화된 provenance.items에서 배지가 다시 그려진다
  await page.reload();
  await expect(page.getByTestId("agent-evidence").first()).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByTestId("agent-evidence").first().locator('[data-testid^="evidence-badge-"]').first(),
  ).toBeVisible();

  // (3) 저장 메모 생성 근거
  await page.getByTestId("save-to-note").first().click();
  await page.getByTestId("right-tab-notes").click();
  const noteList = page.getByTestId("note-list");
  await expect(noteList.getByText("채팅에서 저장")).toBeVisible({ timeout: 10_000 });
  await noteList.getByText("채팅에서 저장").click();
  await expect(page.getByTestId("note-viewer")).toBeVisible();
  const provenance = page.getByTestId("note-provenance");
  await expect(provenance).toBeVisible();
  await expect(provenance).toContainText("자율 채팅");
  await expect(provenance).not.toContainText("생성 근거 기록 없음");

  // (4) 편집 후 "생성 후 편집됨"
  await page.getByTestId("note-edit").click();
  await page.getByTestId("note-content-input").fill("직접 덧붙인 메모 내용");
  await page.getByTestId("note-save").click();
  await noteList.getByText("채팅에서 저장").click();
  await expect(page.getByTestId("note-provenance-edited")).toBeVisible();
  await expect(page.getByTestId("note-provenance-edited")).toHaveText(
    "생성 후 편집됨 — 아래 근거는 생성 시점 기준",
  );
});

test("레거시(provenance NULL) 메시지 → 노트 생성 근거 미기록", async ({ page }) => {
  await gotoDashboard(page);
  const title = `PW 레거시 ${Date.now()}`;
  await createNotebookAndOpen(page, title);
  await registerTextSource(page, "메밀차 자료", "메밀차는 볶은 메밀로 우려낸 차다.");

  const messageId = await sendAndCaptureMessageId(page, "메밀차는 어떤 재료로 만들어?");

  // (5) legacy 재현: DB에서 provenance를 NULL로 되돌린 뒤 새로고침
  await setMessageProvenanceNull(messageId);
  await page.reload();
  await expect(page.getByTestId("agent-evidence")).toHaveCount(0);

  // "메모에 저장" — 서버가 본문을 원문에서 복사하고 mode="unrecorded"로 스냅샷한다(D14)
  await page.getByTestId("save-to-note").first().click();
  await page.getByTestId("right-tab-notes").click();
  const noteList = page.getByTestId("note-list");
  await expect(noteList.getByText("채팅에서 저장")).toBeVisible({ timeout: 10_000 });
  await noteList.getByText("채팅에서 저장").click();
  await expect(page.getByTestId("note-viewer")).toBeVisible();

  const provenance = page.getByTestId("note-provenance");
  await expect(provenance).toBeVisible();
  await expect(provenance).toContainText("생성 근거 미기록");
  await expect(provenance).toContainText("본문은 채팅 원문에서 서버가 복사했습니다");

  // 편집 후에도 "생성 후 편집됨"이 함께 표시된다
  await page.getByTestId("note-edit").click();
  await page.getByTestId("note-content-input").fill("레거시 메모 직접 수정");
  await page.getByTestId("note-save").click();
  await noteList.getByText("채팅에서 저장").click();
  await expect(page.getByTestId("note-provenance-edited")).toBeVisible();
});
