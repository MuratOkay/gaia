import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { ColumnReview, Meaning, Me } from "../api/client";
import { Shell } from "../App";
import { ColumnPage, decisionBody, draftFrom } from "../pages/review/Column";
import { ReviewQueue } from "../pages/review/Queue";
import { tableChanges } from "../pages/review/Table";
import { ReviewTables } from "../pages/review/Tables";

const MEANING: Meaning = {
  version: 2, status: "draft", band: "abstain", score: 0.31, raw_score: 0.31, business_name: "Status",
  purpose: "The status of the invoice.", role: "code", entity: "invoice", unit: null, value_domain: null,
  value_meanings: {}, synonyms: [], example_questions: [], assumptions: [], unknowns: ["what the codes mean"],
  alternatives: [], self_confidence: 0.5, critique: null, semantic_type: null, claimed_semantic_type: null,
  coverage: "targeted", unused: false, model: "deepseek", prompt_version: "v3", created_at: "2026-11-17T10:00:00Z",
  reviewed_by: null, reviewed_at: null, review_note: null, inherited_from: null,
};

const ITEM: ColumnReview = {
  data_source: "demo_pg",
  table: { id: "t1", schema: "finance", name: "invoices", kind: "table", business_name: "Invoices", purpose: "Invoices sent.", grain: null, status: "draft" },
  column: { id: "c1", name: "status", data_type: "smallint", nullable: false, comment: null, keys: [], references: [], checks: [], indexes: [] },
  meaning: MEANING,
  summary: [{ key: "reads_as", name: "Status", role: "code", purpose: "The status of the invoice." },
            { key: "score", score: 0.31, band: "abstain", supports: 2, contradicts: 0 },
            { key: "unsure", items: "what the codes mean" }],
  breakdown: null, ledger: [], personal: false, masked: false, tests: [], join_hints: [],
  profile: { method: "sample", rows: 1000, non_null: 1000, distinct: 4, minimum: "0", maximum: "9",
             top_values: [["1", 700], ["0", 200], ["2", 80], ["9", 20]], sample_values: [] },
  lineage: { shown_in: [], comes_from: [] }, versions: [], questions: [],
  review: { id: "r1", status: "open", reasons: ["abstain", "evidence_exhausted"], priority: 0.6, importance: 0.9, coverage: "targeted", assignee: null },
  reviewable: true, roles: ["identifier", "measure", "dimension", "timestamp", "flag", "code", "free-text", "reference"],
};

function me(roles: Me["roles"]): Me {
  return { login: "akaya", display_name: "Ayse", email: null, local: false, directory: "acme", roles,
           sme_sources: ["ds-1"], pii_processor: false, conflicts: [], via: {} };
}

afterEach(() => vi.restoreAllMocks());

describe("what the SME sends", () => {
  it("starts a code's value meanings from the values in the data", () => {
    const draft = draftFrom(MEANING, ITEM.profile, false);
    expect(draft.meanings).toEqual([["1", ""], ["0", ""], ["2", ""], ["9", ""]]);
    expect(draftFrom(MEANING, ITEM.profile, true).meanings).toEqual([]); // personal: shapes only
    // few distinct values: listed whatever role Gaia gave the column
    expect(draftFrom({ ...MEANING, role: "dimension" }, ITEM.profile, false).meanings).toHaveLength(4);
    // many: only for a code or a flag
    const many = { ...ITEM.profile!, distinct: 5000 };
    expect(draftFrom({ ...MEANING, role: "measure" }, many, false).meanings).toEqual([]);
  });

  it("sends only what changed, and value meanings only when given", () => {
    const draft = draftFrom(MEANING, ITEM.profile, false);
    expect(decisionBody("accept", "", draft, MEANING)).toEqual({ action: "accept" });
    const edited = { ...draft, business_name: "Invoice status", meanings: [["2", "overdue"], ["0", " open "], ["1", ""]] as [string, string][] };
    expect(decisionBody("correct", " manual ", edited, MEANING)).toEqual({
      action: "correct", note: "manual", changes: { business_name: "Invoice status" },
      value_meanings: { "2": "overdue", "0": "open" },
    });
    expect(decisionBody("correct", "", draft, MEANING)).toEqual({ action: "correct" });
  });

  it("a table's correction carries only the changed fields", () => {
    const m = { version: 1, status: "draft", business_name: "Invoices", purpose: "p", grain: null, entity: null, synonyms: ["bills"],
                assumptions: [], unknowns: [], score: 0.8, reviewed_by: null, reviewed_at: null, review_note: null };
    expect(tableChanges(m, { business_name: "Invoices", purpose: "p", grain: "one row per invoice", entity: "", synonyms: "bills" }))
      .toEqual({ grain: "one row per invoice" });
  });
});

