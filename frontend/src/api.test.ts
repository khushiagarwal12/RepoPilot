import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, ask, formatDetail, validateGithubUrl } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("validateGithubUrl", () => {
  it.each(["https://github.com/psf/requests", "https://github.com/psf/requests/", "https://www.github.com/a/b.git"])("accepts %s", (u) =>
    expect(validateGithubUrl(u)).toBeNull());
  it.each(["", "  ", "http://github.com/a/b", "https://github.com.evil.com/a/b", "https://github.com/a", "https://github.com/a/b/tree/main", "not a url"])(
    "rejects %j", (u) => expect(validateGithubUrl(u)).not.toBeNull());
});

describe("formatDetail", () => {
  it("handles string, validation array and unknown bodies", () => {
    expect(formatDetail({ detail: "Nope" }, 409)).toBe("Nope");
    expect(formatDetail({ detail: [{ msg: "too short" }, { msg: "bad" }] }, 422)).toBe("too short; bad");
    expect(formatDetail(null, 500)).toBe("Request failed (HTTP 500).");
  });
});

describe("request errors", () => {
  it("maps network failure to a clear ApiError(0)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fail")));
    await expect(ask(1, "q")).rejects.toMatchObject({ status: 0, message: expect.stringContaining("Cannot reach the backend") });
  });
  it("surfaces server detail and status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "LLM unavailable: x" }), { status: 503 })));
    const err = await ask(1, "q").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 503, message: "LLM unavailable: x" });
  });
});
