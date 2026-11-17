// The D80 run's finding (OWNER_VERIFICATION §28, D81): "Show the SQL" only where the person may
// see it — an SME on the data sources they review, not everywhere.
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { Me } from "../api/client";
import { ChatPage } from "../pages/chat/Chat";

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
}

const akaya: Me = { login: "akaya", display_name: "A. K.", email: null, local: false, directory: "acme", roles: ["user", "sme"],
                    sme_sources: ["ds-1"], pii_processor: false, conflicts: [], via: {}, category_names: ["Sales"] };

const ANSWER = { status: "answered", narrative: "Here it is.", columns: ["n"], rows: [[3]], personal_columns: [], masked: false,
                 interpretation: "x", assumptions: [], uncertain: [], confidence: null, clarification: null, reason: null,
                 query_run_id: "q-1" };

function world(sql: boolean) {
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
    const url = String(input);
    if (url.includes("/api/data-sources")) {
      return json([{ name: "demo_mssql", dialect: "mssql", origin: "config", status: "config", introspected: true,
                     usable: true, default: true, readable_objects: 30, sql }]);
    }
    if (url.endsWith("/api/conversations/c1")) {
      return json({ id: "c1", data_source: "demo_mssql", title: "t", messages: [
        { id: "m1", seq: 1, role: "user", kind: "question", content: "How many?", payload: {}, created_at: "2026-11-17T21:00:00Z" },
        { id: "m2", seq: 2, role: "assistant", kind: "answer", content: "Here it is.", payload: ANSWER, created_at: "2026-11-17T21:00:01Z" },
      ] });
    }
    return json([]);
  });
  render(<MemoryRouter initialEntries={["/chat/c1"]}><Routes><Route path="/chat/:id" element={<ChatPage me={akaya} />} /></Routes></MemoryRouter>);
}

afterEach(() => vi.restoreAllMocks());

describe("the SQL of an answer", () => {
  it("is not offered to an SME on a data source they do not review", async () => {
    world(false);
    expect(await screen.findByText("Here it is.")).toBeInTheDocument();
    expect(screen.queryByText("Show the SQL")).not.toBeInTheDocument();
  });

  it("is offered where they review", async () => {
    world(true);
    expect(await screen.findByText("Show the SQL")).toHaveAttribute("href", "/queries/q-1");
  });
});

describe("the Evaluation page (6A)", () => {
  it("shows the pooled report, the comparison's meanings warning and the runs, replays marked", async () => {
    const { Evaluation } = await import("../pages/evaluation/Evaluation");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/data-sources")) return json(["demo_pg"]);
      if (url.includes("/runs")) {
        return json([
          { id: "7d6902f5aaaa", started_at: "2026-11-17T21:22:00Z", build: "D80", model: "m", mode: "fresh", as_login: null,
            measured: true, set_key: "S", meanings_key: "abc123def456", sme_decisions: 7, correct: 29, scored: 30 },
          { id: "d64c6edfbbbb", started_at: "2026-11-17T20:47:00Z", build: "D80", model: "m", mode: "replay", as_login: null,
            measured: false, set_key: "S", meanings_key: null, sme_decisions: null, correct: 0, scored: 4 },
        ]);
      }
      return json({ data_source: "demo_pg", question_set_key: "S", runs: [], excluded: [], markdown: "",
                    correct: { k: 86, n: 90, interval: [0.89, 0.98] }, executable: { k: 90, n: 90, interval: [0.96, 1] },
                    per_run: [0.93, 0.97, 0.97], questions: { always: ["g01"], flaky: [["o03", 2, 3]], never: ["o06"],
                    never_reasons: { o06: "no column of the result holds the values of 'profit'" } },
                    clarification: { asked: [32, 33], over: [0, 63], agree: [7, 26] },
                    comparison: { difference: 0.075, low: -0.008, high: 0.183, questions: 30, verdict: "passes",
                                  moved: [["g07", 0, 1]], meanings: "the meanings differ: not the build's alone" } });
    });
    render(<MemoryRouter><Evaluation /></MemoryRouter>);
    expect(await screen.findByText(/86 of 90 = 96%/)).toBeInTheDocument();
    expect(screen.getByText("(95% interval 89%–98%)")).toBeInTheDocument();
    expect(screen.getByText(/o03 2\/3/)).toBeInTheDocument();
    expect(screen.getByText("The candidate passes.")).toBeInTheDocument();
    expect(screen.getByText("the meanings differ: not the build's alone")).toBeInTheDocument();
    expect(await screen.findByText("replayed — not counted")).toBeInTheDocument();
    expect(screen.getByText("7 SME decision(s)")).toBeInTheDocument();
  });
});
