import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api, type QueryView, type RunsView } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";

const PAGE = 100;
const STATUSES = ["", "ok", "rejected", "refused", "error", "timeout"] as const;

export function QueryStatus({ q }: { q: QueryView }) {
  const { t } = useTranslation();
  const refused = q.errors.some((e) => e.code === "forbidden" || e.code === "personal_data");
  const key = refused ? "refused" : q.status;
  return <span className={`status query-${key}`}>{t(`queries.statuses.${key}`, { defaultValue: key })}</span>;
}

/** The SQL behind the answers (P5-7): SMEs for their data sources, administrators and
 *  auditors for all — never the "user" role (P4-2). */
export function Queries() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const asked = params.get("asked_by") ?? "";
  const ds = params.get("data_source") ?? "";
  const offset = Number(params.get("offset") ?? 0) || 0;
  const { data, error, loading } = useLoad(
    () => api<{ total: number; items: QueryView[] }>("/api/queries", {
      query: { status: status || undefined, asked_by: asked || undefined, data_source: ds || undefined, limit: PAGE, offset },
    }),
    [status, asked, ds, offset],
  );

  function set(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "offset") next.delete("offset");
    setParams(next);
  }

  return (
    <section>
      <h1>{t("queries.title")}</h1>
      <p className="muted">{t("queries.intro")}</p>
      <form className="inline" onSubmit={(e) => e.preventDefault()} aria-label={t("queries.filters")}>
        <label>{t("queries.status")}
          <select value={status} onChange={(e) => set("status", e.target.value)}>
            {STATUSES.map((s) => <option key={s} value={s}>{s ? t(`queries.statuses.${s}`) : t("queries.any")}</option>)}
          </select>
        </label>
        <label>{t("queries.askedBy")}<input defaultValue={asked} onBlur={(e) => set("asked_by", e.target.value.trim())} /></label>
        <label>{t("queries.dataSource")}<input defaultValue={ds} onBlur={(e) => set("data_source", e.target.value.trim())} /></label>
      </form>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data && (
        <>
          <p className="muted">{t("queries.count", { count: data.total })}</p>
          <table>
            <thead><tr><th>{t("queries.when")}</th><th>{t("queries.askedBy")}</th><th>{t("queries.question")}</th><th>{t("queries.status")}</th><th>{t("queries.rows")}</th></tr></thead>
            <tbody>
              {data.items.map((q) => (
                <tr key={q.id}>
                  <td>{when(q.created_at, i18n.language)}<div className="muted small">{q.data_source}</div></td>
                  <td>{q.asked_by ?? q.origin}</td>
                  <td><Link to={`/queries/${q.id}`}>{q.question ?? "—"}</Link>{q.attempt > 1 && <span className="muted small"> · {t("queries.attempt", { n: q.attempt })}</span>}</td>
                  <td><QueryStatus q={q} /></td>
                  <td>{q.row_count ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="actions">
            <button type="button" disabled={offset === 0} onClick={() => set("offset", String(Math.max(0, offset - PAGE)))}>{t("review.previous")}</button>
            <button type="button" disabled={offset + data.items.length >= data.total} onClick={() => set("offset", String(offset + PAGE))}>{t("review.nextPage")}</button>
          </div>
        </>
      )}
    </section>
  );
}

function Sql({ sql }: { sql: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  return (
    <div className="sql">
      <pre><code>{sql}</code></pre>
      <button type="button" className="link" onClick={async () => {
        await navigator.clipboard?.writeText(sql);
        setCopied(true);
      }}>{copied ? t("queries.copied") : t("queries.copy")}</button>
    </div>
  );
}

export function QueryPage() {
  const { t, i18n } = useTranslation();
  const { id = "" } = useParams();
  const { data: q, error, loading } = useLoad(() => api<QueryView>(`/api/queries/${id}`), [id]);
  return (
    <section>
      <p><Link to="/queries">{t("queries.back")}</Link></p>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {q && (
        <>
          <h1>{q.question ?? t("queries.noQuestion")}</h1>
          <p className="muted">{t("queries.meta", { who: q.asked_by ?? q.origin, when: when(q.created_at, i18n.language), ds: q.data_source })}</p>
          {(q.attempts ?? [q]).map((a) => (
            <article key={a.id} className={`card${a.id === q.id ? " current" : ""}`}>
              <h2>{t("queries.attempt", { n: a.attempt })} <QueryStatus q={a} /></h2>
              <Sql sql={a.executed_sql ?? a.submitted_sql} />
              {a.errors.length > 0 && <ul>{a.errors.map((e, i) => <li key={i} className="error"><code>{e.code}</code> {e.message}</li>)}</ul>}
              {a.forbidden.length > 0 && <p className="warn">{t("queries.reachedFor", { what: a.forbidden.join(", ") })}</p>}
              {a.warnings.map((w, i) => <p key={i} className="warn small"><code>{w.code}</code> {w.message}</p>)}
              <p className="muted small">
                {t("queries.facts", { rows: a.row_count ?? "—", ms: a.elapsed_ms ?? "—", tables: a.tables.join(", ") || "—" })}
                {a.error && ` · ${a.error}`}
              </p>
            </article>
          ))}
        </>
      )}
    </section>
  );
}

/** What ran to build Gaia's understanding, and what it cost (5E, read-only). */
export function Runs() {
  const { t, i18n } = useTranslation();
  const [ds, setDs] = useState("");
  const { data, error, loading } = useLoad(() => api<RunsView>("/api/runs", { query: { data_source: ds || undefined } }), [ds]);
  const cell = (v: unknown) => (v === null || v === undefined ? "—" : String(v));
  return (
    <section>
      <h1>{t("runs.title")}</h1>
      <p className="muted">{t("runs.intro")}</p>
      <label>{t("queries.dataSource")}
        <select value={ds} onChange={(e) => setDs(e.target.value)}>
          <option value="">{t("queries.any")}</option>
          {data?.totals.map((x) => <option key={x.data_source} value={x.data_source}>{x.data_source}</option>)}
        </select>
      </label>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data && (
        <>
          <h2>{t("runs.totals")}</h2>
          <table>
            <thead><tr><th>{t("queries.dataSource")}</th><th>{t("runs.calls")}</th><th>{t("runs.tokens")}</th><th>{t("runs.cost")}</th><th>{t("runs.questions")}</th></tr></thead>
            <tbody>
              {data.totals.map((x) => (
                <tr key={x.data_source}>
                  <td>{x.data_source}</td><td>{x.llm_calls}</td>
                  <td>{(x.prompt_tokens + x.completion_tokens).toLocaleString(i18n.language)}</td>
                  <td>{x.cost === null ? "—" : x.cost.toFixed(4)}</td><td>{x.queries}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h2>{t("runs.reasoning")}</h2>
          <table>
            <thead><tr><th>{t("runs.started")}</th><th>{t("queries.dataSource")}</th><th>{t("queries.status")}</th><th>{t("runs.tables")}</th><th>{t("runs.calls")}</th><th>{t("runs.tokens")}</th><th>{t("runs.model")}</th></tr></thead>
            <tbody>
              {data.reasoning.map((r) => (
                <tr key={String(r.id)}>
                  <td>{when(r.started_at as string, i18n.language)}</td><td>{cell(r.data_source)}</td><td>{cell(r.status)}</td>
                  <td>{t("runs.reasoned", { done: r.tables_reasoned, skipped: r.tables_skipped, failed: r.tables_failed })}</td>
                  <td>{cell(r.llm_calls)}</td>
                  <td>{(Number(r.prompt_tokens ?? 0) + Number(r.completion_tokens ?? 0)).toLocaleString(i18n.language)}</td>
                  <td>{cell(r.model)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h2>{t("runs.profiling")}</h2>
          <table>
            <thead><tr><th>{t("runs.started")}</th><th>{t("queries.dataSource")}</th><th>{t("queries.status")}</th><th>{t("runs.tables")}</th><th>{t("runs.queriesRun")}</th></tr></thead>
            <tbody>
              {data.profiling.map((r) => (
                <tr key={String(r.id)}>
                  <td>{when(r.started_at as string, i18n.language)}</td><td>{cell(r.data_source)}</td><td>{cell(r.status)}</td>
                  <td>{t("runs.profiled", { done: r.tables_profiled, reused: r.tables_reused, failed: r.tables_failed })}</td>
                  <td>{cell(r.queries)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
