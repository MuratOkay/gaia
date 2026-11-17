import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Login } from "../pages/Login";

function mockApi(options: object, login: (body: Record<string, unknown>) => Response) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const path = String(input);
    if (path.startsWith("/api/auth/options")) return new Response(JSON.stringify(options));
    if (path.startsWith("/api/auth/login")) return login(JSON.parse(String(init!.body)));
    return new Response("{}", { status: 404 });
  });
}

const ME = { login: "akaya", display_name: "Ayse Kaya", email: null, local: false, directory: "acme",
             roles: ["user"], sme_sources: [], pii_processor: false, conflicts: [], via: {} };

describe("the sign-in screen", () => {
  afterEach(() => vi.restoreAllMocks());

  it("signs in through the directory", async () => {
    const seen: Record<string, unknown>[] = [];
    mockApi({ directories: [{ name: "acme", kind: "ldap" }], local: false, local_setup_pending: false },
            (body) => { seen.push(body); return new Response(JSON.stringify(ME)); });
    const onSignedIn = vi.fn();
    render(<Login onSignedIn={onSignedIn} />);
    await userEvent.type(await screen.findByLabelText("User name"), "akaya");
    await userEvent.type(screen.getByLabelText("Password"), "Ayse-Password-1");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(ME));
    expect(seen).toEqual([{ login: "akaya", password: "Ayse-Password-1", directory: "acme" }]);
  });

  it("says the same thing for every refusal", async () => {
    mockApi({ directories: [{ name: "acme", kind: "ldap" }], local: false, local_setup_pending: false },
            () => new Response(JSON.stringify({ detail: "sign-in failed" }), { status: 401 }));
    render(<Login onSignedIn={vi.fn()} />);
    await userEvent.type(await screen.findByLabelText("User name"), "akaya");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in failed.");
  });

  it("the local administrator signs in without a directory", async () => {
    const seen: Record<string, unknown>[] = [];
    mockApi({ directories: [{ name: "acme", kind: "ldap" }], local: true, local_setup_pending: false },
            (body) => { seen.push(body); return new Response(JSON.stringify({ ...ME, login: "gaia-admin", local: true })); });
    render(<Login onSignedIn={vi.fn()} />);
    await userEvent.selectOptions(await screen.findByLabelText("Sign in with"), "Local administrator (gaia-admin)");
    expect(screen.queryByLabelText("User name")).toBeNull();
    await userEvent.type(screen.getByLabelText("Password"), "Local-Admin-Pass-1");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(seen).toEqual([{ login: "gaia-admin", password: "Local-Admin-Pass-1" }]));
  });

  it("offers the first-time setup when a code is pending", async () => {
    mockApi({ directories: [], local: false, local_setup_pending: true }, () => new Response("{}"));
    render(<Login onSignedIn={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Set up the local administrator" })).toBeInTheDocument();
    expect(screen.getByLabelText("Setup code")).toBeInTheDocument();
  });

  it("offers Microsoft for an Entra ID directory, without a password form", async () => {
    mockApi({ directories: [{ name: "corp", kind: "entra" }], local: false, local_setup_pending: false },
            () => new Response("{}", { status: 500 }));
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign, search: "", pathname: "/" });
    render(<Login onSignedIn={vi.fn()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Sign in with Microsoft (corp)" }));
    expect(assign).toHaveBeenCalledWith("/api/auth/oidc/start?directory=corp");
    expect(screen.queryByLabelText("Password")).toBeNull();
    vi.unstubAllGlobals();
  });

  it("says so when Microsoft sent the browser back refused", async () => {
    window.history.pushState(null, "", "/?signin=failed");
    mockApi({ directories: [{ name: "corp", kind: "entra" }], local: false, local_setup_pending: false },
            () => new Response("{}"));
    render(<Login onSignedIn={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in failed.");
    expect(window.location.search).toBe("");  // the marker does not stay in the address bar
  });
});
