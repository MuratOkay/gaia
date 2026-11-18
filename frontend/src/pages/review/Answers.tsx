// Answers people doubted (6D, D87; P6-4): "partly" or "wrong" under an answer on a data source
// this SME reviews. The SME decides what the answer really was and why — only that decision
// is a label (answer calibration); a person's doubt alone is not a verdict.
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams, useSearchParams } from "react-router-dom";

import { api, FEEDBACK_CAUSES, type FeedbackItem, type Verdict } from "../../api/client";
import { ResultTable } from "../../components/chat";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";
import { enc, ReviewNav } from "./common";

type Tab = "open" | "decided";

export function ReviewAnswers() {
  const { t } = useTranslation();
  const { ds = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const status: Tab = params.get("status") === "decided" ? "decided" : "open";
  const [tick, setTick] = useState(0);
  const { data, error, loading } = useLoad(
    () => api<{ total: number; items: FeedbackItem[] }>(`/api/review/${enc(ds)}/feedback`, { query: { status } }),
    [ds, status, tick],
  );
  return (
    <section>
      <ReviewNav ds={ds} />
      <h1>{t("review.answers.title", { ds })}</h1>
      <p className="muted">{t("review.answers.intro")}</p>
      <div className="tabs" role="tablist" aria-label={t("review.tabsLabel")}>
        {(["open", "decided"] as Tab[]).map((s) => (
          <button key={s} type="button" role="tab" aria-selected={s === status} className={s === status ? "tab active" : "tab"}
                  onClick={() => setParams(s === "open" ? {} : { status: s })}>
            {t(`review.answers.tabs.${s}`)}
          </button>
        ))}
      </div>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data && data.items.length === 0 && <p className="note">{t(`review.answers.empty.${status}`)}</p>}
      {data?.items.map((item) => <AnswerItem key={item.id} ds={ds} item={item} onDecided={() => setTick((n) => n + 1)} />)}
    </section>
  );
}

function AnswerItem({ ds, item, onDecided }: { ds: string; item: FeedbackItem; onDecided: () => void }) {
  const { t, i18n } = useTranslation();
  const [decision, setDecision] = useState<Verdict | "">(item.decision ?? "");
  const [cause, setCause] = useState(item.cause ?? "");
  const [note, setNote] = useState(item.decision_note ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  async function decide() {
    setBusy(true);
    setError(null);
    try {
      await api(`/api/review/${enc(ds)}/feedback/${item.id}/decision`, {
        method: "POST", body: { decision, cause: decision === "right" ? null : cause || null, note: note.trim() || null },
      });
      onDecided();
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card answer-item">
      <h2>{item.question}</h2>
      <p className="small muted">
        {t("review.answers.said", { who: item.given_by, verdict: t(`chat.feedback.verdicts.${item.verdict}`), when: when(item.created_at, i18n.language) })}
      </p>
      {item.note && <blockquote>{item.note}</blockquote>}
      <ResultTable columns={item.columns} rows={item.rows} masked={item.masked_columns} />
      {item.row_count !== null && item.row_count > item.rows.length && (
        <p className="muted small">{t("chat.rowsKept", { kept: item.rows.length, total: item.row_count })}</p>
      )}
      {item.narrative && <p className="narrative">{item.narrative}</p>}
      <details open>
        <summary>{t("chat.howRead")}</summary>
        {item.interpretation && <p>{item.interpretation}</p>}
        {item.assumptions.length > 0 && <><p className="small"><strong>{t("chat.assumptions")}</strong></p><ul>{item.assumptions.map((a) => <li key={a}>{a}</li>)}</ul></>}
        {item.uncertain.length > 0 && <><p className="small"><strong>{t("chat.uncertain")}</strong></p><ul>{item.uncertain.map((a) => <li key={a}>{a}</li>)}</ul></>}
        {item.confidence && <p className="small">{t(`chat.confidence.${item.confidence.level}`)}{item.confidence.score !== undefined && ` · ${item.confidence.score.toFixed(2)}`}</p>}
        {item.sql && <pre className="sql">{item.sql}</pre>}
      </details>
      {item.status === "decided" ? (
        <p>
          <strong>{t("review.answers.decidedBy", { who: item.decided_by, when: when(item.decided_at, i18n.language) })}</strong>{" "}
          {t(`review.answers.verdicts.${item.decision}`)}
          {item.cause && <> — {t(`chat.feedback.causes.${item.cause}`, { defaultValue: item.cause })}</>}
          {item.decision_note && <span className="muted"> ({item.decision_note})</span>}
        </p>
      ) : (
        <form className="decide" onSubmit={(e) => { e.preventDefault(); void decide(); }} aria-label={t("review.answers.decide")}>
          <fieldset>
            <legend>{t("review.answers.question")}</legend>
            {(["right", "partly", "wrong"] as Verdict[]).map((v) => (
              <label key={v} className="check">
                <input type="radio" name={`d-${item.id}`} checked={decision === v} onChange={() => setDecision(v)} />
                {t(`review.answers.verdicts.${v}`)}
              </label>
            ))}
          </fieldset>
          {decision && decision !== "right" && (
            <label>
              {t("review.answers.why")}
              <select value={cause} onChange={(e) => setCause(e.target.value)} required>
                <option value="">—</option>
                {FEEDBACK_CAUSES.map((c) => <option key={c} value={c}>{t(`review.answers.causes.${c}`)}</option>)}
              </select>
            </label>
          )}
          <label>
            {t("review.answers.note")}
            <textarea rows={2} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || !decision || (decision !== "right" && !cause)}>{t("review.answers.submit")}</button>
          <ErrorNote error={error} />
        </form>
      )}
    </article>
  );
}
