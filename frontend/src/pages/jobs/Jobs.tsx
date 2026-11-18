import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type AlertView, type JobKind, type JobsView, type JobView, type ScheduleView } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";

const FINISHED = new Set(["done", "partial", "cancelled", "failed", "refused"]);

/** Jobs (7C): what ran and runs, who asked for it, what it cost — administrators start,
 * cancel and schedule; auditors read. */
export function JobsPanel({ admin }: { admin: boolean }) {
  const { t, i18n } = useTranslation();
  const jobs = useLoad(() => api<JobsView>("/api/jobs"), []);
  const schedules = useLoad(() => api<ScheduleView[]>("/api/jobs/schedules"), []);
  const [open, setOpen] = useState<JobView | null>(null);
  const [kind, setKind] = useState<JobKind>("refresh");
  const [ds, setDs] = useState("");
  const [when_, setWhen] = useState("daily 02:00");
  const [error, setError] = useState<Error | null>(null);

  async function act(work: () => Promise<unknown>) {
    setError(null);
    try {
      await work();
      await Promise.all([jobs.reload(), schedules.reload()]);
    } catch (e) {
      setError(e as Error);
    }
  }
  const list = Array.isArray(jobs.data?.jobs) ? jobs.data.jobs : [];
  const sources = Array.isArray(jobs.data?.data_sources) ? jobs.data.data_sources : [];
  const kinds = Array.isArray(jobs.data?.kinds) ? jobs.data.kinds : [];
  const scheduled = Array.isArray(schedules.data) ? schedules.data : [];
  const source = ds || sources[0] || "";

  return (
    <section aria-label={t("jobs.title")}>
      <h2>{t("jobs.title")}</h2>
      <p className="muted">{t("jobs.intro")}</p>
      <ErrorNote error={error ?? jobs.error ?? schedules.error} />
      <Busy loading={jobs.loading} />
      {admin && kinds.length > 0 && (
        <form className="inline" onSubmit={(e) => { e.preventDefault(); void act(() => api("/api/jobs", { method: "POST", body: { kind, data_source: source } })); }}>
          <label>{t("jobs.kind")}
            <select value={kind} onChange={(e) => setKind(e.target.value as JobKind)}>
              {kinds.map((k) => <option key={k} value={k}>{t(`jobs.kinds.${k}`)}</option>)}
            </select>
          </label>
          <label>{t("queries.dataSource")}
            <select value={source} onChange={(e) => setDs(e.target.value)}>
              {sources.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <button type="submit">{t("jobs.start")}</button>
          <label>{t("jobs.when")}
            <input value={when_} onChange={(e) => setWhen(e.target.value)} placeholder="daily 02:00 | weekly sun 04:00" />
          </label>
          <button type="button" onClick={() => void act(() => api("/api/jobs/schedules", { method: "PUT", body: { data_source: source, kind, when: when_ } }))}>{t("jobs.schedule")}</button>
        </form>
      )}
      {jobs.data && list.length === 0 && <p className="note">{t("jobs.none")}</p>}
      {list.length > 0 && (
        <table>
          <thead><tr><th>{t("runs.started")}</th><th>{t("queries.dataSource")}</th><th>{t("jobs.kind")}</th><th>{t("queries.status")}</th><th>{t("jobs.by")}</th><th>{t("runs.tokens")}</th><th /></tr></thead>
          <tbody>
            {list.map((j) => (
              <tr key={j.id}>
                <td>{when(j.started_at ?? j.created_at, i18n.language)}</td>
                <td>{j.data_source}</td>
                <td>{t(`jobs.kinds.${j.kind}`)}</td>
                <td>
                  <span className={`status status-${j.state}`}>{t(`jobs.states.${j.state}`)}</span>
                  {j.state === "running" && j.progress.step && <span className="muted"> · {t("jobs.step", { step: j.progress.step })}</span>}
                  {j.cancel_requested_at && !FINISHED.has(j.state) && <span className="muted"> · {t("jobs.stopping")}</span>}
                  {j.error && <div className="muted">{j.error}</div>}
                  {typeof j.result?.stopped === "string" && <div className="muted">{t("jobs.stopped", { why: j.result.stopped })}</div>}
                </td>
                <td>{j.requested_by === "schedule" ? t("jobs.bySchedule") : j.requested_by}</td>
                <td>{j.tokens.toLocaleString(i18n.language)}</td>
                <td>
                  <button type="button" className="link" onClick={() => void api<JobView>(`/api/jobs/${j.id}`).then(setOpen)}>{t("jobs.log")}</button>
                  {admin && !FINISHED.has(j.state) && !j.cancel_requested_at && (
                    <button type="button" className="link" onClick={() => void act(() => api(`/api/jobs/${j.id}/cancel`, { method: "POST" }))}>{t("jobs.cancel")}</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && (
        <div className="panel" role="region" aria-label={t("jobs.log")}>
          <h3>{t(`jobs.kinds.${open.kind}`)} · {open.data_source} <button type="button" className="link" onClick={() => setOpen(null)}>{t("common.close")}</button></h3>
          <ol className="log">
            {(open.events ?? []).map((e) => <li key={e.seq} className={e.level}><span className="muted">{when(e.at, i18n.language)}</span> <pre>{e.message}</pre></li>)}
          </ol>
        </div>
      )}
      <h3>{t("jobs.schedules")}</h3>
      {schedules.data && scheduled.length === 0 && <p className="muted">{t("jobs.noSchedules")}</p>}
      {scheduled.length > 0 && (
        <table>
          <thead><tr><th>{t("queries.dataSource")}</th><th>{t("jobs.kind")}</th><th>{t("jobs.when")}</th><th>{t("jobs.next")}</th><th /></tr></thead>
          <tbody>
            {scheduled.map((s) => (
              <tr key={s.id}>
                <td>{s.data_source}</td><td>{t(`jobs.kinds.${s.kind}`)}</td><td>{s.when}</td>
                <td>{s.enabled ? when(s.next_due_at, i18n.language) : t("jobs.off")}</td>
                <td>{admin && (
                  <button type="button" className="link" onClick={() => void act(() => api(`/api/jobs/schedules/${s.id}/enabled`, { method: "POST", body: { enabled: !s.enabled } }))}>
                    {s.enabled ? t("jobs.turnOff") : t("jobs.turnOn")}
                  </button>
                )}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** What an administrator should look at (7C): failed or partial jobs, drift the watch caught. */
export function Alerts() {
  const { t, i18n } = useTranslation();
  const { data, error, reload } = useLoad(() => api<AlertView[]>("/api/jobs/alerts"), []);
  if (error || !Array.isArray(data) || data.length === 0) return null;
  return (
    <section aria-label={t("alerts.title")}>
      <h2>{t("alerts.title")}</h2>
      <ul className="alerts">
        {data.map((a) => (
          <li key={a.id} className="note warn">
            <strong>{t(`alerts.kinds.${a.kind}`, { defaultValue: a.kind })}</strong> · {when(a.created_at, i18n.language)} — {a.message}
            {" "}<button type="button" className="link" onClick={() => void api(`/api/jobs/alerts/${a.id}/ack`, { method: "POST" }).then(reload)}>{t("alerts.ack")}</button>
          </li>
        ))}
      </ul>
    </section>
  );
}
