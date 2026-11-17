import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import type { Me, Role } from "../api/client";
import { Shell } from "../App";
import { toSettings } from "../pages/admin/Directories";

function me(roles: Role[]): Me {
  return { login: "x", display_name: "X", email: null, local: false, directory: "acme", roles,
           sme_sources: [], pii_processor: false, conflicts: [], via: {} };
}

function links(): string[] {
  return screen.getAllByRole("link").map((a) => a.textContent ?? "");
}

describe("navigation follows the roles", () => {
  beforeEach(() => vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("[]")));
  afterEach(() => vi.restoreAllMocks());

  it("a user sees neither administration nor audit", () => {
    render(<MemoryRouter><Shell me={me(["user"])} onSignedOut={vi.fn()} /></MemoryRouter>);
    expect(links()).toEqual(["Home"]);
  });

  it("an administrator sees the administration", () => {
    render(<MemoryRouter><Shell me={me(["admin", "user"])} onSignedOut={vi.fn()} /></MemoryRouter>);
    expect(links()).toEqual(["Home", "Data sources", "Directories", "Roles & grants", "Accounts"]);
  });

  it("an auditor sees the audit, and a page outside their role says so", () => {
    render(<MemoryRouter initialEntries={["/admin/grants"]}><Shell me={me(["auditor"])} onSignedOut={vi.fn()} /></MemoryRouter>);
    expect(links()).toEqual(["Home", "Audit"]);
    expect(screen.getByText("Your role does not include this page.")).toBeInTheDocument();
  });
});

describe("the directory form", () => {
  it("maps onto the settings the API validates", () => {
    const settings = toSettings({
      name: "acme", urls: "ldaps://dc1, ldaps://dc2", start_tls: false, tls_verify: true, ca_file: "",
      flavor: "ad", bind_dn: " cn=svc,dc=acme ", base_dn: "dc=acme", user_base: "", user_filter: "",
      login_attribute: "", role_kind: "attribute", group_base: "", group_filter: "", nested: true,
      nested_strategy: "client", attribute: "department", ou_base: "", password: "x",
    });
    expect(settings).toEqual({
      urls: ["ldaps://dc1", "ldaps://dc2"], start_tls: false, tls_verify: true, ca_file: undefined,
      flavor: "ad", bind_dn: "cn=svc,dc=acme", base_dn: "dc=acme", user_base: undefined,
      user_filter: undefined, login_attribute: undefined,
      role_source: { kind: "attribute", attribute: "department" },
    });
    expect(JSON.stringify(settings)).not.toContain("password");
  });
});
