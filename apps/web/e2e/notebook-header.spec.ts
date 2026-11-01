import { expect, test } from "@playwright/test";
import { createNotebookViaApi, deleteNotebookViaApi, gotoDashboard } from "./helpers";

// ui-ux-redesign P3 AC-3.10(r1 PR-09) — 노트북 헤더에서 바꾼 이름이 헤더 h1과 사이드바에 함께 보이고(캐시 동기화),
// 헤더에서 지우면 노트북 홈으로 replace된다. 노트북은 이 스펙이 API로 만들고, UI 삭제가 실패하면 finally에서 API로 지운다.

test("AC-3.10 노트북 헤더에서 이름을 바꾸면 h1과 사이드바가 함께 바뀌고, 헤더에서 삭제하면 노트북 홈으로 간다", async ({
  page,
}) => {
  await gotoDashboard(page);
  const stamp = Date.now();
  const title = `PW 헤더 ${stamp}`;
  const renamed = `PW 헤더 바뀐 이름 ${stamp}`;
  const notebookId = await createNotebookViaApi(page, title);
  let deletedViaUi = false;
  try {
    await page.goto(`/notebooks/${notebookId}`);
    const header = page.getByTestId("notebook-header");
    await expect(header.getByRole("heading", { level: 1, name: title })).toBeVisible();
    // 브레드크럼 「노트북」은 노트북 홈 링크다(heading 아님)
    await expect(
      page.getByRole("navigation", { name: "위치" }).getByRole("link", { name: "노트북", exact: true }),
    ).toHaveAttribute("href", "/");
    // 사이드바의 현재 노트북 항목(링크 이름 「{이모지} {제목}」)
    const sidebarNotebooks = page.getByRole("group", { name: "노트북", exact: true });
    await expect(sidebarNotebooks.getByRole("link", { name: new RegExp(`${title}$`) })).toBeVisible();

    // 1) 헤더 더보기 → 이름 변경 — 키보드로 연다(⋯ 포커스 → Enter → 첫 항목 「이름 변경」 → Enter). 이름 칸이 포커스를
    //    가져야 한다: 예전 `autoFocus`는 닫히는 메뉴가 ⋯로 포커스를 되돌리며 덮어 포커스가 모달 밖에 남았다(P3 리뷰 수정 중
    //    실브라우저에서 확인 — initialFocus로 수정). 그래서 이름은 키보드 입력으로 바꾼다.
    const more = header.getByRole("button", { name: "노트북 동작 더보기" });
    await more.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("menuitem", { name: "이름 변경" })).toBeFocused();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: "노트북 이름 변경" });
    const nameInput = dialog.getByRole("textbox");
    await expect(nameInput).toBeFocused();
    await page.waitForTimeout(300); // 메뉴 닫힘 뒤에도 포커스가 이름 칸에 남는지
    await expect(nameInput).toBeFocused();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.type(renamed);
    await expect(nameInput).toHaveValue(renamed);
    const patched = page.waitForResponse(
      (r) => new URL(r.url()).pathname === `/api/v1/notebooks/${notebookId}` && r.request().method() === "PATCH",
    );
    await dialog.getByRole("button", { name: "저장" }).click();
    expect((await patched).status()).toBe(200);
    await expect(dialog).toBeHidden();

    // h1과 사이드바가 같은 새 이름이다(새로고침 없이)
    await expect(header.getByRole("heading", { level: 1, name: renamed })).toBeVisible();
    await expect(sidebarNotebooks.getByRole("link", { name: new RegExp(`${renamed}$`) })).toBeVisible();
    await expect(sidebarNotebooks.getByRole("link", { name: new RegExp(`${title}$`) })).toHaveCount(0);

    // 2) 헤더 더보기 → 노트북 삭제 → 확인 → 노트북 홈으로 replace, 지워진 노트북을 다시 읽지 않는다
    const detailGetsAfterDelete: string[] = [];
    let deleteSeen = false;
    page.on("request", (req) => {
      const path = new URL(req.url()).pathname;
      if (path === `/api/v1/notebooks/${notebookId}` && req.method() === "DELETE") deleteSeen = true;
      else if (deleteSeen && path.startsWith(`/api/v1/notebooks/${notebookId}`) && req.method() === "GET") {
        detailGetsAfterDelete.push(path);
      }
    });
    await header.getByRole("button", { name: "노트북 동작 더보기" }).click();
    await page.getByRole("menuitem", { name: "노트북 삭제" }).click();
    const confirm = page.getByRole("alertdialog", { name: `‘${renamed}’ 노트북을 삭제할까요?` });
    await expect(confirm).toBeVisible();
    const deleted = page.waitForResponse(
      (r) => new URL(r.url()).pathname === `/api/v1/notebooks/${notebookId}` && r.request().method() === "DELETE",
    );
    await confirm.getByRole("button", { name: "삭제" }).click();
    expect((await deleted).status()).toBe(204);
    deletedViaUi = true;
    await page.waitForURL((url) => url.pathname === "/");
    await expect(page.getByRole("heading", { name: "노트북", exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("노트북을 삭제했습니다").first()).toBeVisible();
    await expect(sidebarNotebooks.getByRole("link", { name: new RegExp(`${renamed}$`) })).toHaveCount(0);
    expect(detailGetsAfterDelete, "삭제 뒤 지워진 노트북(하위 경로 포함) GET 요청").toEqual([]);
  } finally {
    if (!deletedViaUi) await deleteNotebookViaApi(page, notebookId, "notebook-header");
  }
});
