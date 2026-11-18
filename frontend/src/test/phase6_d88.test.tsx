// Build D84–D88 (OWNER_VERIFICATION §29): "Was this right?" under an answer (6D, P6-4), the
// SME's doubted answers, P(right) for SMEs/admins/auditors once calibrated (P6-2), the
// "uncalibrated" mark (6C), and the Evaluation page's new sections (6G included).
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import type { Me } from "../api/client";
import { ConfidenceBadge } from "../components/chat";
import { ChatPage } from "../pages/chat/Chat";
import { Band } from "../pages/review/common";

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

const finn: Me = { login: "finn", display_name: null, email: null, local: false, directory: "acme", roles: ["user"],
                   sme_sources: [], pii_processor: false, conflicts: [], via: {}, category_names: ["Sales"] };

const ANSWER = { status: "answered", narrative: "Istanbul sold the most.", columns: ["city"], rows: [["Istanbul"]],
                 personal_columns: [], masked: false, interpretation: "x", assumptions: [], uncertain: [],
                 confidence: { level: "medium", score: 0.71, reasons: [] }, clarification: null, reason: null };

afterEach(() => vi.restoreAllMocks());

describe("Was this right? (6D)", () => {
  function world(feedback: unknown = null) {
    const calls: { url: string; method: string; body: unknown }[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      calls.push({ url, method, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      if (url.includes("/api/data-sources")) {
        return json([{ name: "demo_pg", dialect: "postgres", origin: "config", status: "config", introspected: true,
                       usable: true, default: true, readable_objects: 30, sql: false }]);
      }
      if (url.endsWith("/api/conversations/c1")) {
        return json({ id: "c1", data_source: "demo_pg", title: "t", messages: [
          { id: "m1", seq: 1, role: "user", kind: "question", content: "Which city?", payload: {}, created_at: "2026-11-18T09:00:00Z" },
          { id: "m2", seq: 2, role: "assistant", kind: "answer", content: "Istanbul sold the most.", payload: ANSWER,
            created_at: "2026-11-18T09:00:01Z", feedback },
        ] });
      }
      if (url.endsWith("/api/messages/m2/feedback") && method === "PUT") {
        const body = JSON.parse(String(init?.body));
        return json({ verdict: body.verdict, note: body.note, status: body.verdict === "right" ? "none" : "open",
                      decision: null, cause: null, decision_note: null, decided_at: null, updated_at: "2026-11-18T09:01:00Z" });
      }
      return json([]);
    });
    render(<MemoryRouter initialEntries={["/chat/c1"]}><Routes><Route path="/chat/:id" element={<ChatPage me={finn} />} /></Routes></MemoryRouter>);
    return calls;
  }

  it("counts a yes at once", async () => {
    const calls = world();
    fireEvent.click(await screen.findByRole("button", { name: "Yes" }));
    expect(await screen.findByText("You said this was right.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ verdict: "right", note: null });
  });

  it("sends a no at once, and the note after it (D92)", async () => {
    // on the owner's host "Partly" and "No" only opened a form, and two verdicts never reached
    // the expert: the click itself now sends, the note is optional and follows
    const calls = world();
    fireEvent.click(await screen.findByRole("button", { name: "No" }));
    expect(await screen.findByText(/waiting for the data source's expert/)).toBeInTheDocument();
    const puts = () => calls.filter((c) => c.method === "PUT").map((c) => c.body);
    expect(puts()).toEqual([{ verdict: "wrong", note: null }]);
    fireEvent.change(screen.getByLabelText(/What was not right/), { target: { value: "Ankara is missing" } });
    fireEvent.click(screen.getByRole("button", { name: "Save the note" }));
    expect(await screen.findByText(/“Ankara is missing”/)).toBeInTheDocument();
    expect(puts()).toEqual([{ verdict: "wrong", note: null }, { verdict: "wrong", note: "Ankara is missing" }]);
    // the note can be changed later, the verdict stays with the expert
    fireEvent.click(screen.getByRole("button", { name: "change the note" }));
    expect(screen.getByLabelText(/What was not right/)).toHaveValue("Ankara is missing");
  });

  it("sends a partly at once too, and a change of mind", async () => {
    const calls = world();
    fireEvent.click(await screen.findByRole("button", { name: "Partly" }));
    expect(await screen.findByText("You said this was partly right.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "change" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes" }));
    expect(await screen.findByText("You said this was right.")).toBeInTheDocument();
    expect(calls.filter((c) => c.method === "PUT").map((c) => (c.body as { verdict: string }).verdict))
      .toEqual(["partly", "right"]);
  });

  it("shows the expert's decision", async () => {
    world({ verdict: "wrong", note: "x", status: "decided", decision: "wrong", cause: "meaning",
            decision_note: "store city, not customer city", decided_at: "2026-11-18T10:00:00Z", updated_at: "2026-11-18T10:00:00Z" });
    expect(await screen.findByText(/the expert decided: it was wrong/)).toBeInTheDocument();
    expect(screen.getByText(/a column was read wrongly/)).toBeInTheDocument();
    expect(screen.getByText("(store city, not customer city)")).toBeInTheDocument();
  });

  it("shows no percentage to a person who only chats", async () => {
    world();
    expect(await screen.findByText("Confidence: medium")).toBeInTheDocument();
    expect(screen.queryByText(/uncalibrated/)).not.toBeInTheDocument();
  });
});

describe("P(right) and the uncalibrated mark (6C, 6D)", () => {
  it("shows the probability to SMEs, administrators and auditors once calibrated", () => {
    render(<ConfidenceBadge showProbability confidence={{ level: "medium", score: 0.71, reasons: [], probability: 0.8, lower: 0.74 }} />);
    expect(screen.getByText(/right 80% of the time \(at least 74%\)/)).toBeInTheDocument();
  });

  it("says uncalibrated when it is not", () => {
    render(<ConfidenceBadge showProbability confidence={{ level: "high", score: 0.9, reasons: [] }} />);
    expect(screen.getByText(/uncalibrated/)).toBeInTheDocument();
  });

  it("marks a meaning's score in review", () => {
    const { rerender } = render(<Band band="deep_profile" score={0.62} mark />);
    expect(screen.getByText(/0\.62/)).toBeInTheDocument();
    expect(screen.getByText("(uncalibrated)")).toBeInTheDocument();
    rerender(<Band band="accepted" score={0.93} calibrated lower={0.85} mark />);
    expect(screen.getByText(/right 93% of the time \(at least 85%\)/)).toBeInTheDocument();
    expect(screen.queryByText("(uncalibrated)")).not.toBeInTheDocument();
  });
});

describe("the SME's doubted answers (6D)", () => {
  it("lists them and decides one, with a cause", async () => {
    const { ReviewAnswers } = await import("../pages/review/Answers");
    const posts: unknown[] = [];
    let decided = false;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        decided = true;
        return json({});
      }
      if (url.includes("/feedback")) {
        return json({ total: decided ? 0 : 1, items: decided ? [] : [{
          id: "f1", message_id: "m2", status: "open", verdict: "wrong", note: "Ankara is missing", given_by: "finn",
          created_at: "2026-11-18T09:01:00Z", question: "Which city sold the most?", narrative: "Istanbul sold the most.",
          interpretation: "Revenue per customer city", assumptions: [], uncertain: [], confidence: { level: "medium", score: 0.71, reasons: [] },
          columns: ["city"], rows: [["Istanbul"]], row_count: 1, masked_columns: [], sql: "SELECT city FROM x", model: "m",
          answered_at: "2026-11-18T09:00:01Z", decision: null, cause: null, decision_note: null, decided_by: null, decided_at: null }] });
      }
      return json([]);
    });
    render(<MemoryRouter initialEntries={["/review/demo_pg/answers"]}><Routes><Route path="/review/:ds/answers" element={<ReviewAnswers />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("Which city sold the most?")).toBeInTheDocument();
    expect(screen.getByText("Ankara is missing")).toBeInTheDocument();
    expect(screen.getByText("SELECT city FROM x")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Decide" });
    expect(submit).toBeDisabled();
    fireEvent.click(screen.getByLabelText("wrong"));
    expect(submit).toBeDisabled(); // why, first
    fireEvent.change(screen.getByLabelText("Why"), { target: { value: "meaning" } });
    fireEvent.click(submit);
    await waitFor(() => expect(posts).toEqual([{ decision: "wrong", cause: "meaning", note: null }]));
    expect(await screen.findByText("No doubted answer is waiting.")).toBeInTheDocument();
  });
});

describe("the Evaluation page (D84–D88)", () => {
  it("shows why answers were wrong, the levels against the reference, and calibration", async () => {
    const { Evaluation } = await import("../pages/evaluation/Evaluation");
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/data-sources")) return json(["demo_pg"]);
      if (url.includes("/runs")) return json([]);
      return json({ data_source: "demo_pg", question_set_key: "S", runs: [], excluded: [], markdown: "",
                    correct: { k: 86, n: 90, interval: [0.89, 0.98] }, executable: { k: 90, n: 90, interval: [0.96, 1] },
                    questions: { always: [], flaky: [], never: ["o06"], never_reasons: { o06: "profit differs" },
                                 never_detail: { o06: { objects: ["sales.order_lines"], missing: ["sales.orders"],
                                                        clarification: ["How?", "not cancelled"], interpretation: "Profit over all orders" } } },
                    wrong_causes: { wrong: 4, retrieval: 3, reading: 1, unknown: 0, retrieval_questions: ["o06"], reading_questions: ["o02"] },
                    confidence_levels: { high: [60, 62], low: [2, 4] },
                    labels: { n: 13, counts: { right: 12, partly: 0, wrong: 1 }, auroc: 0.17, ranking_ready: false, judge: null },
                    calibration: [{ kind: "meaning", model: "deepseek-flash", scoring: "heuristic-v2", in_use: null, method: "identity",
                                    why: "13 label(s), 1 not right: too few to calibrate", labels: 13, wrong: 1, version: "identity",
                                    improves: false, identity: { n: 13, ece: 0.3, brier: 0.2, reliability: [{ from: 0.6, to: 0.7, n: 6, predicted: 0.62, observed: 1 }] },
                                    held_out: null }],
                    categories: { rule: { n: 28, agree: 24, none: 1, by_basis: { direct: [20, 21], related: [4, 6] },
                                          misses: [{ object: "sales.payments", proposed: "Sales", confirmed: "Finance", basis: "direct", why: "x" }] },
                                  seen: { n: 28, kept: 25, no_proposal: 0 } } });
    });
    render(<MemoryRouter><Evaluation /></MemoryRouter>);
    expect(await screen.findByText(/the context lacked sales.orders \(retrieval\)/)).toBeInTheDocument();
    expect(screen.getByText(/answered after: “not cancelled”/)).toBeInTheDocument();
    expect(screen.getByText(/3 of 4: the context lacked an object/)).toBeInTheDocument();
    expect(screen.getByText(/60 of 62 = 97%/)).toBeInTheDocument();
    expect(screen.getByText(/too few right or not-right labels/)).toBeInTheDocument();
    expect(screen.getByText(/too few to calibrate/)).toBeInTheDocument();
    expect(screen.getByText(/the identity \(uncalibrated\)/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "reliability of deepseek-flash" })).toBeInTheDocument();
    expect(screen.getByText(/proposes 27 of them — 24 as people confirmed/)).toBeInTheDocument();
    expect(screen.getByText(/wrong: sales.payments — proposed Sales, people said Finance/)).toBeInTheDocument();
  });
});
