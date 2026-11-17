import { api, ApiError, csrfToken, SIGNED_OUT_EVENT, url } from "../api/client";

describe("the API client", () => {
  afterEach(() => vi.restoreAllMocks());

  it("reads the CSRF token from the readable cookie", () => {
    expect(csrfToken("a=1; gaia_csrf=abc%3D; gaia_session=zzz")).toBe("abc=");
    expect(csrfToken("a=1")).toBeUndefined();
  });

  it("sends the CSRF token on changes only", async () => {
    document.cookie = "gaia_csrf=tok123";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", { status: 200 }));
    await api("/api/x");
    await api("/api/x", { method: "POST", body: { a: 1 } });
    const [getCall, postCall] = fetchMock.mock.calls;
    expect((getCall[1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
    expect((postCall[1]!.headers as Record<string, string>)["X-CSRF-Token"]).toBe("tok123");
    expect(postCall[1]!.credentials).toBe("same-origin");
  });

  it("turns a 401 into a signed-out signal and an error with the API's detail", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ detail: "the session has expired" }), { status: 401 }));
    const heard = vi.fn();
    window.addEventListener(SIGNED_OUT_EVENT, heard);
    await expect(api("/api/auth/me")).rejects.toEqual(new ApiError(401, "the session has expired"));
    expect(heard).toHaveBeenCalledOnce();
    window.removeEventListener(SIGNED_OUT_EVENT, heard);
  });

  it("builds query strings without undefined values", () => {
    expect(url("/api/a", { x: 1, y: undefined, z: true })).toBe("/api/a?x=1&z=true");
  });
});
