import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { type Me, parseEvents, type StreamEvent } from "../api/client";
import { AssistantTurn, formatCell } from "../components/chat";
import { Categories } from "../pages/admin/Categories";
import { applyEvent, ChatPage } from "../pages/chat/Chat";

function me(roles: Me["roles"]): Me {
  return { login: "berk", display_name: "Berk", email: null, local: false, directory: "acme", roles,
           sme_sources: [], pii_processor: false, conflicts: [], via: {}, category_names: ["Sales"] };
}

function sse(events: [string, object][]): string {
  return events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join(": keep-alive\n\n");
}

const ANSWER = {
  question: "Top cities?", status: "answered", narrative: "Istanbul sold the most.", columns: ["city", "total"],
  rows: [["Istanbul", 1234567.5], ["Ankara", 99]], row_cap_reached: false, personal_columns: [], masked: false,
  interpretation: "Top cities by order amount", assumptions: ["cancelled orders left out"], uncertain: [],
  confidence: { level: "high", score: 0.91, reasons: ["the query needed 1 repair(s)"] }, clarification: null,
  reason: null, notes: [], elapsed_ms: 10, query_run_id: "q-1", message_id: "m-2", seq: 2,
};

afterEach(() => vi.restoreAllMocks());

describe("the chat's stream", () => {
  it("parses server-sent events as they arrive, keep-alives skipped", () => {
    const seen: StreamEvent[] = [];
    const text = sse([["message", { id: "m-1" }], ["status", { stage: "generation" }]]);
    const rest = parseEvents(text.slice(0, 30), (e) => seen.push(e));
    expect(seen).toEqual([]);
    parseEvents(rest + text.slice(30) + "event: done\ndata: {}", (e) => seen.push(e));
    expect(seen.map((e) => e.event)).toEqual(["message", "status"]);
    parseEvents("event: done\ndata: {}\n\n", (e) => seen.push(e));
    expect(seen.at(-1)).toEqual({ event: "done", data: {} });
  });

  it("puts the table on screen before the narrative arrives", () => {
    let state = applyEvent([], null, { event: "message", data: { id: "m-1", role: "user", content: "Top cities?" } });
    state = applyEvent(state.messages, state.pending, { event: "status", data: { stage: "running" } });
    state = applyEvent(state.messages, state.pending, { event: "result", data: { columns: ["city"], rows: [["Istanbul"]] } });
    expect(state.pending).toEqual({ stage: "running", result: { columns: ["city"], rows: [["Istanbul"]] } });
    state = applyEvent(state.messages, state.pending, { event: "answer", data: ANSWER });
    expect(state.pending).toBeNull();
    expect(state.messages.map((m) => [m.role, m.kind])).toEqual([["user", undefined], ["assistant", "answer"]]);
  });

  it("formats numbers and dates for people", () => {
    expect(formatCell(1234567.5)).toBe("1,234,567.5");
    expect(formatCell(42)).toBe("42");
    expect(formatCell("2025-12-01")).toBe("Dec 1, 2025");
    expect(formatCell(null)).toBe("");
  });
});

