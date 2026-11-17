// The D75 host run's findings (OWNER_VERIFICATION §26, D76, D77): the runs filter, the chat
// page of someone with nothing to ask about, a page that fails to render, the directory whose
// last test failed, and what a category proposal rests on.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import type { DirectoryView, Me, ObjectView, Role } from "../api/client";
import { Shell } from "../App";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { Categories, needsLook } from "../pages/admin/Categories";
import { Directories } from "../pages/admin/Directories";
import { ChatPage } from "../pages/chat/Chat";
import { Runs, sourceOptions } from "../pages/queries/Queries";

function me(roles: Role[], extra: Partial<Me> = {}): Me {
  return { login: "x", display_name: "X", email: null, local: false, directory: "acme", roles,
           sme_sources: [], pii_processor: false, conflicts: [], via: {}, ...extra };
}

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
}

afterEach(() => vi.restoreAllMocks());

describe("runs", () => {
  it("keeps every data source on offer after one is chosen", async () => {
    const asked: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      asked.push(url);
      const only = new URL(url, "http://x").searchParams.get("data_source");
      const totals = ["demo_pg", "pg_copy"].filter((n) => !only || n === only)
        .map((n) => ({ data_source: n, llm_calls: 1, prompt_tokens: 1, completion_tokens: 1, cost: null, queries: 0 }));
      return json({ data_sources: ["demo_pg", "pg_copy"], totals, reasoning: [], profiling: [] });
    });
    render(<MemoryRouter><Runs /></MemoryRouter>);
    const select = await screen.findByRole("combobox");
    await waitFor(() => expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["any", "demo_pg", "pg_copy"]));
    await userEvent.selectOptions(select, "demo_pg");
    await waitFor(() => expect(asked.at(-1)).toContain("data_source=demo_pg"));
    // the bug: only "any" and the chosen one were left — the other needed "any" first
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["any", "demo_pg", "pg_copy"]);
    await userEvent.selectOptions(select, "pg_copy");
    await waitFor(() => expect(asked.at(-1)).toContain("data_source=pg_copy"));
  });

  it("offers the chosen data source even when the list does not name it", () => {
    expect(sourceOptions(["b", "a"], "")).toEqual(["a", "b"]);
    expect(sourceOptions(undefined, "c")).toEqual(["c"]);
  });
});

describe("a chat with nothing to ask about says why", () => {
  it("the local account: no Chat in the navigation, and the page explains", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json([]));
    const local = me(["admin"], { login: "gaia-admin", local: true, directory: null });
    render(<MemoryRouter initialEntries={["/chat"]}><Shell me={local} onSignedOut={vi.fn()} /></MemoryRouter>);
    const nav = within(screen.getByRole("navigation")).getAllByRole("link").map((a) => a.textContent);
    expect(nav).not.toContain("Chat");
    expect(await screen.findByText(/local administrator account cannot chat/)).toBeInTheDocument();
  });

  it("an administrator without categories is pointed at the grants", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json([]));
    render(<MemoryRouter><ChatPage me={me(["admin", "user"])} /></MemoryRouter>);
    expect(await screen.findByText(/As an administrator you may grant categories/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Roles & grants" })).toHaveAttribute("href", "/admin/grants");
  });

  it("a user without categories is told to ask an administrator", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json([]));
    render(<MemoryRouter><ChatPage me={me(["user"])} /></MemoryRouter>);
    expect(await screen.findByText(/Ask your administrator for a data category/)).toBeInTheDocument();
  });
});

describe("a page that fails to render", () => {
  function Broken(): never {
    throw new Error("payload.confidence is undefined");
  }

  it("shows the error instead of a blank window", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);  // React reports it too
    render(<ErrorBoundary resetKey="/chat"><Broken /></ErrorBoundary>);
    expect(screen.getByRole("alert")).toHaveTextContent("This page could not be shown.");
    expect(screen.getByRole("alert")).toHaveTextContent("payload.confidence is undefined");
  });
});

describe("a directory whose last test failed", () => {
  it("is said to stay offered, with the failed check", async () => {
    const acme: DirectoryView = {
      name: "acme", kind: "ldap", status: "verified", settings: {}, probed_at: "2026-11-17T18:00:00Z",
      probe: [{ name: "transport", ok: false, detail: "LDAPS to dc1.acme.test:6360: the server's certificate was signed by a CA named like the one in … but with a different key" }],
      synced_at: null, sync: null, created_by: "cli:murat", last_test_failed: true,
    };
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => json([acme]));
    render(<MemoryRouter><Directories /></MemoryRouter>);
    expect(await screen.findByText(/These settings passed before, so people are still offered/)).toBeInTheDocument();
    expect(screen.getByText(/with a different key/)).toBeInTheDocument();
  });
});

describe("what a category proposal rests on", () => {
  const objects: ObjectView[] = [
    { id: "1", name: "dbo.inventory", kind: "table", business_name: "Inventory",
      category: { id: "c1", name: "Store", status: "proposed", source: "gaia", decided_by: null, decided_at: "2026-11-17T18:00:00Z",
                  reason: "matches “inventory” in its name", basis: "direct" } },
    { id: "2", name: "dbo.inventory_movements", kind: "table", business_name: "Inventory Movements",
      category: { id: "c1", name: "Store", status: "proposed", source: "gaia", decided_by: null, decided_at: "2026-11-17T18:00:00Z",
                  reason: "named after dbo.inventory", basis: "related" } },
    { id: "3", name: "dbo.releases", kind: "table", business_name: "Game Releases", category: null },
    { id: "4", name: "dbo.shops", kind: "table", business_name: "Shops",
      category: { id: "c1", name: "Store", status: "confirmed", source: "sme", decided_by: "akaya", decided_at: "2026-11-17T18:00:00Z",
                  reason: null, basis: null } },
  ];

  it("marks what to check, and the page can show only those", async () => {
    expect(objects.map(needsLook)).toEqual([false, true, true, false]);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/objects")) return json(objects);
      if (url.includes("/api/admin/data-sources")) {
        return json([{ name: "demo_mssql", dialect: "mssql", origin: "config", status: "config", introspected: false },
                     { name: "nge", dialect: "mssql", origin: "admin", status: "verified", introspected: true }]);
      }
      return json([{ id: "c1", name: "Store", description: null, keywords: ["store", "inventory"], objects: 3, grants: 1 }]);
    });
    render(<MemoryRouter><Categories /></MemoryRouter>);
    // a data source not read yet is not chosen (it has no objects to place)
    expect(await screen.findByText("dbo.inventory_movements")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "demo_mssql (not read yet)" })).toBeDisabled();
    expect(screen.getByText("related tables")).toHaveClass("basis-related");
    expect(screen.getByText("its names")).toHaveClass("basis-direct");
    await userEvent.click(screen.getByRole("checkbox", { name: /only those to check/ }));
    expect(screen.queryByText("dbo.inventory")).not.toBeInTheDocument();
    expect(screen.getByText("dbo.inventory_movements")).toBeInTheDocument();
    expect(screen.getByText("dbo.releases")).toBeInTheDocument();
    expect(screen.queryByText("dbo.shops")).not.toBeInTheDocument();
  });
});
