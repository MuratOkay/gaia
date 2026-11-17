import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type EvaluationReport, type EvaluationRunView } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";

const pct = (x: number) => `${Math.round(x * 100)}%`;

function Share({ k, n, interval }: { k: number; n: number; interval: [number, number] | null }) {
  const { t } = useTranslation();
  if (!n) return <span className="muted">—</span>;
  return (
    <span>
      {t("evaluation.share", { k, n, pct: pct(k / n) })}
      {interval && <span className="muted"> {t("evaluation.interval", { lo: pct(interval[0]), hi: pct(interval[1]) })}</span>}
    </span>
  );
}

/** Golden runs and the quality report (Phase 6A, D80) — administrators and auditors. */
export function Evaluation() {
  const { t, i18n } = useTranslation();
  const sources = useLoad(() => api<string[]>("/api/evaluation/data-sources"));
  const [ds, setDs] = useState("");
  const [last, setLast] = useState(3);
  const [compare, setCompare] = useState(false);
  useEffect(() => {
    if (!ds && sources.data?.length) setDs(sources.data[0]);
  }, [sources.data, ds]);
  const runs = useLoad(() => (ds ? api<EvaluationRunView[]>("/api/evaluation/runs", { query: { data_source: ds } }) : Promise.resolve([])), [ds]);
  const report = useLoad(
    () => (ds ? api<EvaluationReport>("/api/evaluation/report", { query: { data_source: ds, last, compare_builds: compare } }) : Promise.resolve(null)),
    [ds, last, compare],
  );
  const r = report.data;
  const c = r?.comparison;
  return (
    <section>
      <h1>{t("evaluation.title")}</h1>
      <p className="muted">{t("evaluation.intro")}</p>
      <ErrorNote error={sources.error ?? runs.error ?? report.error} />
      {sources.data?.length === 0 && <p className="note">{t("evaluation.none")}</p>}
      <div className="actions">
        <label>{t("queries.dataSource")}
          <select value={ds} onChange={(e) => setDs(e.target.value)}>
            {sources.data?.map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label>{t("evaluation.last")}
          <select value={last} onChange={(e) => setLast(Number(e.target.value))}>
            {[1, 3, 5, 10].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <label className="check small">
          <input type="checkbox" checked={compare} onChange={(e) => setCompare(e.target.checked)} />
          {t("evaluation.compareBuilds")}
        </label>
      </div>
      <Busy loading={report.loading} />
      {r && r.correct && (
        <>
          <h2>{t("evaluation.report")}</h2>
          <ul>
            <li>{t("evaluation.correct")}: <Share {...r.correct} /></li>
            <li>{t("evaluation.executable")}: <Share {...r.executable!} /></li>
            {r.per_run && r.per_run.length > 0 && (
              <li>{t("evaluation.perRun", { min: pct(Math.min(...r.per_run)), max: pct(Math.max(...r.per_run)), runs: r.per_run.length })}</li>
            )}
            {r.questions && (
              <>
                <li>{t("evaluation.always", { count: r.questions.always.length })}</li>
                <li>{t("evaluation.flaky")}: {r.questions.flaky.length ? r.questions.flaky.map(([k, a, n]) => `${k} ${a}/${n}`).join(", ") : t("evaluation.noneOf")}</li>
                <li>{t("evaluation.never")}: {r.questions.never.length ? r.questions.never.join(", ") : t("evaluation.noneOf")}
                  {r.questions.never.length > 0 && (
                    <ul className="small">
                      {r.questions.never.map((k) => (
                        <li key={k}>{k}: {r.questions?.never_reasons?.[k] ?? ""}
                          {r.questions?.never_sql?.[k] && <pre className="small">{r.questions.never_sql[k]}</pre>}
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              </>
            )}
            {r.clarification && (
              <li>{t("evaluation.agree")}: <Share k={r.clarification.agree[0]} n={r.clarification.agree[1]} interval={null} /></li>
            )}
          </ul>
          {c && (
            <div className={`note ${c.verdict === "fails" ? "warn" : ""}`} role="status">
              <p><strong>{t(`evaluation.verdicts.${c.verdict.replace(" ", "_")}`)}</strong>{" "}
                {t("evaluation.difference", { d: (c.difference * 100).toFixed(1), lo: (c.low * 100).toFixed(1), hi: (c.high * 100).toFixed(1), n: c.questions })}</p>
              {c.moved.map(([k, b, cand]) => (
                <p key={k} className="small">{t("evaluation.moved", { k, b: pct(b), c: pct(cand) })}</p>
              ))}
              {c.meanings && <p className="warn small">{c.meanings}</p>}
            </div>
          )}
        </>
      )}
      {r && !r.correct && <p className="note">{t("evaluation.noMeasured")}</p>}
      <h2>{t("evaluation.runs")}</h2>
      <Busy loading={runs.loading} />
      {!!runs.data?.length && (
        <table>
          <thead><tr><th>{t("evaluation.run")}</th><th>{t("runs.started")}</th><th>{t("evaluation.build")}</th><th>{t("runs.model")}</th>
            <th>{t("evaluation.mode")}</th><th>{t("evaluation.meanings")}</th><th>{t("evaluation.correct")}</th></tr></thead>
          <tbody>
            {runs.data.map((x) => (
              <tr key={x.id} className={x.measured ? undefined : "revoked"}>
                <td><code>{x.id.slice(0, 8)}</code>{x.as_login && <div className="muted small">{t("evaluation.as", { who: x.as_login })}</div>}</td>
                <td>{when(x.started_at, i18n.language)}</td>
                <td>{x.build ?? "—"}</td>
                <td>{x.model ?? "—"}</td>
                <td>{t(`evaluation.modes.${x.mode}`, { defaultValue: x.mode })}</td>
                <td>{x.meanings_key ? <><code>{x.meanings_key.slice(0, 6)}</code>{x.sme_decisions != null && <span className="muted small"> {t("evaluation.sme", { n: x.sme_decisions })}</span>}</> : <span className="muted">{t("evaluation.unknown")}</span>}</td>
                <td>{x.scored ? `${x.correct}/${x.scored}` : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
