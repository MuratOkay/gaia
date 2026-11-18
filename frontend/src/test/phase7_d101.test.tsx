import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";

import type { ModelsView } from "../api/client";
import { Models } from "../pages/models/Models";

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

const FUTURE = new Date(Date.now() + 20 * 86400_000).toISOString();

const VIEW: ModelsView = {
  models: [
    { name: "deepseek-flash", origin: "config", base_url: "https://api.deepseek.com", model_id: "deepseek-flash", locality: "remote",
      status: "config", options: {}, key_env: null, has_key: true, probe: null, probed_at: null, created_by: null,
      serves: ["reasoning"], usable: true },
    { name: "qwen", origin: "admin", base_url: "https://gw.example/v1", model_id: "qwen-max", locality: "remote",
      status: "verified", options: {}, key_env: "QWEN_KEY", has_key: true,
      probe: [{ name: "plain", ok: true, detail: "'ready' from qwen-max" }, { name: "json", ok: true, detail: "business_name='Order total'" }],
      probed_at: "2026-11-18T09:00:00Z", created_by: "dyilmaz", serves: ["answering"], usable: true },
    { name: "llama", origin: "admin", base_url: "http://localhost:11434/v1", model_id: "llama3", locality: "local",
      status: "unverified", options: {}, key_env: null, has_key: true, probe: null, probed_at: null, created_by: "dyilmaz",
      serves: [], usable: false },
  ],
  purposes: ["reasoning", "answering"],
  serving: { reasoning: "deepseek-flash", answering: "qwen" },
  assignments: {
    answering: { purpose: "answering", model: "qwen", previous: "deepseek-flash", since: "2026-11-18T10:00:00Z",
                 rollback_until: FUTURE, forced: false, by: "dyilmaz", note: "cheaper", trial_job_id: "j1" },
  },
  history: [{ purpose: "answering", model: "qwen", previous: "deepseek-flash", since: "2026-11-18T10:00:00Z",
              rollback_until: FUTURE, forced: false, by: "dyilmaz", note: "cheaper", trial_job_id: "j1" }],
  trials: [{
    id: "j1", kind: "model_trial", data_source: "demo_pg", params: { model: "qwen", purpose: "both" }, state: "done",
    requested_by: "dyilmaz", schedule_id: null, worker: "host:1", progress: {}, result: {}, error: null, tokens: 90000,
    calls: 120, cancel_requested_at: null, cancel_requested_by: null, created_at: "2026-11-18T09:10:00Z",
    started_at: "2026-11-18T09:10:05Z", heartbeat_at: null, finished_at: "2026-11-18T09:40:00Z",
    lines: ["trial of qwen (qwen-max, remote) — both", "  answering against deepseek-flash: **cannot tell**, +1.5 points"],
  }],
  data_sources: ["demo_mssql", "demo_pg"],
  meanings: { demo_pg: { "deepseek-flash": 180, "SME-decided": 12 } },
  rollback_days: 30,
  trial_valid_days: 14,
};

function backend(posted: { url: string; method: string; body: unknown }[]) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    if (init?.method && init.method !== "GET") {
      posted.push({ url, method: init.method, body: init.body ? JSON.parse(String(init.body)) : null });
      if (url === "/api/models/switch") return json({ detail: "no trial of llama for answering in the last 14 days" }, 400);
      return json({});
    }
    return json(VIEW);
  });
}

afterEach(() => vi.restoreAllMocks());

describe("models (7D, D101)", () => {
  it("an administrator sees what serves, probes, tries, switches, rolls back and registers", async () => {
    const posted: { url: string; method: string; body: unknown }[] = [];
    backend(posted);
    render(<MemoryRouter><Models admin /></MemoryRouter>);
    const page = await screen.findByRole("region", { name: "Models" });
    const serving = (await within(page).findAllByRole("table"))[0];
    const rows = within(serving).getAllByRole("row");
    expect(rows[1]).toHaveTextContent(/reasoning.*deepseek-flash.*from configuration/);
    expect(rows[2]).toHaveTextContent(/answering.*qwen.*dyilmaz.*deepseek-flash.*rollback possible until.*cheaper/);
    expect(screen.getByText("environment variable QWEN_KEY")).toBeInTheDocument();
    expect(screen.getByText("none needed")).toBeInTheDocument();  // a local server
    expect(screen.getByText(/answering against deepseek-flash/)).toBeInTheDocument();
    expect(screen.getByText("deepseek-flash 180 · SME-decided 12")).toBeInTheDocument();

    await userEvent.click(within(rows[2]).getByRole("button", { name: "Roll back to deepseek-flash" }));
    await waitFor(() => expect(posted[0]).toEqual({ url: "/api/models/rollback", method: "POST", body: { purpose: "answering" } }));

    const registered = within(page).getAllByRole("table")[1];
    const llama = within(registered).getAllByRole("row")[3];
    await userEvent.click(within(llama).getByRole("button", { name: "Probe" }));
    await waitFor(() => expect(posted[1].url).toBe("/api/models/llama/probe"));
    // the model serving answering cannot be retired from here; the others can
    expect(within(within(registered).getAllByRole("row")[2]).queryByRole("button", { name: "Retire" })).toBeNull();

    const change = screen.getByRole("region", { name: "Try and switch" });
    const [model, source, trialFor] = within(change).getAllByRole("combobox");
    expect(within(model).getAllByRole("option").map((o) => o.textContent)).toEqual(["deepseek-flash", "qwen"]); // usable only
    await userEvent.selectOptions(source, "demo_pg");
    await userEvent.selectOptions(trialFor, "answering");
    await userEvent.click(within(change).getByRole("button", { name: "Start a trial" }));
    await waitFor(() => expect(posted[2]).toEqual({ url: "/api/models/qwen/trial", method: "POST", body: { data_source: "demo_pg", purpose: "answering" } }));
    expect(await screen.findByText("A trial of qwen is queued: the worker runs it.")).toBeInTheDocument();

    await userEvent.click(within(change).getByRole("checkbox"));
    await userEvent.type(within(change).getByRole("textbox"), "provider change");
    await userEvent.click(within(change).getByRole("button", { name: "Switch to qwen" }));
    await waitFor(() => expect(posted[3].body).toEqual({ purpose: "answering", model: "qwen", force: true, note: "provider change" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no trial of llama");

    const add = screen.getByRole("form", { name: "Register a model" });
    await userEvent.type(within(add).getByLabelText("Name"), "llama2");
    await userEvent.type(within(add).getByLabelText("Base URL"), "http://localhost:11434/v1");
    await userEvent.type(within(add).getByLabelText("Model id"), "llama3.1");
    await userEvent.selectOptions(within(add).getByLabelText("Locality"), "local");
    await userEvent.click(within(add).getByRole("button", { name: "Register" }));
    await waitFor(() => expect(posted[4]).toEqual({ url: "/api/models", method: "POST", body: {
      name: "llama2", base_url: "http://localhost:11434/v1", model_id: "llama3.1", key_env: null, key: null, locality: "local" } }));
  });

  it("an auditor reads without the buttons", async () => {
    backend([]);
    render(<MemoryRouter><Models /></MemoryRouter>);
    await screen.findByText("Serving now");
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.queryByRole("form", { name: "Register a model" })).toBeNull();
    expect(screen.getByText(/answering against deepseek-flash/)).toBeInTheDocument();
  });
});
