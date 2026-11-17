// The pieces of an answer (Phase 5D): the table first, then the narrative; the confidence as a
// level with its reasons (never a percentage before Phase 6 calibrates it, T7); clarifications
// as choices; refusals worded without naming what was refused (5C).
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { AnswerDoc, ChatMessage } from "../api/client";

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

export function ConfidenceBadge({ confidence }: { confidence: AnswerDoc["confidence"] }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  if (!confidence) return null;
  return (
    <div className={`confidence confidence-${confidence.level}`}>
      <button type="button" className="link" aria-expanded={open} onClick={() => setOpen(!open)}>
        {t(`chat.confidence.${confidence.level}`)}
      </button>
      {open && (
        confidence.reasons.length
          ? <ul>{confidence.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
          : <p className="muted small">{t("chat.confidenceNoReasons")}</p>
      )}
    </div>
  );
}

export const STAGES = ["retrieval", "retrieved", "generation", "checking", "running", "repairing", "narrating"] as const;

export function Stage({ stage }: { stage: string }) {
  const { t } = useTranslation();
  return <p className="muted stage" role="status">{t(`chat.stages.${stage}`, { defaultValue: stage })}</p>;
}

/** Gaia's turn: an answer, a question back, "cannot answer", a refusal or a failure. */
export function AssistantTurn({ kind, doc, canSeeSql, onOption, open }: {
  kind: ChatMessage["kind"];
  doc: Partial<AnswerDoc>;
  canSeeSql: boolean;
  onOption?: (n: number) => void;
  open?: boolean;
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
          <ConfidenceBadge confidence={doc.confidence ?? null} />
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