describe("an answer", () => {
  it("shows the confidence as a level with its reasons, never a percentage", async () => {
    render(<MemoryRouter><AssistantTurn kind="answer" doc={ANSWER as never} canSeeSql={false} /></MemoryRouter>);
    expect(screen.getByText("Confidence: high")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/91|0\.91|%/);
    await userEvent.click(screen.getByRole("button", { name: "Confidence: high" }));
    expect(screen.getByText("the query needed 1 repair(s)")).toBeInTheDocument();
    expect(screen.queryByText("Show the SQL")).toBeNull(); // P4-2: not for users
  });

  it("masks personal data and offers the SQL to SMEs and administrators", () => {
    const doc = { ...ANSWER, columns: ["customer", "email"], rows: [["C1", "[hidden]"]], personal_columns: [1], masked: true };
    render(<MemoryRouter><AssistantTurn kind="answer" doc={doc as never} canSeeSql /></MemoryRouter>);
    expect(screen.getByTitle("personal data: masked for you")).toHaveTextContent("•••");
    expect(screen.getByText(/masked for you \(it needs the PII-processor permission\)/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Show the SQL" })).toHaveAttribute("href", "/queries/q-1");
  });

  it("a refusal says that the role does not cover it, and nothing more", () => {
    render(<MemoryRouter><AssistantTurn kind="refused" doc={{ reason: "This needs data your role does not cover." }} canSeeSql={false} /></MemoryRouter>);
    expect(screen.getByText("Not allowed:")).toBeInTheDocument();
  });
});

describe("the chat screen", () => {
  it("starts a conversation, streams the turn, and answers a clarification by its option", async () => {
    const posted: unknown[] = [];
    let turn = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.startsWith("/api/data-sources")) return new Response(JSON.stringify([{ name: "demo_pg", dialect: "postgres", origin: "config", status: "config", introspected: true, usable: true, default: true, readable_objects: 12 }]));
      if (path.startsWith("/api/conversations?")) return new Response("[]");
      if (path === "/api/conversations" && init?.method === "POST") return new Response(JSON.stringify({ id: "c-1", data_source: "demo_pg", title: null }), { status: 201 });
      if (path === "/api/conversations/c-1/messages") {
        posted.push(JSON.parse(String(init!.body)));
        turn += 1;
        if (turn === 1) {
          return new Response(sse([
            ["message", { id: "m-1", seq: 1, role: "user", kind: "question", content: "Who are our best customers?" }],
            ["status", { stage: "generation" }],
            ["clarification", { ...ANSWER, status: "clarify", columns: [], rows: [], narrative: null,
              clarification: { id: "k-1", question: "Best by what?", options: ["number of orders", "order amount"] }, message_id: "m-2", seq: 2 }],
            ["done", {}]]));
        }
        return new Response(sse([
          ["message", { id: "m-3", seq: 3, role: "user", kind: "clarification_answer", content: "2" }],
          ["result", { columns: ["city", "total"], rows: [["Istanbul", 1234567.5]] }],
          ["answer", { ...ANSWER, message_id: "m-4", seq: 4 }],
          ["done", {}]]));
      }
      return new Response("{}", { status: 404 });
    });
    render(<MemoryRouter initialEntries={["/chat"]}><Routes>
      <Route path="/chat" element={<ChatPage me={me(["user"])} />} />
      <Route path="/chat/:id" element={<ChatPage me={me(["user"])} />} />
    </Routes></MemoryRouter>);
    const box = await screen.findByLabelText("Your question");
    await waitFor(() => expect(screen.getByRole("button", { name: "Send" })).toBeDisabled());
    await userEvent.type(box, "Who are our best customers?");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Best by what?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "order amount" }));
    expect(await screen.findByText("Istanbul sold the most.")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Result" })).getByText("1,234,567.5")).toBeInTheDocument();
    expect(posted).toEqual([{ content: "Who are our best customers?" }, { content: "2" }]);
  });

  it("says so when the person's role covers no data", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("[]"));
    render(<MemoryRouter><ChatPage me={me(["user"])} /></MemoryRouter>);
    expect(await screen.findByText(/Your role covers no data yet/)).toBeInTheDocument();
  });
});

describe("the data categories page", () => {
  it("warns about objects nobody can read and places one", async () => {
    const puts: [string, unknown][] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const path = String(input);
      if (init?.method === "PUT") {
        puts.push([path, JSON.parse(String(init.body))]);
        return new Response("{}");
      }
      if (path.startsWith("/api/admin/categories")) return new Response(JSON.stringify([{ id: "k1", name: "Finance", description: null, keywords: ["finance"], created_by: "x", created_at: "2026-01-01", retired_at: null, objects: 1, grants: 0 }]));
      if (path.startsWith("/api/admin/data-sources/demo_pg/objects")) return new Response(JSON.stringify([
        { id: "t1", name: "finance.invoices", kind: "table", business_name: "Invoices", category: { id: "k1", name: "Finance", status: "proposed", source: "gaia", decided_by: null, decided_at: "2026-01-01", reason: "matches finance" } },
        { id: "t2", name: "hr.employees", kind: "table", business_name: "Employees", category: null }]));
      if (path.startsWith("/api/admin/data-sources")) return new Response(JSON.stringify([{ name: "demo_pg", dialect: "postgres", origin: "config", status: "config" }]));
      return new Response("[]");
    });
    render(<MemoryRouter><Categories /></MemoryRouter>);
    expect(await screen.findByText("1 object(s) have no category: nobody can read them.")).toBeInTheDocument();
    expect(screen.getByText("proposed by Gaia: matches finance")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Category of hr.employees"), "Finance");
    await waitFor(() => expect(puts).toEqual([["/api/admin/data-sources/demo_pg/objects/t2/category", { category: "Finance" }]]));
  });
});
