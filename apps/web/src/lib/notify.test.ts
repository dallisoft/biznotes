import { act, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { toast, Toaster } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api-client";
import {
  ACTION_DURATION_MS,
  describeError,
  ERROR_CODE_MESSAGE,
  ERROR_DURATION_MS,
  ERROR_STATUS_MESSAGE,
  FALLBACK_ERROR_DESCRIPTION,
  failureTitle,
  NETWORK_ERROR_MESSAGE,
  notifyError,
  notifySuccess,
  objectParticle,
  REDACTED,
  redactSecrets,
  SERVER_MESSAGE_MAX_LENGTH,
  SUCCESS_DURATION_MS,
  successMessage,
} from "./notify";

// 실제 sonner `Toaster`를 렌더하고 실제 `toast`를 부른다 — 호출 인자 확인용 spy는 원래 구현을 그대로 통과시킨다.
function renderToaster() {
  return render(createElement(Toaster));
}

afterEach(() => {
  act(() => {
    toast.dismiss();
  });
  vi.restoreAllMocks();
});

/** api-client의 `throwIfNotOk`와 같은 방식으로 ApiError를 만든다. */
function apiError(status: number, body?: unknown): ApiError {
  let detail = "Error";
  let code: string | undefined;
  if (typeof body === "object" && body !== null && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    detail = typeof d === "string" ? d : JSON.stringify(d);
    if (typeof d === "object" && d !== null && typeof (d as { code?: unknown }).code === "string") {
      code = (d as { code: string }).code;
    }
  }
  return new ApiError(status, detail, body, code);
}

describe("successMessage·objectParticle — 「{대상}을(를) {동작}했습니다」", () => {
  it("받침에 따라 을/를을 고른다", () => {
    expect(successMessage("노트북", "삭제")).toBe("노트북을 삭제했습니다");
    expect(successMessage("메모", "저장")).toBe("메모를 저장했습니다");
    expect(successMessage("MCP 서버", "추가")).toBe("MCP 서버를 추가했습니다");
    expect(successMessage("스킬", "복제")).toBe("스킬을 복제했습니다");
  });

  it("한글로 끝나지 않으면 「을(를)」", () => {
    expect(objectParticle("MCP")).toBe("을(를)");
    expect(objectParticle("v2")).toBe("을(를)");
    expect(objectParticle("")).toBe("을(를)");
  });
});

describe("failureTitle — 「{동작}하지 못했습니다」", () => {
  it("동작 뒤에 「하지 못했습니다」를 붙인다", () => {
    expect(failureTitle("저장")).toBe("저장하지 못했습니다");
    expect(failureTitle("노트북을 삭제")).toBe("노트북을 삭제하지 못했습니다");
  });
});

describe("notifySuccess (실제 Toaster)", () => {
  it("문장을 토스트로 보이고 4초 동안 유지한다", async () => {
    const spy = vi.spyOn(toast, "success");
    renderToaster();
    act(() => {
      notifySuccess("노트북을 만들었습니다");
    });
    expect(await screen.findByText("노트북을 만들었습니다")).toBeInTheDocument();
    expect(SUCCESS_DURATION_MS).toBe(4000);
    expect(spy).toHaveBeenCalledWith(
      "노트북을 만들었습니다",
      expect.objectContaining({ duration: 4000 }),
    );
  });

  it("보조 설명과 id를 넘긴다", async () => {
    const spy = vi.spyOn(toast, "success");
    renderToaster();
    act(() => {
      notifySuccess("소스를 추가했습니다", { id: "src-1", description: "처리가 끝나면 알려 드립니다" });
    });
    expect(await screen.findByText("처리가 끝나면 알려 드립니다")).toBeInTheDocument();
    expect(spy).toHaveBeenCalledWith(
      "소스를 추가했습니다",
      expect.objectContaining({ id: "src-1", description: "처리가 끝나면 알려 드립니다" }),
    );
  });

  it("버튼(「되돌리기」)을 달면 8초 동안 보이고, 누르면 콜백을 부른다(#220)", async () => {
    const spy = vi.spyOn(toast, "success");
    const onClick = vi.fn();
    renderToaster();
    act(() => {
      notifySuccess("결과를 숨겼습니다", { action: { label: "되돌리기", onClick } });
    });
    const button = await screen.findByRole("button", { name: "되돌리기" });
    expect(ACTION_DURATION_MS).toBe(8000);
    expect(spy).toHaveBeenCalledWith(
      "결과를 숨겼습니다",
      expect.objectContaining({ duration: 8000 }),
    );
    act(() => button.click());
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("비밀값 모양은 화면에 나오지 않는다(비밀값 토스트 금지 방어선)", async () => {
    renderToaster();
    act(() => {
      notifySuccess("비밀번호를 초기화했습니다", { description: "임시 비밀번호: Xy7_aB3-kLmN" });
    });
    expect(await screen.findByText(`임시 비밀번호: ${REDACTED}`)).toBeInTheDocument();
    expect(screen.queryByText(/Xy7_aB3-kLmN/)).not.toBeInTheDocument();
  });
});

describe("notifyError (실제 Toaster)", () => {
  it("모르는 오류: 제목 + 「잠시 후 다시 시도해 주세요」 + 서버 메시지 둘째 줄, 6초", async () => {
    const spy = vi.spyOn(toast, "error");
    renderToaster();
    act(() => {
      notifyError("저장", apiError(500, { detail: "database is locked" }));
    });
    expect(await screen.findByText("저장하지 못했습니다")).toBeInTheDocument();
    const description = screen.getByText(/잠시 후 다시 시도해 주세요/);
    expect(description.textContent).toBe(`${FALLBACK_ERROR_DESCRIPTION}\ndatabase is locked`);
    expect(ERROR_DURATION_MS).toBe(6000);
    expect(spy).toHaveBeenCalledWith(
      "저장하지 못했습니다",
      expect.objectContaining({
        duration: 6000,
        classNames: { description: "whitespace-pre-line" },
      }),
    );
  });

  it("알려진 코드는 한국어 사전 문구만 보인다(서버 메시지 없음)", async () => {
    renderToaster();
    act(() => {
      notifyError(
        "노트북을 공유",
        apiError(403, { detail: { code: "password_change_required", message: "must change" } }),
      );
    });
    expect(await screen.findByText(ERROR_CODE_MESSAGE.password_change_required)).toBeInTheDocument();
    expect(screen.queryByText(/must change/)).not.toBeInTheDocument();
  });

  it("오류 없이 부르면 기본 문구 한 줄", async () => {
    renderToaster();
    act(() => {
      notifyError("불러오기");
    });
    expect(await screen.findByText("불러오기하지 못했습니다")).toBeInTheDocument();
    expect(screen.getByText(FALLBACK_ERROR_DESCRIPTION)).toBeInTheDocument();
  });
});

describe("describeError — 설명 줄 규칙", () => {
  it("코드 사전이 상태 사전보다 먼저다", () => {
    expect(describeError(apiError(400, { detail: { code: "invalid_current_password" } }))).toEqual([
      ERROR_CODE_MESSAGE.invalid_current_password,
    ]);
  });

  it("코드가 없으면 HTTP 상태 사전", () => {
    expect(describeError(apiError(404, { detail: "Notebook not found" }))).toEqual([
      ERROR_STATUS_MESSAGE[404],
    ]);
    expect(describeError(apiError(429))).toEqual([ERROR_STATUS_MESSAGE[429]]);
  });

  it("미지 코드·미지 상태는 기본 문구 + 서버 메시지", () => {
    expect(describeError(apiError(502, { detail: { code: "upstream_down", message: "gateway timeout" } }))).toEqual([
      FALLBACK_ERROR_DESCRIPTION,
      "gateway timeout",
    ]);
  });

  it("422는 서버 메시지를 붙이지 않는다(검증 본문이 입력값을 되돌려줄 수 있다)", () => {
    expect(describeError(apiError(422, { detail: "password 'hunter2xyz' is too weak" }))).toEqual([
      FALLBACK_ERROR_DESCRIPTION,
    ]);
    expect(
      describeError(apiError(422, { detail: [{ loc: ["body", "password"], input: "hunter2xyz" }] })),
    ).toEqual([FALLBACK_ERROR_DESCRIPTION]);
  });

  it("배열·message 없는 구조화 본문은 서버 메시지로 쓰지 않는다", () => {
    expect(describeError(apiError(500, { detail: [{ msg: "x" }] }))).toEqual([FALLBACK_ERROR_DESCRIPTION]);
    expect(describeError(apiError(500, { detail: { code: "x" } }))).toEqual([FALLBACK_ERROR_DESCRIPTION]);
  });

  it("본문 없는 응답은 statusText, detail이 비면 기본 문구만", () => {
    expect(describeError(new ApiError(503, "Service Unavailable"))).toEqual([
      FALLBACK_ERROR_DESCRIPTION,
      "Service Unavailable",
    ]);
    expect(describeError(new ApiError(500, "", { error: "x" }))).toEqual([FALLBACK_ERROR_DESCRIPTION]);
    // `detail` 키 없는 본문이면 api-client가 detail에 undefined를 넣는다
    expect(
      describeError(new ApiError(500, undefined as unknown as string, { message: "x" })),
    ).toEqual([FALLBACK_ERROR_DESCRIPTION]);
  });

  it("네트워크 오류는 연결 안내", () => {
    expect(describeError(new TypeError("Failed to fetch"))).toEqual([NETWORK_ERROR_MESSAGE]);
    expect(describeError(new TypeError("Load failed"))).toEqual([NETWORK_ERROR_MESSAGE]);
  });

  it("일반 Error·문자열은 기본 문구 + 메시지, 그 밖의 값은 기본 문구만", () => {
    expect(describeError(new Error("boom"))).toEqual([FALLBACK_ERROR_DESCRIPTION, "boom"]);
    expect(describeError("plain")).toEqual([FALLBACK_ERROR_DESCRIPTION, "plain"]);
    expect(describeError({ weird: true })).toEqual([FALLBACK_ERROR_DESCRIPTION]);
    expect(describeError(new Error(FALLBACK_ERROR_DESCRIPTION))).toEqual([FALLBACK_ERROR_DESCRIPTION]);
  });

  it("긴 서버 메시지는 잘라서 보인다", () => {
    const [, line] = describeError(new Error("a".repeat(500)));
    expect(line).toBe(`${"a".repeat(SERVER_MESSAGE_MAX_LENGTH)}…`);
  });

  it("서버 메시지의 비밀값 모양을 가린다", () => {
    expect(describeError(apiError(500, { detail: "bad token=abcd1234efgh for user" }))).toEqual([
      FALLBACK_ERROR_DESCRIPTION,
      `bad token=${REDACTED} for user`,
    ]);
  });
});

describe("redactSecrets — 비밀값 토스트 금지 방어선", () => {
  it("키=값·JSON·한국어 라벨 형식의 비밀값을 가린다", () => {
    expect(redactSecrets("password=Sup3rSecret!")).toBe(`password=${REDACTED}`);
    expect(redactSecrets('{"temporary_password":"Xy7_aB3-kLmN"}')).toBe(
      `{"temporary_password":"${REDACTED}"}`,
    );
    expect(redactSecrets("webhook secret: whsec_9f8e7d6c")).toBe(`webhook secret: ${REDACTED}`);
    expect(redactSecrets("api_key = sk-live-abc123")).toBe(`api_key = ${REDACTED}`);
    expect(redactSecrets("임시 비밀번호: Xy7_aB3-kLmN")).toBe(`임시 비밀번호: ${REDACTED}`);
    expect(redactSecrets("시크릿=Q2xhdWRl")).toBe(`시크릿=${REDACTED}`);
  });

  it("Bearer 토큰과 JWT를 가린다", () => {
    expect(redactSecrets("Authorization: Bearer abc.def-ghi_jkl")).toBe(
      `Authorization: Bearer ${REDACTED}`,
    );
    const jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk";
    expect(redactSecrets(`expired ${jwt}`)).toBe(`expired ${REDACTED}`);
  });

  it("비밀값이 아닌 일반 문장·식별자·숫자는 그대로 둔다", () => {
    for (const text of [
      "노트북을 만들었습니다",
      "비밀번호를 변경했습니다",
      "토큰: 1,234",
      "max_tokens=4096",
      "Notebook 3fa85f64-5717-4562-b3fc-2c963f66afa6 not found",
      "token budget exceeded",
    ]) {
      expect(redactSecrets(text)).toBe(text);
    }
  });
});
