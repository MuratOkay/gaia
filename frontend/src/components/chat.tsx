// The pieces of an answer (Phase 5D): the table first, then the narrative; the confidence as a
// level with its reasons — and, once an answer calibrator is in use (6D), P(right) for SMEs,
// administrators and auditors (P6-2); never a percentage from the heuristic alone (T7);
// clarifications as choices; refusals worded without naming what was refused (5C); under an
// answer, "Was this right?" (6D, P6-4).
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, type AnswerDoc, type ChatMessage, type FeedbackView, type Verdict } from "../api/client";

export const HIDDEN = "[hidden]";
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

/** A cell as people read it: numbers grouped, dates in the locale, masked data as dots. */
export function formatCell(value: unknown, locale = "en"): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") {
    return new Intl.NumberFormat(locale, { maximumFractionDigits: Number.isInteger(value) ? 0 : 2 }).format(value);
  }
  if (typeof value === "boolean") return value ? "✓" : "✗";
  const text = String(value);
  if (ISO_DATE.test(text)) {
    const d = new Date(`${text}T00:00:00`);
    return Number.isNaN(d.getTime()) ? text : new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(d);
  }
  if (ISO_DATETIME.test(text)) {
    const d = new Date(text);
    return Number.isNaN(d.getTime()) ? text : new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(d);
  }
  return text;
}

