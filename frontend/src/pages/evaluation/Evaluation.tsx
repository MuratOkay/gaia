import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type CalibrationTrial, type EvaluationReport, type EvaluationRunView } from "../../api/client";
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

/** The 95 % Wilson interval of k in n (as the report's). */
export function wilson(k: number, n: number): [number, number] | null {
  if (n <= 0) return null;
  const z = 1.959964;
  const p = k / n;
  const d = 1 + (z * z) / n;
  const centre = (p + (z * z) / (2 * n)) / d;
  const half = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / d;
  return [Math.max(0, centre - half), Math.min(1, centre + half)];
}

/** Predicted against observed, per band of the prediction: the identity (grey) and the
 *  calibrator on held-out labels (blue); the diagonal is a perfect calibration (6C). */
export function Reliability({ trial }: { trial: CalibrationTrial }) {
  const { t } = useTranslation();
  const size = 160;
  const at = (x: number) => 10 + x * size;
  const dot = (r: { predicted: number; observed: number; n: number }, cls: string, key: string) => (
    <circle key={key} className={cls} cx={at(r.predicted)} cy={at(1 - r.observed)} r={Math.min(7, 2 + Math.sqrt(r.n))}>
      <title>{t("evaluation.calibration.point", { p: pct(r.predicted), o: pct(r.observed), n: r.n })}</title>
    </circle>
  );
  return (
    <svg className="reliability" viewBox={`0 0 ${size + 20} ${size + 20}`} role="img"
         aria-label={t("evaluation.calibration.diagram", { model: trial.model })}>
      <rect x={10} y={10} width={size} height={size} fill="none" stroke="#ccc" />
      <line className="diag" x1={at(0)} y1={at(1)} x2={at(1)} y2={at(0)} />
      {trial.identity.reliability.map((r, i) => dot(r, "ident", `i${i}`))}
      {trial.held_out?.reliability.map((r, i) => dot(r, "held", `h${i}`))}
    </svg>
  );
}

function fmt(x: number | null | undefined): string {
  return x === null || x === undefined ? "—" : x.toFixed(3);
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
                          {r.questions?.never_detail?.[k] && (() => {
                            const d = r.questions!.never_detail![k];
                            return (
                              <div className="muted">
                                {d.missing?.length ? t("evaluation.lacked", { objects: d.missing.join(", ") })
                                  : d.objects?.length ? t("evaluation.heldAll") : null}
                                {d.clarification?.[1] && <div>{t("evaluation.answeredAfter", { text: d.clarification[1] })}</div>}
                                {d.interpretation && <div>{t("evaluation.understood", { text: d.interpretation })}</div>}
                              </div>
                            );
                          })()}
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
      {r?.wrong_causes && r.wrong_causes.wrong > 0 && (
        <>
          <h3>{t("evaluation.causes.title")}</h3>
          <ul>
            <li>{t("evaluation.causes.retrieval", { n: r.wrong_causes.retrieval, of: r.wrong_causes.wrong })}
              {r.wrong_causes.retrieval_questions.length > 0 && <span className="muted"> — {r.wrong_causes.retrieval_questions.join(", ")}</span>}</li>
            <li>{t("evaluation.causes.reading", { n: r.wrong_causes.reading, of: r.wrong_causes.wrong })}
              {r.wrong_causes.reading_questions.length > 0 && <span className="muted"> — {r.wrong_causes.reading_questions.join(", ")}</span>}</li>
            {r.wrong_causes.unknown > 0 && <li className="muted">{t("evaluation.causes.unknown", { n: r.wrong_causes.unknown })}</li>}
          </ul>
        </>
      )}
      {r?.confidence_levels && Object.keys(r.confidence_levels).length > 0 && (
        <>
          <h3>{t("evaluation.levels.title")}</h3>
          <ul>
            {Object.entries(r.confidence_levels).map(([level, [k, n]]) => (
              <li key={level}>{t(`chat.confidence.${level}`)}: <Share k={k} n={n} interval={wilson(k, n)} /></li>
            ))}
          </ul>
          <p className="muted small">{t("evaluation.levels.help")}</p>
        </>
      )}
      {r?.labels && (
        <>
          <h3>{t("evaluation.labels.title")}</h3>
          <p>{t("evaluation.labels.counts", { n: r.labels.n, right: r.labels.counts.right ?? 0, partly: r.labels.counts.partly ?? 0, wrong: r.labels.counts.wrong ?? 0 })}
            {r.labels.auroc !== null && <> · AUROC {r.labels.auroc.toFixed(2)}{r.labels.ranking_ready === false && <span className="muted"> {t("evaluation.labels.tooFew")}</span>}</>}</p>
          {r.labels.judge && (
            <p className="small">{t("evaluation.labels.judge", { agree: r.labels.judge.agree, n: r.labels.judge.n, kappa: r.labels.judge.kappa === null ? "—" : r.labels.judge.kappa.toFixed(2) })}</p>
          )}
        </>
      )}
      {!!r?.calibration?.length && (
        <>
          <h3>{t("evaluation.calibration.title")}</h3>
          <p className="muted small">{t("evaluation.calibration.help")}</p>
          <div className="cards">
            {r.calibration.map((c) => (
              <div key={`${c.kind}-${c.model}-${c.scoring}`} className="card">
                <p><strong>{t(`evaluation.calibration.kinds.${c.kind}`)}</strong> · <code>{c.model}</code></p>
                <p className="small">{c.why}</p>
                <p className="small">{t("evaluation.calibration.metrics", { ece: fmt(c.identity.ece), brier: fmt(c.identity.brier) })}</p>
                {c.held_out && (
                  <p className="small">{t("evaluation.calibration.heldOut", { method: c.method, ece: fmt(c.held_out.ece), brier: fmt(c.held_out.brier) })}{" "}
                    <strong>{c.improves ? t("evaluation.calibration.improves") : t("evaluation.calibration.doesNot")}</strong></p>
                )}
                <p className="small">{t("evaluation.calibration.inUse")}: {c.in_use ? <code>{c.in_use}</code> : t("evaluation.calibration.identity")}</p>
                {c.labels > 0 && <Reliability trial={c} />}
              </div>
            ))}
          </div>
        </>
      )}
      {r?.categories && r.categories.rule.n > 0 && (
        <>
          <h3>{t("evaluation.categories.title")}</h3>
          <p>{t("evaluation.categories.rule", { n: r.categories.rule.n, proposed: r.categories.rule.n - r.categories.rule.none, agree: r.categories.rule.agree })}</p>
          <ul className="small">
            {Object.entries(r.categories.rule.by_basis).map(([b, [k, n]]) => (
              <li key={b}>{t(`categories.basis.${b}`, { defaultValue: b })}: {k} / {n}</li>
            ))}
            {r.categories.rule.misses.map((m) => (
              <li key={m.object} className="warn">{t("evaluation.categories.miss", { object: m.object, proposed: m.proposed, confirmed: m.confirmed })}</li>
            ))}
          </ul>
          {r.categories.seen.n > 0 && (
            <p className="muted small">{t("evaluation.categories.seen", { kept: r.categories.seen.kept, n: r.categories.seen.n - r.categories.seen.no_proposal })}</p>
          )}
        </>
      )}
      {r?.feedback && (
        <>
          <h3>{t("evaluation.feedback.title")}</h3>
          <p>{t("evaluation.feedback.counts", { right: r.feedback.given.right ?? 0, partly: r.feedback.given.partly ?? 0, wrong: r.feedback.given.wrong ?? 0, open: r.feedback.open, decided: r.feedback.decided })}</p>
        </>
      )}
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
