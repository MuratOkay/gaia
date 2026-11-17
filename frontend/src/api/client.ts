// The only way the UI talks to the API. Same origin (Vite proxies /api in development),
// so the session cookie travels by itself; every request that changes something repeats
// the CSRF token from the readable gaia_csrf cookie (Phase 5A, D68).

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

export const SIGNED_OUT_EVENT = "gaia:signed-out";
const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

export function csrfToken(cookie: string = document.cookie): string | undefined {
  for (const part of cookie.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === "gaia_csrf") return decodeURIComponent(rest.join("="));
  }
  return undefined;
}

type Options = { method?: string; body?: unknown; query?: Record<string, string | number | boolean | undefined> };

export function url(path: string, query?: Options["query"]): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query ?? {})) if (v !== undefined) params.set(k, String(v));
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

/** One server-sent event of the chat (5D): ``event:`` and its JSON ``data:``. */
export interface StreamEvent {
  event: string;
  data: Record<string, unknown>;
}

/** Parses a text/event-stream body as it arrives; comment lines (keep-alives) are skipped.
 *  Returns what is left over (an event not complete yet). */
export function parseEvents(buffer: string, emit: (e: StreamEvent) => void): string {
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (data.length) emit({ event, data: safeJson(data.join("\n")) as Record<string, unknown> });
  }
  return rest;
}

