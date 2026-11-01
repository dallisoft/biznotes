import { afterEach, describe, expect, it, vi } from "vitest";
import {
  api,
  ApiError,
  onPasswordChangeRequired,
  PASSWORD_CHANGE_REQUIRED,
} from "@/lib/api-client";

/** fetch 목 — 호출 순서대로 응답을 돌려주고 인자를 기록한다. */
function mockFetch(responses: Response[]) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fn = vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    const next = responses.shift();
    if (!next) throw new Error("예상보다 많은 fetch 호출");
    return next;
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

afterEach(async () => {
  // api-client의 refreshPromise는 setTimeout(…, 0)으로 해제된다.
  // 테스트 간 캐시가 새지 않도록 매크로태스크 한 틱을 흘려보낸다.
  await new Promise((r) => setTimeout(r, 0));
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("postForm (R4-6 / HCI-R4-4)", () => {
  it("Content-Type을 설정하지 않는다 — 브라우저가 multipart boundary를 붙여야 한다", async () => {
    const calls = mockFetch([json({ id: "s1" })]);
    const fd = new FormData();
    fd.append("file", new Blob(["x"]), "a.txt");

    await api.postForm("/api/v1/upload", fd);

    const headers = calls[0].init.headers as Record<string, string> | undefined;
    // 수동으로 Content-Type을 붙이면 boundary가 없어 서버가 파싱하지 못한다.
    expect(headers?.["Content-Type"]).toBeUndefined();
    expect(calls[0].init.body).toBe(fd);
  });

  it("401이면 refresh 후 1회 재시도한다", async () => {
    const calls = mockFetch([
      json({ detail: "expired" }, 401), // 1차 업로드
      json({ ok: true }), //              refresh
      json({ id: "s1" }), //              재시도 성공
    ]);

    const result = await api.postForm<{ id: string }>("/api/v1/upload", new FormData());

    expect(result).toEqual({ id: "s1" });
    expect(calls.map((c) => c.url)).toEqual([
      expect.stringContaining("/api/v1/upload"),
      expect.stringContaining("/api/v1/auth/refresh"),
      expect.stringContaining("/api/v1/upload"),
    ]);
  });
});

describe("getText / getBlob (R4-6)", () => {
  it("getText는 401 갱신 후 본문을 돌려준다", async () => {
    mockFetch([
      json({ detail: "expired" }, 401),
      json({ ok: true }),
      new Response("SKILL.md 본문", { status: 200 }),
    ]);
    await expect(api.getText("/api/v1/skills/1/files/SKILL.md")).resolves.toBe("SKILL.md 본문");
  });

  it("getBlob은 실패 시 ApiError를 던진다", async () => {
    mockFetch([json({ detail: "없는 내보내기" }, 404)]);
    await expect(api.getBlob("/api/v1/exports/1/download")).rejects.toBeInstanceOf(ApiError);
  });

  it("putBytes는 JSON 변환 없이 지정한 Content-Type으로 파일을 보낸다", async () => {
    const calls = mockFetch([new Response(null, { status: 204 })]);
    const bytes = new Uint8Array([0, 255, 1]);

    await expect(
      api.putBytes("/api/v1/skills/1/files/assets/logo.bin", bytes, "image/png"),
    ).resolves.toBeUndefined();

    expect(calls[0].url).toContain("/api/v1/skills/1/files/assets/logo.bin");
    expect(calls[0].init).toMatchObject({
      method: "PUT",
      body: bytes,
      headers: { "Content-Type": "image/png" },
    });
  });
});

describe("stream (R4-6 / HCI-R4-4)", () => {
  it("헤더 수신 전 401이면 재시도하고, 본문은 소비하지 않은 채 넘긴다", async () => {
    const body = new Response("data: hi\n\n", { status: 200 });
    mockFetch([json({ detail: "expired" }, 401), json({ ok: true }), body]);

    const res = await api.stream("/api/v1/chat/sessions/1/messages", { method: "POST" });

    expect(res.bodyUsed).toBe(false); // 호출측이 getReader()로 읽어야 한다
    expect(res.body).not.toBeNull();
  });

  it("에러 응답은 ApiError로 변환한다(스트림 시작 전)", async () => {
    mockFetch([json({ detail: "세션 없음" }, 404)]);
    await expect(api.stream("/api/v1/chat/sessions/x/messages")).rejects.toThrow("세션 없음");
  });
});

describe("ApiError.body (기준선 #85·#86 — 파싱된 에러 본문 보존)", () => {
  it("JSON 에러 본문을 detail과 함께 body로 보존한다", async () => {
    mockFetch([json({ detail: "검토 대기", reason: "review_pending", request_id: "r1" }, 409)]);

    const err = await api.get("/api/v1/x").catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(409);
    expect(apiErr.detail).toBe("검토 대기");
    expect(apiErr.body).toEqual({ detail: "검토 대기", reason: "review_pending", request_id: "r1" });
  });

  it("검증 422 목록이면 msg만 모으고 input은 버리되 원본 body는 그대로 둔다(#93·#96)", async () => {
    const detail = [
      { loc: ["body", "email"], msg: "value is not a valid email address", input: "a@b" },
      { loc: ["body", "password"], msg: "String should have at most 128 characters", input: "s3cret-pw" },
    ];
    mockFetch([json({ detail }, 422)]);

    const err = (await api.post("/api/v1/x", {}).catch((e: unknown) => e)) as ApiError;

    expect(err.detail).toBe(
      "value is not a valid email address; String should have at most 128 characters",
    );
    expect(err.message).toBe(err.detail);
    expect(err.message).not.toContain("s3cret-pw");
    expect(err.message).not.toContain("a@b");
    expect(err.body).toEqual({ detail });
  });

  it("객체 detail이면 detail 문자열, 없으면 message 문자열을 쓴다(#96)", async () => {
    mockFetch([
      json({ detail: { code: "invalid_current_password", detail: "현재 비밀번호가 다릅니다" } }, 400),
      json({ detail: { code: "quota", message: "한도를 넘었습니다" } }, 409),
    ]);

    const a = (await api.post("/api/v1/x", {}).catch((e: unknown) => e)) as ApiError;
    const b = (await api.post("/api/v1/x", {}).catch((e: unknown) => e)) as ApiError;

    expect(a.detail).toBe("현재 비밀번호가 다릅니다");
    expect(a.code).toBe("invalid_current_password");
    expect(b.detail).toBe("한도를 넘었습니다");
  });

  it("detail에서 읽을 글이 없으면 statusText를 쓴다(JSON 문자열을 만들지 않는다)", async () => {
    mockFetch([
      json({ detail: { code: "x", reason: "y" } }, 400),
      json({ detail: [{ loc: ["body"], input: "pw" }] }, 422),
      json({ error: "no detail key" }, 500),
    ]);

    const errs: ApiError[] = [];
    for (let i = 0; i < 3; i += 1) {
      errs.push((await api.post("/api/v1/x", {}).catch((e: unknown) => e)) as ApiError);
    }

    for (const err of errs) {
      expect(typeof err.detail).toBe("string");
      expect(err.detail).not.toMatch(/[{[]/);
      expect(err.detail).not.toContain("pw");
    }
    expect(errs[0].body).toEqual({ detail: { code: "x", reason: "y" } });
  });

  it("본문이 JSON이 아니면 statusText를 쓰고 body는 비운다", async () => {
    mockFetch([new Response("oops", { status: 502, statusText: "Bad Gateway" })]);

    const err = (await api.get("/api/v1/x").catch((e: unknown) => e)) as ApiError;

    expect(err.status).toBe(502);
    expect(err.detail).toBe("Bad Gateway");
    expect(err.body).toBeUndefined();
  });
});

const forced = () =>
  json({ detail: { code: PASSWORD_CHANGE_REQUIRED, detail: "비밀번호를 변경해야 합니다" } }, 403);

describe("403 password_change_required 통지 (AC-3.1, D-33)", () => {
  it("ApiError.code는 본문 detail.code를 노출하고 detail이 문자열·목록이면 undefined다", async () => {
    mockFetch([
      json({ detail: { code: "invalid_current_password", detail: "x" } }, 400),
      json({ detail: "plain" }, 400),
      json({ detail: [{ msg: "m" }] }, 422),
    ]);
    const a = (await api.post("/api/v1/x", {}).catch((e: unknown) => e)) as ApiError;
    const b = (await api.post("/api/v1/x", {}).catch((e: unknown) => e)) as ApiError;
    const c = (await api.post("/api/v1/x", {}).catch((e: unknown) => e)) as ApiError;
    expect(a.code).toBe("invalid_current_password");
    expect(b.code).toBeUndefined();
    expect(c.code).toBeUndefined();
  });

  it("request(get·post)의 403 code면 구독자를 한 번 부르고 code가 실린 ApiError를 던진다", async () => {
    const listener = vi.fn();
    const off = onPasswordChangeRequired(listener);
    mockFetch([forced(), forced()]);

    const getErr = (await api.get("/api/v1/notebooks").catch((e: unknown) => e)) as ApiError;
    expect(listener).toHaveBeenCalledTimes(1);
    const postErr = (await api.post("/api/v1/x", {}).catch((e: unknown) => e)) as ApiError;
    expect(listener).toHaveBeenCalledTimes(2);

    for (const err of [getErr, postErr]) {
      expect(err).toBeInstanceOf(ApiError);
      expect(err.status).toBe(403);
      expect(err.code).toBe(PASSWORD_CHANGE_REQUIRED);
    }
    off();
  });

  it("stream의 403 code도 통지한다(채팅 SSE 시작 전)", async () => {
    const listener = vi.fn();
    const off = onPasswordChangeRequired(listener);
    mockFetch([forced()]);
    await expect(
      api.stream("/api/v1/chat/sessions/1/messages", { method: "POST", body: "{}" }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledTimes(1);
    off();
  });

  it("직접 getBlob·getText·postForm·putBytes의 403 code도 통지한다", async () => {
    const listener = vi.fn();
    const off = onPasswordChangeRequired(listener);
    mockFetch([forced(), forced(), forced(), forced()]);
    await expect(api.getBlob("/api/v1/exports/1/download")).rejects.toBeInstanceOf(ApiError);
    await expect(api.getText("/api/v1/skills/1/files/a")).rejects.toBeInstanceOf(ApiError);
    await expect(api.postForm("/api/v1/upload", new FormData())).rejects.toBeInstanceOf(ApiError);
    await expect(api.putBytes("/api/v1/skills/1/files/a", new Uint8Array([1]))).rejects.toBeInstanceOf(
      ApiError,
    );
    expect(listener).toHaveBeenCalledTimes(4);
    off();
  });

  it("code 없는 403·다른 code의 403·같은 code의 다른 상태는 통지하지 않는다", async () => {
    const listener = vi.fn();
    const off = onPasswordChangeRequired(listener);
    mockFetch([
      json({ detail: "권한 없음" }, 403),
      json({ detail: { code: "other_code", detail: "x" } }, 403),
      json({ detail: { code: PASSWORD_CHANGE_REQUIRED, detail: "x" } }, 401),
      new Response("forbidden", { status: 403 }),
    ]);
    for (let i = 0; i < 4; i++) {
      await api.get("/api/v1/auth/x").catch(() => undefined);
    }
    expect(listener).not.toHaveBeenCalled();
    off();
  });

  it("정상 SSE 응답(res.ok)은 본문을 읽지 않고 통지 대상이 아니다", async () => {
    const listener = vi.fn();
    const off = onPasswordChangeRequired(listener);
    mockFetch([new Response("data: hi\n\n", { status: 200 })]);
    const res = await api.stream("/api/v1/chat/sessions/1/messages", { method: "POST" });
    expect(res.bodyUsed).toBe(false);
    expect(listener).not.toHaveBeenCalled();
    off();
  });

  it("해제 함수를 부르면 더 이상 통지받지 않고, 구독자 예외는 ApiError 전달을 막지 않는다", async () => {
    const throwing = vi.fn(() => {
      throw new Error("구독자 오류");
    });
    const listener = vi.fn();
    const offThrowing = onPasswordChangeRequired(throwing);
    const off = onPasswordChangeRequired(listener);
    mockFetch([forced(), forced()]);

    await expect(api.get("/api/v1/x")).rejects.toBeInstanceOf(ApiError);
    expect(throwing).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledTimes(1);

    off();
    offThrowing();
    await expect(api.get("/api/v1/x")).rejects.toBeInstanceOf(ApiError);
    expect(listener).toHaveBeenCalledTimes(1);
    expect(throwing).toHaveBeenCalledTimes(1);
  });
});

describe("api.get init.signal (AC-3.4, v6 PR5-02)", () => {
  it("get passes signal to fetch", async () => {
    // 전역 fetch만 대역 — 실제 fetch처럼 signal이 취소되면 AbortError로 거부한다
    const seen: (AbortSignal | null | undefined)[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (_url: string, init: RequestInit = {}) =>
          new Promise<Response>((_resolve, reject) => {
            seen.push(init.signal);
            init.signal?.addEventListener("abort", () =>
              reject(new DOMException("The operation was aborted.", "AbortError")),
            );
          }),
      ),
    );
    const controller = new AbortController();

    const pending = api.get("/api/v1/auth/me", { signal: controller.signal });
    controller.abort();

    const err = await pending.catch((e: unknown) => e);
    expect(seen).toEqual([controller.signal]);
    expect(err).not.toBeInstanceOf(ApiError);
    expect((err as DOMException).name).toBe("AbortError");
  });

  it("signal 없이 부르면 fetch init에 signal을 싣지 않는다(기존 호출 무변경)", async () => {
    const calls = mockFetch([json({ ok: true })]);
    await api.get("/api/v1/x");
    expect(calls[0].init.signal).toBeUndefined();
  });
});