export function ResultTable({ columns, rows, masked = [] }: { columns: string[]; rows: unknown[][]; masked?: number[] }) {
  const { t, i18n } = useTranslation();
  if (!columns.length) return null;
  const numeric = columns.map((_c, i) => rows.length > 0 && rows.every((r) => r[i] === null || typeof r[i] === "number"));
  return (
    <div className="result" role="region" aria-label={t("chat.result")}>
      <table>
        <thead>
          <tr>{columns.map((c, i) => <th key={i} className={numeric[i] ? "num" : undefined}>{c}{masked.includes(i) && <span className="muted" title={t("chat.maskedColumn")}> 🔒</span>}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => (
            <tr key={ri}>
              {r.map((v, i) => v === HIDDEN
                ? <td key={i} className="masked" title={t("chat.maskedCell")}>•••</td>
                : <td key={i} className={numeric[i] ? "num" : undefined}>{formatCell(v, i18n.language)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ConfidenceBadge({ confidence, showProbability = false }: {
  confidence: AnswerDoc["confidence"];
  /** SMEs, administrators and auditors see P(right) (P6-2); people who only chat, the level */
  showProbability?: boolean;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  if (!confidence) return null;
  const p = confidence.probability;
  return (
    <div className={`confidence confidence-${confidence.level}`}>
      <button type="button" className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
        {t(`chat.confidence.${confidence.level}`)}
      </button>
      {showProbability && (p !== undefined && p !== null
        ? <span className="small"> {t("chat.probability", { p: Math.round(p * 100), lo: Math.round((confidence.lower ?? 0) * 100) })}</span>
        : <span className="muted small" title={t("chat.uncalibratedHelp")}> {t("chat.uncalibrated")}</span>)}
      {open && (
        confidence.reasons.length
          ? <ul>{confidence.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          : <p className="muted small">{t("chat.confidenceNoReasons")}</p>
      )}
    </div>
  );
}

/** "Was this right?" under an answer (6D, P6-4): right is counted; partly and wrong go to the
 *  data source's SMEs, whose decision then shows here. Every verdict is sent on the click
 *  (D92): before, "Partly" and "No" only opened a note form and nothing reached the expert
 *  until its Send was pressed — on the owner's host two verdicts were lost that way
 *  (OWNER_VERIFICATION §32). The note follows, optional, and can be added or changed. */
export function WasThisRight({ messageId, initial }: { messageId: string; initial?: FeedbackView | null }) {
  const { t } = useTranslation();
  const [given, setGiven] = useState<FeedbackView | null>(initial ?? null);
  const [changing, setChanging] = useState(false);
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(verdict: Verdict, text?: string | null) {
    setBusy(true);
    setError(null);
    try {
      const f = await api<FeedbackView>(`/api/messages/${messageId}/feedback`, {
        method: "PUT", body: { verdict, note: text?.trim() || null },
      });
      setGiven(f);
      setChanging(false);
      // after "Partly" or "No": offer the note (the verdict is already with the expert)
      setNoting(verdict !== "right" && text === undefined);
      if (text === undefined) setNote(f.note ?? "");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (given && !changing) {
    return (
      <div className="feedback small" aria-label={t("chat.feedback.title")}>
        <span className="muted">{t(`chat.feedback.given.${given.verdict}`)}</span>
        {given.status === "open" && <span className="muted"> · {t("chat.feedback.waiting")}</span>}
        {given.status === "decided" && given.decision && (
          <span> · {t("chat.feedback.decided", { decision: t(`chat.feedback.decisions.${given.decision}`) })}
            {given.cause && <> — {t(`chat.feedback.causes.${given.cause}`, { defaultValue: given.cause })}</>}
            {given.decision_note && <span className="muted"> ({given.decision_note})</span>}
          </span>
        )}
        {given.note && !noting && <span className="muted"> · “{given.note}”</span>}
        {" "}<button type="button" className="link" onClick={() => setChanging(true)}>{t("chat.feedback.change")}</button>
        {given.verdict !== "right" && given.status === "open" && !noting && (
          <>{" "}<button type="button" className="link" onClick={() => { setNote(given.note ?? ""); setNoting(true); }}>
            {given.note ? t("chat.feedback.editNote") : t("chat.feedback.addNote")}</button></>
        )}
        {noting && (
          <form className="feedback-note" onSubmit={(e) => { e.preventDefault(); void send(given.verdict as Verdict, note); }}>
            <label>
              {t("chat.feedback.notePrompt")}
              <textarea rows={2} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />
            </label>
            <button type="submit" disabled={busy}>{t("chat.feedback.saveNote")}</button>
            <span className="muted"> {t("chat.feedback.sentToSme")}</span>
          </form>
        )}
        {error && <span className="error"> {error}</span>}
      </div>
    );
  }
  return (
    <div className="feedback small" role="group" aria-label={t("chat.feedback.title")}>
      <span>{t("chat.feedback.title")}</span>{" "}
      {(["right", "partly", "wrong"] as Verdict[]).map((v) => (
        <button key={v} type="button" disabled={busy} onClick={() => void send(v)}>
          {t(`chat.feedback.verdicts.${v}`)}
        </button>
      ))}
      {" "}<span className="muted">{t("chat.feedback.toSme")}</span>
      {error && <span className="error"> {error}</span>}
    </div>
  );
}

export const STAGES = ["retrieval", "retrieved", "generation", "checking", "running", "repairing", "narrating"] as const;

export function Stage({ stage }: { stage: string }) {
  const { t } = useTranslation();
  return <p className="muted stage" role="status">{t(`chat.stages.${stage}`, { defaultValue: stage })}</p>;
}

/** Gaia's turn: an answer, a question back, "cannot answer", a refusal or a failure. */
export function AssistantTurn({ kind, doc, canSeeSql, onOption, open, messageId, feedback, showProbability = false }: {
  kind: ChatMessage["kind"];
  doc: Partial<AnswerDoc>;
  canSeeSql: boolean;
  onOption?: (n: number) => void;
  open?: boolean;
  /** the stored message: "Was this right?" needs it (6D) */
  messageId?: string;
  feedback?: FeedbackView | null;
  showProbability?: boolean;
}) {
  const { t } = useTranslation();
  const masked = doc.masked ? doc.personal_columns ?? [] : doc.masked_columns ?? [];
  if (kind === "clarification" && doc.clarification) {
    const c = doc.clarification;
    return (
      <div className="turn assistant clarification">
        <p><strong>{c.question}</strong></p>
        {c.options.length > 0 && (
          <ol className="options">
            {c.options.map((o, i) => (
              <li key={i}>
                <button type="button" className="option" disabled={!open || !onOption} onClick={() => onOption?.(i + 1)}>{o}</button>
              </li>
            ))}
          </ol>
        )}
        {open && <p className="muted small">{t("chat.clarifyHint")}</p>}
      </div>
    );
  }
  if (kind === "answer") {
    const kept = doc.rows_kept !== undefined && doc.row_count !== undefined && doc.rows_kept < doc.row_count;
    return (
      <div className="turn assistant answer">
        <ResultTable columns={doc.columns ?? []} rows={doc.rows ?? []} masked={masked} />
        {kept && <p className="muted small">{t("chat.rowsKept", { kept: doc.rows_kept, total: doc.row_count })}</p>}
        {doc.row_cap_reached && <p className="muted small">{t("chat.capped")}</p>}
        {masked.length > 0 && <p className="muted small">{t(doc.masked ? "chat.maskedLive" : "chat.maskedStored")}</p>}
        {doc.narrative && <p className="narrative">{doc.narrative}</p>}
        <div className="answer-foot">
          <ConfidenceBadge confidence={doc.confidence ?? null} showProbability={showProbability} />
          {(doc.interpretation || (doc.assumptions?.length ?? 0) > 0 || (doc.uncertain?.length ?? 0) > 0) && (
            <details>
              <summary>{t("chat.howRead")}</summary>
              {doc.interpretation && <p>{doc.interpretation}</p>}
              {(doc.assumptions?.length ?? 0) > 0 && (
                <>
                  <p className="small"><strong>{t("chat.assumptions")}</strong></p>
                  <ul>{doc.assumptions!.map((a) => <li key={a}>{a}</li>)}</ul>
                </>
              )}
              {(doc.uncertain?.length ?? 0) > 0 && (
                <>
                  <p className="small"><strong>{t("chat.uncertain")}</strong></p>
                  <ul>{doc.uncertain!.map((a) => <li key={a}>{a}</li>)}</ul>
                </>
              )}
            </details>
          )}
          {canSeeSql && doc.query_run_id && <Link className="small" to={`/queries/${doc.query_run_id}`}>{t("chat.showSql")}</Link>}
        </div>
        {messageId && !messageId.startsWith("live-") && <WasThisRight messageId={messageId} initial={feedback} />}
      </div>
    );
  }
  const cls = kind === "refused" ? "refused" : kind === "failed" ? "failed" : "cannot";
  return (
    <div className={`turn assistant ${cls}`}>
      <p><strong>{t(`chat.kinds.${kind}`, { defaultValue: kind })}</strong> {doc.reason}</p>
      {canSeeSql && doc.query_run_id && kind !== "cannot_answer" && <Link className="small" to={`/queries/${doc.query_run_id}`}>{t("chat.showSql")}</Link>}
    </div>
  );
}
