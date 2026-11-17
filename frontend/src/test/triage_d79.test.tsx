// The D77 run's findings (OWNER_VERIFICATION §27, D79): the chat that broke in newer browsers.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";

import type { AccountView, Me } from "../api/client";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { Accounts, AccountTable } from "../pages/admin/Accounts";
import { ChatPage } from "../pages/chat/Chat";

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
}

const finn: Me = { login: "finn", display_name: "Finn", email: null, local: false, directory: "acme", roles: ["user"],
                   sme_sources: [], pii_processor: false, conflicts: [], via: {}, category_names: ["Sales"] };

afterEach(() => vi.restoreAllMocks());

describe("the chat in a browser whose scroll methods return a Promise", () => {
  it("opens (the owner's browser: 'destroy is not a function')", async () => {
    // newer browsers return a Promise from scrollIntoView; an effect returning it was taken
    // for its clean-up, and StrictMode's unmount right after the mount called it
    Element.prototype.scrollIntoView = vi.fn(() => Promise.resolve()) as unknown as Element["scrollIntoView"];
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/data-sources")) {
        return json([{ name: "demo_pg", dialect: "postgres", origin: "config", status: "config", usable: true, default: true }]);
      }
      return json([]);
    });
    render(
      <StrictMode>
        <MemoryRouter>
          <ErrorBoundary resetKey="/chat"><ChatPage me={finn} /></ErrorBoundary>
        </MemoryRouter>
      </StrictMode>,
    );
    expect(await screen.findByText(/Ask a question about demo_pg/)).toBeInTheDocument();
    expect(screen.queryByText("This page could not be shown.")).not.toBeInTheDocument();
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });
});

describe("effects", () => {
  function files(dir: string): string[] {
    return readdirSync(dir).flatMap((f: string) => {
      const p = join(dir, f);
      return statSync(p).isDirectory() ? (f === "test" ? [] : files(p)) : /\.tsx?$/.test(f) ? [p] : [];
    });
  }

  it("never return the value of an expression (only a clean-up function, or nothing)", () => {
    const bare = files(join(__dirname, ".."))
      .flatMap((f) => [...readFileSync(f, "utf8").matchAll(/useEffect\(\s*\(\)\s*=>\s*(?=[^\s{(])([^\n]*)/g)]
        .map((m) => `${f.split("/src/")[1]}: ${m[1].slice(0, 60)}`));
    expect(bare).toEqual([]);
  });
});

describe("an account whose directory object changed (D78)", () => {
  const akaya: AccountView = {
    login: "akaya", directory: "acme", local: false, display_name: "A. K.", email: null, enabled: true,
    last_sign_in_at: null, synced_at: null, directory_roles: ["GAIA-Finance"], roles: ["user"], sme_sources: [],
    pii_processor: true, conflicts: [], via: {}, identity_changed_at: "2026-11-18T09:00:00Z",
  };

  it("the administrator decides: re-link or start anew", async () => {
    const posted: unknown[] = [];
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (String(input).endsWith("/identity")) {
        posted.push(JSON.parse(String(init?.body)));
        return json([{ login: "akaya", directory: "acme", action: "renew", grants_revoked: 1 }]);
      }
      return json([akaya]);
    });
    render(<MemoryRouter><Accounts /></MemoryRouter>);
    expect(await screen.findByText(/Another directory object has carried this login/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Someone else: start anew" }));
    await waitFor(() => expect(posted).toEqual([{ directory: "acme", login: "akaya", action: "renew" }]));
  });

  it("the auditor sees it, without the buttons", () => {
    render(<MemoryRouter><AccountTable rows={[akaya]} /></MemoryRouter>);
    expect(screen.getByRole("alert")).toHaveTextContent("Sign-ins are refused until you decide.");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