/** POST that answers with server-sent events (the chat's turn): each event as it comes. */
export async function stream(path: string, body: unknown, onEvent: (e: StreamEvent) => void,
                             signal?: AbortSignal): Promise<void> {
  const headers: Record<string, string> = { Accept: "text/event-stream", "Content-Type": "application/json" };
  const token = csrfToken();
  if (token) headers["X-CSRF-Token"] = token;
  const response = await fetch(path, { method: "POST", headers, credentials: "same-origin", body: JSON.stringify(body), signal });
  if (!response.ok || !response.body) {
    const text = await response.text();
    const data = text ? safeJson(text) : undefined;
    if (response.status === 401) window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
    const detail = typeof data === "object" && data && "detail" in data ? String((data as { detail: unknown }).detail) : response.statusText;
    throw new ApiError(response.status, detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer = parseEvents(buffer + decoder.decode(value, { stream: true }), onEvent);
  }
  parseEvents(buffer + "\n\n", onEvent);
}

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (!SAFE.has(method)) {
    const token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
  }
  const response = await fetch(url(path, options.query), {
    method,
    headers,
    credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const text = await response.text();
  const data = text ? safeJson(text) : undefined;
  if (!response.ok) {
    if (response.status === 401 && !path.startsWith("/api/auth/login")) {
      window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
    }
    const detail = typeof data === "object" && data && "detail" in data ? String((data as { detail: unknown }).detail) : response.statusText;
    throw new ApiError(response.status, detail);
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

// -- shapes the API returns ---------------------------------------------------------------

export type Role = "user" | "sme" | "admin" | "auditor";

export interface Me {
  login: string;
  display_name: string | null;
  email: string | null;
  local: boolean;
  directory: string | null;
  roles: Role[];
  sme_sources: string[];
  pii_processor: boolean;
  categories?: string[];
  category_names?: string[];
  conflicts: string[];
  via: Record<string, string[]>;
}

export interface SignInOptions {
  directories: { name: string; kind: string }[];
  local: boolean;
  local_setup_pending: boolean;
}

export interface Check {
  name: string;
  ok: boolean;
  detail: string;
}

export interface DataSourceView {
  name: string;
  dialect: string;
  origin: string;
  status: string;
  host?: string;
  port?: number;
  database?: string;
  username?: string;
  probed_at?: string | null;
  probe?: Check[] | null;
  introspected?: boolean;
}

export interface DirectoryView {
  name: string;
  kind: string;
  status: string;
  settings: Record<string, unknown>;
  probed_at: string | null;
  probe: Check[] | null;
  synced_at: string | null;
  sync: Record<string, number> | null;
  created_by: string | null;
  /** verified settings whose last test failed: still offered at sign-in (D76) */
  last_test_failed?: boolean;
}

export interface DirectoryRoleView {
  id: string;
  directory: string;
  source: string;
  name: string;
  ref: string;
  description: string | null;
  members: number | null;
  last_seen_at: string;
  gone_at: string | null;
}

export interface GrantView {
  id: string;
  privilege: string;
  subject: string;
  data_source: string | null;
  category?: string | null;
  granted_by: string;
  granted_at: string;
  note: string | null;
  revoked_by: string | null;
  revoked_at: string | null;
}

export interface AccountView {
  login: string;
  directory: string | null;
  local: boolean;
  display_name: string | null;
  email: string | null;
  enabled: boolean;
  last_sign_in_at: string | null;
  synced_at: string | null;
  directory_roles: string[];
  roles: Role[];
  sme_sources: string[];
  pii_processor: boolean;
  categories?: string[];
  conflicts: string[];
  via: Record<string, string[]>;
}

export interface AuditEvent {
  seq: number;
  at: string;
  actor: string | null;
  action: string;
  subject: string | null;
  detail: Record<string, unknown>;
  ip: string | null;
  hash: string;
}

export interface AuditSummary {
  accounts: number;
  by_role: Record<Role, number>;
  pii_processors: number;
  conflicts: string[];
  local_sign_ins: number;
  refusals: number;
  questions_refused?: number;
  chain: { ok: boolean; events: number; head: string; broken_at: number | null; reason: string | null };
}

// -- SME review (5B) ------------------------------------------------------------------------

export type QueueStatus = "open" | "done" | "stale";

export interface ReviewSource {
  name: string;
  id: string;
  dialect: string;
  open: number;
  reviewed: number;
  stale: number;
}

export interface QueueItem {
  id: string | null;
  table_id: string;
  table: string;
  column_id: string | null;
  column: string | null;
  data_type: string | null;
  business_name: string | null;
  role: string | null;
  score: number | null;
  band: string | null;
  status: string | null;
  reasons: string[];
  priority: number | null;
  coverage: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  resolved_at: string | null;
}

export interface Queue {
  total: number;
  items: QueueItem[];
}

export interface Meaning {
  version: number;
  status: string;
  band: string;
  score: number;
  raw_score: number;
  business_name: string | null;
  purpose: string | null;
  role: string | null;
  entity: string | null;
  unit: string | null;
  value_domain: Record<string, unknown> | null;
  value_meanings: Record<string, string>;
  synonyms: string[];
  example_questions: string[];
  assumptions: string[];
  unknowns: string[];
  alternatives: unknown[];
  self_confidence: number | null;
  critique: Record<string, unknown> | null;
  semantic_type: string | null;
  claimed_semantic_type: string | null;
  coverage: string;
  unused: boolean;
  personal?: boolean | null;
  model: string | null;
  prompt_version: string | null;
  created_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  inherited_from: string | null;
}

export interface SummaryLine {
  key: string;
  [value: string]: string | number | null | undefined;
}

export interface Evidence {
  kind: string;
  direction: string;
  strength: number;
  source: string;
  ref: string | null;
  detail: string | null;
}

export interface ColumnProfileView {
  method: string | null;
  rows: number | null;
  non_null: number | null;
  distinct: number | null;
  minimum: string | null;
  maximum: string | null;
  top_values: [string, number][];
  sample_values: string[];
  [key: string]: unknown;
}

export interface JoinHintView {
  id: string;
  from: string;
  to: string;
  source: string;
  status: string;
  confidence: number | null;
  rationale: string | null;
  reviewable: boolean;
}

export interface TestView {
  test: string;
  kind: string;
  passed: boolean | null;
  metric: number | null;
  origin: string | null;
  method: string | null;
  detail: string | null;
}

export interface VersionView {
  version: number;
  created_at: string;
  status: string;
  band: string;
  score: number;
  business_name: string | null;
  role: string | null;
  model: string | null;
  reviewed_by: string | null;
  review_note: string | null;
}

export interface ColumnReview {
  data_source: string;
  table: { id: string; schema: string; name: string; kind: string; business_name: string | null; purpose: string | null; grain: string | null; status: string | null };
  column: {
    id: string;
    name: string;
    data_type?: string;
    nullable?: boolean;
    default?: string | null;
    comment?: string | null;
    keys?: string[];
    checks?: string[];
    references?: string[];
    indexes?: string[];
    computed?: string | null;
    rows?: number | null;
  };
  meaning: Meaning | null;
  summary: SummaryLine[];
  breakdown: Record<string, unknown> | null;
  ledger: Evidence[];
  profile: ColumnProfileView | null;
  personal: boolean;
  masked: boolean;
  tests: TestView[];
  join_hints: JoinHintView[];
  lineage: {
    shown_in: { view: string; column: string; how: string }[];
    comes_from: { how: string; source: string | null; expression: string | null; inputs: string[] }[];
  };
  versions: VersionView[];
  questions: { question: string; at: string; origin: string | null }[];
  review: { id: string; status: string; reasons: string[]; priority: number; importance: number; coverage: string; assignee: string | null } | null;
  reviewable: boolean;
  roles: string[];
}

export interface ColumnDecision {
  meaning: Meaning;
  propagated: { views: string[]; done: boolean; note?: string };
  requeued: string[];
  next: QueueItem | null;
}

export interface TableMeaning {
  version: number;
  status: string;
  business_name: string | null;
  purpose: string | null;
  grain: string | null;
  entity: string | null;
  synonyms: string[];
  assumptions: string[];
  unknowns: string[];
  score: number;
  reviewed_by: string | null;
  reviewed_at: string | null;
  review_note: string | null;
}

export interface TableReview {
  data_source: string;
  id: string;
  category?: Placement | null;
  categories?: { id: string; name: string; description: string | null }[];
  schema: string;
  name: string;
  kind: string;
  rows: number | null;
  meaning: TableMeaning | null;
  columns: {
    id: string;
    name: string;
    data_type: string;
    business_name: string | null;
    role: string | null;
    score: number | null;
    band: string | null;
    status: string | null;
    inherited: boolean;
    queued: boolean;
    reasons: string[];
  }[];
}

export interface GlossaryEntry {
  id: string;
  term: string;
  definition: string;
  synonyms: string[];
  source: string;
  everywhere: boolean;
  editable: boolean;
}

export interface TableListEntry {
  id: string;
  name: string;
  kind: string;
  business_name: string | null;
  reviewed_by: string | null;
  columns: number;
  queued: number;
  decided: number;
}

// -- need-to-know (5C) -------------------------------------------------------------------

export interface CategoryView {
  id: string;
  name: string;
  description: string | null;
  keywords: string[];
  created_by: string;
  created_at: string;
  retired_at: string | null;
  objects: number;
  grants: number;
}

export interface Placement {
  id: string;
  name: string;
  status: "proposed" | "confirmed";
  source: "gaia" | "sme" | "admin";
  decided_by: string | null;
  decided_at: string;
  reason: string | null;
  /** what Gaia's proposal rests on (D77) */
  basis?: "direct" | "view" | "related" | "weak" | null;
}

export interface ObjectView {
  id: string;
  name: string;
  kind: string;
  business_name: string | null;
  category: Placement | null;
}

export interface RoleSuggestion {
  directory_role_id: string;
  directory: string;
  role: string;
  categories: string[];
}

// -- the chat (5D) ---------------------------------------------------------------------------

export interface ChatSource {
  name: string;
  dialect: string;
  origin: string;
  status: string;
  introspected: boolean;
  usable: boolean;
  default: boolean;
  readable_objects: number | null;
}

export interface ConversationView {
  id: string;
  data_source: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ClarificationView {
  id?: string;
  question: string;
  options: string[];
  ambiguity?: string | null;
}

export interface AnswerDoc {
  question: string;
  status: "answered" | "clarify" | "cannot_answer" | "failed" | "refused";
  narrative: string | null;
  columns: string[];
  rows: unknown[][];
  row_cap_reached: boolean;
  personal_columns?: number[];
  masked?: boolean;
  refusal?: string | null;
  interpretation: string | null;
  assumptions: string[];
  uncertain: string[];
  confidence: { level: "high" | "medium" | "low"; score?: number; reasons: string[] } | null;
  clarification: ClarificationView | null;
  reason: string | null;
  notes: string[];
  elapsed_ms: number;
  query_run_id?: string | null;
  rows_kept?: number;
  row_count?: number;
  masked_columns?: number[];
  message_id?: string;
  seq?: number;
}

export interface ChatMessage {
  id: string;
  seq: number;
  role: "user" | "assistant";
  kind: "question" | "clarification_answer" | "answer" | "clarification" | "cannot_answer" | "failed" | "refused";
  content: string;
  payload: Partial<AnswerDoc>;
  created_at: string;
  clarification_id: string | null;
}

export interface QueryView {
  id: string;
  data_source: string;
  question: string | null;
  asked_by: string | null;
  origin: string;
  attempt: number;
  parent_id: string | null;
  status: string;
  created_at: string;
  dialect: string;
  submitted_sql: string;
  executed_sql: string | null;
  errors: { code: string; message: string }[];
  warnings: { code: string; message: string }[];
  tables: string[];
  forbidden: string[];
  row_count: number | null;
  row_cap_reached: boolean;
  elapsed_ms: number | null;
  error: string | null;
  model: string | null;
  attempts?: QueryView[];
}

export interface RunsView {
  /** every data source, whatever the filter (D76) */
  data_sources?: string[];
  totals: { data_source: string; llm_calls: number; prompt_tokens: number; completion_tokens: number; cost: number | null; queries: number }[];
  reasoning: Record<string, unknown>[];
  profiling: Record<string, unknown>[];
}