describe("the review screens", () => {
  it("an SME sees Review in the navigation; a user does not", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("[]"));
    const { unmount } = render(<MemoryRouter><Shell me={me(["user", "sme"])} onSignedOut={vi.fn()} /></MemoryRouter>);
    expect(screen.getAllByRole("link").map((a) => a.textContent)).toEqual(["Home", "Review"]);
    unmount();
    render(<MemoryRouter initialEntries={["/review"]}><Shell me={me(["user"])} onSignedOut={vi.fn()} /></MemoryRouter>);
    expect(screen.getByText("Your role does not include this page.")).toBeInTheDocument();
  });

  it("lists the queue with why each item is there, and switches to the decided ones", async () => {
    const asked: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      asked.push(String(input));
      return new Response(JSON.stringify({ total: 1, items: [{
        id: "r1", table_id: "t1", table: "finance.invoices", column_id: "c1", column: "status", data_type: "smallint",
        business_name: "Status", role: "code", score: 0.31, band: "abstain", status: "draft", reasons: ["abstain", "contradiction"],
        priority: 0.6, coverage: "targeted", reviewed_by: null, reviewed_at: null, resolved_at: null }] }));
    });
    render(<MemoryRouter initialEntries={["/review/demo_pg"]}><Routes><Route path="/review/:ds" element={<ReviewQueue />} /></Routes></MemoryRouter>);
    const link = await screen.findByRole("link", { name: /finance.invoices.*status/ });
    expect(link).toHaveAttribute("href", "/review/demo_pg/columns/c1");
    expect(screen.getByText("Gaia is not sure", { selector: ".chip" })).toBeInTheDocument();
    expect(screen.getByText("evidence contradicts", { selector: ".chip" })).toBeInTheDocument();
    expect(asked[0]).toBe("/api/review/demo_pg/queue?status=open&limit=50&offset=0");
    await userEvent.click(screen.getByRole("tab", { name: "Decided" }));
    await waitFor(() => expect(asked.at(-1)).toContain("status=done"));
  });

  it("shows the summary and records a correction with the codes' meanings", async () => {
    const posted: unknown[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (init?.method === "POST") {
        posted.push(JSON.parse(String(init.body)));
        return new Response(JSON.stringify({
          meaning: { ...MEANING, version: 3, status: "sme_reviewed", band: "accepted", score: 1, reviewed_by: "akaya", value_meanings: { "2": "overdue" } },
          propagated: { views: [], done: true }, requeued: ["rpt.v_invoices.state"],
          next: { column_id: "c2", table: "finance.payments", column: "method" } }));
      }
      if (path.startsWith("/api/review/demo_pg/columns/c1")) return new Response(JSON.stringify(ITEM));
      return new Response("{}", { status: 404 });
    });
    render(<MemoryRouter initialEntries={["/review/demo_pg/columns/c1"]}><Routes><Route path="/review/:ds/columns/:id" element={<ColumnPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("Gaia reads it as “Status” (code): The status of the invoice.")).toBeInTheDocument();
    expect(screen.getByText("Certainty 0.31 (not sure), from 2 piece(s) of evidence for and 0 against.")).toBeInTheDocument();
    const form = screen.getByRole("form", { name: "Your decision" });
    await userEvent.click(within(form).getByRole("radio", { name: /Correct/ }));
    await userEvent.type(within(form).getByLabelText("Meaning of 2"), "overdue");
    await userEvent.click(within(form).getByRole("button", { name: "Correct and save" }));
    expect(await screen.findByText("Saved as version 3.")).toBeInTheDocument();
    expect(posted).toEqual([{ action: "correct", value_meanings: { "2": "overdue" } }]);
    expect(screen.getByText("Computed from this column, queued for a look: rpt.v_invoices.state.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next: finance.payments.method" })).toBeInTheDocument();
  });

  it("a view column that shows its base as it is cannot be reviewed itself", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({
      ...ITEM, reviewable: false, meaning: { ...MEANING, inherited_from: "legacy.tb_trx02.f2" } })));
    render(<MemoryRouter initialEntries={["/review/demo_pg/columns/c1"]}><Routes><Route path="/review/:ds/columns/:id" element={<ColumnPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByText(/shows legacy.tb_trx02.f2 as it is/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Your decision" })).toBeNull();
  });

  it("every table is reachable, queued or not, and can be found by name", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify([
      { id: "t1", name: "finance.invoices", kind: "table", business_name: "Invoices", reviewed_by: null, columns: 9, queued: 0, decided: 0 },
      { id: "t2", name: "crm.customers", kind: "table", business_name: "Customers", reviewed_by: null, columns: 17, queued: 3, decided: 1 },
    ])));
    render(<MemoryRouter initialEntries={["/review/demo_pg/tables"]}><Routes><Route path="/review/:ds/tables" element={<ReviewTables />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("link", { name: "finance.invoices" })).toHaveAttribute("href", "/review/demo_pg/tables/t1");
    await userEvent.type(screen.getByLabelText("Find"), "invoice");
    expect(screen.queryByRole("link", { name: "crm.customers" })).toBeNull();
    expect(screen.getByRole("link", { name: "finance.invoices" })).toBeInTheDocument();
  });
});
