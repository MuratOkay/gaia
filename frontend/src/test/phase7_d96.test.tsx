import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { ReviewQueue } from "../pages/review/Queue";
import { ReviewSources } from "../pages/review/Sources";

afterEach(() => vi.restoreAllMocks());

describe("the calibration sample (7A, D96)", () => {
  it("tells the SME how many of the week's random checks wait", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify([
      { name: "demo_pg", id: "ds-1", dialect: "postgres", open: 120, reviewed: 13, stale: 0,
        feedback_open: 0, sample: { this_week: 10, open: 9 } },
      { name: "demo_mssql", id: "ds-2", dialect: "mssql", open: 3, reviewed: 0, stale: 0,
        feedback_open: 0, sample: { this_week: 0, open: 0 } },
    ])));
    render(<MemoryRouter><ReviewSources /></MemoryRouter>);
    const line = await screen.findByText(/9 of this week's random checks waiting/);
    expect(line.closest("a")).toHaveAttribute("href", "/review/demo_pg");
    expect(screen.getAllByText(/random checks waiting/)).toHaveLength(1);
  });

  it("shows a drawn item as drawn at random, with no certainty", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({
      total: 1, items: [{
        id: "r9", table_id: "t1", table: "sales.orders", column_id: "c9", column: "total_amount",
        data_type: "numeric", business_name: "Order total", role: "measure", score: null, band: null,
        status: "draft", reasons: ["calibration_sample"], priority: null, coverage: "targeted",
        reviewed_by: null, reviewed_at: null, resolved_at: null, blind: true }] })));
    render(<MemoryRouter initialEntries={["/review/demo_pg"]}><Routes><Route path="/review/:ds" element={<ReviewQueue />} /></Routes></MemoryRouter>);
    expect(await screen.findByText("drawn at random to measure Gaia", { selector: ".chip" })).toBeInTheDocument();
    expect(screen.queryByText(/0\.\d\d/)).not.toBeInTheDocument();
    // the reason can be chosen as a filter
    expect(screen.getByRole("option", { name: "drawn at random to measure Gaia" })).toBeInTheDocument();
  });
});
