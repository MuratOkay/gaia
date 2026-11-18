import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import type { JobView } from "../api/client";
import { Alerts } from "../pages/jobs/Jobs";
import { Runs } from "../pages/queries/Queries";

function json(data: unknown): Response {
  return new Response(JSON.stringify(data), { headers: { "Content-Type": "application/json" } });
}

const RUNNING: JobView = {
  id: "j1", kind: "refresh", data_source: "demo_pg", params: {}, state: "running", requested_by: "schedule",
  schedule_id: "s1", worker: "host:1", progress: { step: "reason" }, result: null, error: null, tokens: 120000,
  calls: 40, cancel_requested_at: null, cancel_requested_by: null, created_at: "2026-11-18T02:00:00Z",
  started_at: "2026-11-18T02:00:05Z", heartbeat_at: "2026-11-18T02:10:00Z", finished_at: null,
};
const PARTIAL: JobView = { ...RUNNING, id: "j0", state: "partial", requested_by: "dyilmaz", progress: {},
  result: { stopped: "the token budget of 2,000,000 is spent" }, finished_at: "2026-11-17T03:00:00Z" };

function backend(posted: { url: string; body: unknown }[]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (init?.method && init.method !== "GET") {
      posted.push({ url, body: init.body ? JSON.parse(String(init.body)) : null });
      return json({});
    }
    if (url.startsWith("/api/jobs/schedules")) {
      return json([{ id: "s1", data_source: "demo_pg", kind: "refresh", when: "daily 02:00", params: {}, enabled: true,
                     next_due_at: "2026-11-19T02:00:00Z", last_queued_at: null, created_by: "dyilmaz", updated_at: "" }]);
    }
    if (url.startsWith("/api/jobs/j1")) {
      return json({ ...RUNNING, events: [{ seq: 1, at: "2026-11-18T02:00:05Z", level: "info", message: "introspect: 1 added" }] });
    }
    if (url.startsWith("/api/jobs")) return json({ jobs: [RUNNING, PARTIAL], data_sources: ["demo_mssql", "demo_pg"], kinds: ["refresh", "drift_watch", "sample"] });
    return json({ data_sources: [], totals: [], reasoning: [], profiling: [] });
  });
}

afterEach(() => vi.restoreAllMocks());

describe("jobs (7C, D98)", () => {
  it("an administrator sees what runs, starts, schedules and cancels", async () => {
    const posted: { url: string; body: unknown }[] = [];
    backend(posted);
    render(<MemoryRouter><Runs admin /></MemoryRouter>);
    const jobs = await screen.findByRole("region", { name: "Jobs" });
    const rows = await within(jobs).findAllByRole("row");
    expect(rows[1]).toHaveTextContent(/refresh.*running.*at reason.*the schedule/);
    expect(rows[2]).toHaveTextContent("stopped early");
    expect(rows[2]).toHaveTextContent("stopped: the token budget of 2,000,000 is spent — the next run takes up the rest");
    await userEvent.selectOptions(within(jobs).getAllByRole("combobox")[0], "drift_watch");
    await userEvent.click(within(jobs).getByRole("button", { name: "Start now" }));
    await waitFor(() => expect(posted[0]).toEqual({ url: "/api/jobs", body: { kind: "drift_watch", data_source: "demo_mssql" } }));
    await userEvent.click(within(jobs).getByRole("button", { name: "Schedule" }));
    await waitFor(() => expect(posted[1].body).toEqual({ data_source: "demo_mssql", kind: "drift_watch", when: "daily 02:00" }));
    await userEvent.click(within(rows[1]).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(posted[2].url).toBe("/api/jobs/j1/cancel"));
    expect(within(rows[2]).queryByRole("button", { name: "Cancel" })).toBeNull();
    await userEvent.click(within(jobs).getByRole("button", { name: "turn off" }));
    await waitFor(() => expect(posted[3]).toEqual({ url: "/api/jobs/schedules/s1/enabled", body: { enabled: false } }));
    await userEvent.click(within(rows[1]).getByRole("button", { name: "Log" }));
    expect(await screen.findByText("introspect: 1 added")).toBeInTheDocument();
  });

  it("an auditor reads without the buttons", async () => {
    backend([]);
    render(<MemoryRouter><Runs /></MemoryRouter>);
    const jobs = await screen.findByRole("region", { name: "Jobs" });
    await within(jobs).findAllByRole("row");
    expect(within(jobs).queryByRole("button", { name: "Start now" })).toBeNull();
    expect(within(jobs).queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(within(jobs).queryByRole("button", { name: "turn off" })).toBeNull();
  });

  it("shows what to look at and lets it be marked done", async () => {
    const posted: string[] = [];
    let open = true;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      if (init?.method === "POST") {
        posted.push(String(input));
        open = false;
        return json({});
      }
      return json(open ? [{ id: "a1", kind: "drift", data_source: "demo_pg", job_id: "j9", created_at: "2026-11-18T04:30:00Z",
                            message: "the golden set of demo_pg got worse: broken g01", detail: null,
                            acknowledged_at: null, acknowledged_by: null }] : []);
    });
    render(<MemoryRouter><Alerts /></MemoryRouter>);
    expect(await screen.findByText(/The golden set got worse/)).toBeInTheDocument();
    expect(screen.getByText(/broken g01/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() => expect(posted).toEqual(["/api/jobs/alerts/a1/ack"]));
    await waitFor(() => expect(screen.queryByText(/broken g01/)).toBeNull());
  });
});
