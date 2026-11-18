import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { Me, Role } from "../api/client";
import { Shell } from "../App";
import { EMPTY, toEntraSettings, toSettings } from "../pages/admin/Directories";

function me(roles: Role[]): Me {
  return { login: "x", display_name: "X", email: null, local: false, directory: "acme", roles,
           sme_sources: [], pii_processor: false, conflicts: [], via: {} };
}

function links(): string[] {
  return within(screen.getByRole("navigation")).getAllByRole("link").map((a) => a.textContent ?? "");
}

describe("navigation follows the roles", () => {
  beforeEach(() => vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("[]")));
  afterEach(() => vi.restoreAllMocks());

  it("a user sees the chat, and neither administration nor audit", () => {
    render(<MemoryRouter><Shell me={me(["user"])} onSignedOut={vi.fn()} /></MemoryRouter>);
    expect(links()).toEqual(["Home", "Chat"]);
  });

  it("an administrator sees the administration", () => {
    render(<MemoryRouter><Shell me={me(["admin", "user"])} onSignedOut={vi.fn()} /></MemoryRouter>);
    expect(links()).toEqual(["Home", "Chat", "Queries", "Data sources", "Directories", "Data categories",
                             "Roles & grants", "Accounts", "Runs", "Evaluation", "Models"]);
  });

  it("an auditor sees the audit, and a page outside their role says so", () => {
    render(<MemoryRouter initialEntries={["/admin/grants"]}><Shell me={me(["auditor"])} onSignedOut={vi.fn()} /></MemoryRouter>);
    expect(links()).toEqual(["Home", "Audit"]);
    expect(screen.getByText("Your role does not include this page.")).toBeInTheDocument();
  });
});

describe("the Audit menu stays open on its shared pages", () => {
  afterEach(() => vi.restoreAllMocks());

  for (const path of ["/audit/queries", "/audit/runs", "/audit/evaluation"]) {
    it(`on ${path}`, async () => {
      vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
        const u = String(input);
        if (u.includes("/api/queries")) {
          return new Response(JSON.stringify({ total: 1, items: [{ id: "q-1", question: "How many?", attempt: 1,
            status: "ok", created_at: "2026-11-18T09:00:00Z", data_source: "demo_pg", asked_by: "finn", tables: [],
            row_count: 1, elapsed_ms: 5, origin: "api", errors: [], warnings: [], forbidden: [], parent_id: null,
            dialect: "postgres", submitted_sql: "SELECT 1", executed_sql: "SELECT 1", row_cap_reached: false, error: null, model: "m" }] }));
        }
        if (u.includes("/api/runs")) return new Response(JSON.stringify({ totals: [], reasoning: [], profiling: [] }));
        return new Response("[]");
      });
      render(<MemoryRouter initialEntries={[path]}><Shell me={me(["auditor"])} onSignedOut={vi.fn()} /></MemoryRouter>);
      const menu = await screen.findByRole("navigation", { name: "Audit" });
      const current = within(menu).getAllByRole("link").find((a) => a.getAttribute("href") === path);
      expect(current).toHaveClass("active");
      expect(within(menu).getByRole("link", { name: "Who may do what" })).toBeInTheDocument();
      if (path === "/audit/queries") {
        expect(await screen.findByRole("link", { name: "How many?" })).toHaveAttribute("href", "/audit/queries/q-1");
      }
    });
  }
});

describe("the directory form", () => {
  it("maps onto the settings the API validates", () => {
    const settings = toSettings({
      ...EMPTY,
      name: "acme", urls: "ldaps://dc1, ldaps://dc2", start_tls: false, tls_verify: true, ca_file: "",
      flavor: "ad", bind_dn: " cn=svc,dc=acme ", base_dn: "dc=acme", user_base: "", user_filter: "",
      login_attribute: "", role_kind: "attribute", group_base: "", group_filter: "", nested: true,
      nested_strategy: "client", attribute: "department", ou_base: "", password: "x",
    });
    expect(settings).toEqual({
      urls: ["ldaps://dc1", "ldaps://dc2"], start_tls: false, tls_verify: true, ca_file: undefined,
      ca_pem: undefined, flavor: "ad", bind_dn: "cn=svc,dc=acme", base_dn: "dc=acme", user_base: undefined,
      user_filter: undefined, login_attribute: undefined,
      role_source: { kind: "attribute", attribute: "department" },
    });
    expect(JSON.stringify(settings)).not.toContain("password");
  });
});

describe("the Entra ID form", () => {
  it("maps onto the settings the API validates, with the callback of this site", () => {
    const settings = toEntraSettings({ ...EMPTY, kind: "entra", name: "corp", tenant_id: " t-1 ", client_id: "c-1",
                                       password: "secret", name_prefix: "GAIA-" }, "http://localhost:5173");
    expect(settings).toEqual({
      tenant_id: "t-1", client_id: "c-1", redirect_uri: "http://localhost:5173/api/auth/oidc/callback",
      user_filter: undefined, role_source: { kind: "group", name_prefix: "GAIA-", security_only: true },
    });
    expect(JSON.stringify(settings)).not.toContain("secret");
    expect(toEntraSettings({ ...EMPTY, entra_role_kind: "attribute", entra_attribute: "jobTitle" }, "x").role_source)
      .toEqual({ kind: "attribute", attribute: "jobTitle" });
  });
});
