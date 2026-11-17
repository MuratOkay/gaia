import { type FormEvent, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api, type ColumnDecision, type ColumnProfileView, type ColumnReview, type JoinHintView, type Meaning, type SummaryLine } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";
import { Band, enc, MeaningStatus, Reasons, ReviewNav, splitList } from "./common";

export type Action = "accept" | "correct" | "reject" | "unused";
const ACTIONS: Action[] = ["accept", "correct", "reject", "unused"];

export interface Draft {
  business_name: string;
  purpose: string;
  role: string;
  entity: string;
  unit: string;
  synonyms: string;
  meanings: [string, string][];
  personal: "" | "yes" | "no"; // "": the detectors decide (5C)
}

export const FEW_VALUES = 25;

/** What the form starts from: Gaia's meaning, the meanings already given, and — for a code,
 *  or any column with few distinct values — the values seen in the data, so the SME only
 *  types what they mean. Never for personal data shown as shapes. */
export function draftFrom(meaning: Meaning, profile: ColumnProfileView | null, masked: boolean): Draft {
  const given = Object.entries(meaning.value_meanings ?? {});
  const topValues = profile?.top_values ?? [];
  const seen = masked ? [] : topValues.map(([v]) => v).filter((v) => !given.some(([k]) => k === v));
  const few = profile?.distinct !== null && profile?.distinct !== undefined && profile.distinct <= FEW_VALUES;
  const codeLike = meaning.role === "code" || meaning.role === "flag" || given.length > 0 || few;
  return {
    business_name: meaning.business_name ?? "",
    purpose: meaning.purpose ?? "",
    role: meaning.role ?? "",
    entity: meaning.entity ?? "",
    unit: meaning.unit ?? "",
    synonyms: meaning.synonyms.join(", "),
    personal: meaning.personal === true ? "yes" : meaning.personal === false ? "no" : "",
    meanings: [...given, ...(codeLike ? seen.slice(0, FEW_VALUES).map((v): [string, string] => [v, ""]) : [])],
  };
}

/** The request body for a decision: only what the SME changed is sent. */
export function decisionBody(action: Action, note: string, draft: Draft, meaning: Meaning) {
  const body: { action: Action; note?: string; changes?: Record<string, unknown>; value_meanings?: Record<string, string> } = { action };
  if (note.trim()) body.note = note.trim();
  if (action !== "correct") return body;
  const changes: Record<string, unknown> = {};
  for (const key of ["business_name", "purpose", "role", "entity", "unit"] as const) {
    if (draft[key].trim() !== (meaning[key] ?? "")) changes[key] = draft[key].trim();
  }
  const synonyms = splitList(draft.synonyms);
  if (synonyms.join("|") !== meaning.synonyms.join("|")) changes.synonyms = synonyms;
  const personal = draft.personal === "yes" ? true : draft.personal === "no" ? false : null;
  if (personal !== (meaning.personal ?? null)) changes.personal = personal;
  if (Object.keys(changes).length) body.changes = changes;
  const meanings = Object.fromEntries(draft.meanings.filter(([k, v]) => k.trim() && v.trim()).map(([k, v]) => [k.trim(), v.trim()]));
  const before = meaning.value_meanings ?? {};
  const same = Object.keys(meanings).length === Object.keys(before).length && Object.entries(meanings).every(([k, v]) => before[k] === v);
  if (!same) body.value_meanings = meanings;
  return body;
}

function Sentence({ line }: { line: SummaryLine }) {
  const { t } = useTranslation();
  const values: Record<string, unknown> = { ...line };
  if (line.key === "score") values.band = t(`review.bands.${String(line.band)}`, { defaultValue: String(line.band) });
  if (line.key === "decided") values.status = t(`review.statuses.${String(line.status)}`, { defaultValue: String(line.status) });
  if (line.key === "critique") values.verdict = t(`review.verdicts.${String(line.verdict)}`, { defaultValue: String(line.verdict) });
  if (line.key === "score") values.score = Number(line.score).toFixed(2);
  return <li>{t(`review.summary.${line.key}`, values)}</li>;
}

function DecisionPanel({ ds, item, onDone }: { ds: string; item: ColumnReview; onDone: (d: ColumnDecision) => void }) {
  const { t } = useTranslation();
  const meaning = item.meaning!;
  const [action, setAction] = useState<Action>("accept");
  const [note, setNote] = useState("");
  const initial = useMemo(() => draftFrom(meaning, item.profile, item.masked), [meaning, item.profile, item.masked]);
  const [draft, setDraft] = useState<Draft>(initial);
  const [error, setError] = useState<Error | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setDraft(initial);
  }, [initial]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      onDone(await api<ColumnDecision>(`/api/review/${enc(ds)}/columns/${item.column.id}/decision`, { method: "POST", body: decisionBody(action, note, draft, meaning) }));
    } catch (e) {
      setError(e as Error);
    } finally {
      setBusy(false);
    }
  }

  const field = (key: "business_name" | "entity" | "unit") => (
    <label>
      {t(`review.fields.${key}`)}
      <input value={draft[key]} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} />
    </label>
  );

  return (
    <form className="card decision" onSubmit={submit} aria-label={t("review.decision")}>
      <h2>{t("review.decision")}</h2>
      <div className="choices" role="radiogroup" aria-label={t("review.decision")}>
        {ACTIONS.map((a) => (
          <label key={a} className="check">
            <input type="radio" name="action" value={a} checked={action === a} onChange={() => setAction(a)} />
            <span><strong>{t(`review.actions.${a}`)}</strong> <span className="muted">— {t(`review.actionHelp.${a}`)}</span></span>
          </label>
        ))}
      </div>
      {action === "correct" && (
        <fieldset>
          <legend>{t("review.correctIntro")}</legend>
          {field("business_name")}
          <label>
            {t("review.fields.role")}
            <select value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })}>
              {item.roles.map((r) => <option key={r} value={r}>{r}</option>)}
            </select>
          </label>
          <label className="wide">
            {t("review.fields.purpose")}
            <textarea rows={3} value={draft.purpose} onChange={(e) => setDraft({ ...draft, purpose: e.target.value })} />
          </label>
          {field("entity")}
          {field("unit")}
          <label className="wide">
            {t("review.fields.synonyms")}
            <input value={draft.synonyms} onChange={(e) => setDraft({ ...draft, synonyms: e.target.value })} />
          </label>
          <label>
            {t("review.fields.personal")}
            <select value={draft.personal} onChange={(e) => setDraft({ ...draft, personal: e.target.value as Draft["personal"] })}>
              <option value="">{t("review.personal.auto")}</option>
              <option value="yes">{t("review.personal.yes")}</option>
              <option value="no">{t("review.personal.no")}</option>
            </select>
          </label>
          <div className="wide">
            <h3>{t("review.meaningsTitle")}</h3>
            <p className="muted small">{t("review.meaningsHelp")}</p>
            <table className="compact">
              <thead><tr><th>{t("review.value")}</th><th>{t("review.means")}</th><th /></tr></thead>
              <tbody>
                {draft.meanings.map(([value, means], i) => (
                  <tr key={i}>
                    <td><input aria-label={t("review.valueN", { n: i + 1 })} value={value} onChange={(e) => setDraft({ ...draft, meanings: draft.meanings.map((m, j) => (j === i ? [e.target.value, m[1]] : m)) })} /></td>
                    <td><input aria-label={t("review.meansN", { value: value || i + 1 })} value={means} onChange={(e) => setDraft({ ...draft, meanings: draft.meanings.map((m, j) => (j === i ? [m[0], e.target.value] : m)) })} /></td>
                    <td><button type="button" className="link" onClick={() => setDraft({ ...draft, meanings: draft.meanings.filter((_m, j) => j !== i) })}>{t("common.remove")}</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <button type="button" className="link" onClick={() => setDraft({ ...draft, meanings: [...draft.meanings, ["", ""]] })}>{t("review.addValue")}</button>
          </div>
        </fieldset>
      )}
      <label>
        {action === "reject" ? t("review.noteRequired") : t("review.note")}
        <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} required={action === "reject"} />
      </label>
      <ErrorNote error={error} />
      <div className="actions">
        <button type="submit" disabled={busy}>{t("review.save", { action: t(`review.actions.${action}`) })}</button>
      </div>
    </form>
  );
}

function Outcome({ ds, done }: { ds: string; done: ColumnDecision }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <article className="card done" role="status">
      <h2>{t("review.saved", { version: done.meaning.version })}</h2>
      <p><MeaningStatus status={done.meaning.status} /> <Band band={done.meaning.band} score={done.meaning.score} /></p>
      {done.propagated.views.length > 0 && (
        <p>{done.propagated.done ? t("review.propagated", { views: done.propagated.views.join(", ") }) : t("review.propagateLater", { views: done.propagated.views.join(", ") })}</p>
      )}
      {done.requeued.length > 0 && <p>{t("review.requeued", { columns: done.requeued.join(", ") })}</p>}
      <div className="actions">
        {done.next?.column_id
          ? <button type="button" onClick={() => navigate(`/review/${enc(ds)}/columns/${done.next!.column_id}`)}>{t("review.nextItem", { item: `${done.next.table}.${done.next.column}` })}</button>
          : <p className="note">{t("review.queueEmpty")}</p>}
        <Link className="button secondary" to={`/review/${enc(ds)}`}>{t("review.backToQueue")}</Link>
      </div>
    </article>
  );
}

function HintRow({ ds, hint, onChanged }: { ds: string; hint: JoinHintView; onChanged: () => void }) {
  const { t } = useTranslation();
  const [error, setError] = useState<Error | null>(null);
  async function decide(action: "confirm" | "reject") {
    let note: string | undefined;
    if (action === "reject") {
      const answer = window.prompt(t("review.hintWhy", { from: hint.from, to: hint.to }));
      if (!answer) return;
      note = answer;
    }
    setError(null);
    try {
      await api(`/api/review/${enc(ds)}/join-hints/${hint.id}/decision`, { method: "POST", body: { action, note } });
      onChanged();
    } catch (e) {
      setError(e as Error);
    }
  }
  return (
    <tr>
      <td>{hint.from}</td>
      <td>{hint.to}</td>
      <td>{t(`review.hintSources.${hint.source}`, { defaultValue: hint.source })} · {t(`review.hintStatuses.${hint.status}`, { defaultValue: hint.status })}</td>
      <td>{hint.confidence ?? "—"}</td>
      <td>{hint.rationale ?? ""}<ErrorNote error={error} /></td>
      <td>
        {hint.reviewable && hint.source === "llm" && (
          <span className="actions">
            <button type="button" onClick={() => decide("confirm")}>{t("review.confirm")}</button>
            <button type="button" className="danger" onClick={() => decide("reject")}>{t("review.rejectHint")}</button>
          </span>
        )}
      </td>
    </tr>
  );
}

function Facts({ item }: { item: ColumnReview }) {
  const { t } = useTranslation();
  const c = item.column;
  const rows: [string, React.ReactNode][] = [
    [t("review.facts.type"), c.data_type],
    [t("review.facts.nullable"), c.nullable === undefined ? "—" : c.nullable ? t("common.yes") : t("common.no")],
    [t("review.facts.default"), c.default],
    [t("review.facts.comment"), c.comment],
    [t("review.facts.keys"), c.keys?.join(", ")],
    [t("review.facts.references"), c.references?.join(", ")],
    [t("review.facts.checks"), c.checks?.join("; ")],
    [t("review.facts.indexes"), c.indexes?.join(", ")],
    [t("review.facts.computed"), c.computed],
    [t("review.facts.rows"), c.rows?.toLocaleString()],
  ];
  return (
    <table className="compact">
      <tbody>{rows.map(([k, v]) => <tr key={k}><th>{k}</th><td>{v || "—"}</td></tr>)}</tbody>
    </table>
  );
}

function Profile({ item }: { item: ColumnReview }) {
  const { t } = useTranslation();
  const p = item.profile;
  if (!p) return <p className="muted">{t("review.noProfile")}</p>;
  return (
    <>
      {item.masked && <p className="note warn">{t("review.masked")}</p>}
      <table className="compact">
        <tbody>
          <tr><th>{t("review.profile.rows")}</th><td>{p.rows?.toLocaleString() ?? "—"}</td></tr>
          <tr><th>{t("review.profile.nonNull")}</th><td>{p.non_null?.toLocaleString() ?? "—"}</td></tr>
          <tr><th>{t("review.profile.distinct")}</th><td>{p.distinct?.toLocaleString() ?? "—"}</td></tr>
          <tr><th>{t("review.profile.range")}</th><td>{p.minimum ?? "—"} … {p.maximum ?? "—"}</td></tr>
        </tbody>
      </table>
      {p.top_values.length > 0 && (
        <table className="compact">
          <thead><tr><th>{t("review.value")}</th><th>{t("review.profile.count")}</th><th>{t("review.means")}</th></tr></thead>
          <tbody>
            {p.top_values.map(([v, n]) => (
              <tr key={v}><td><code>{v}</code></td><td>{n.toLocaleString()}</td><td>{item.meaning?.value_meanings?.[v] ?? ""}</td></tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

/** Everything Gaia knows about one column: a summary first, the details below, the
 *  decision beside it (P5-4). */
export function ColumnPage() {
  const { t, i18n } = useTranslation();
  const { ds = "", id = "" } = useParams();
  const { data: item, error, loading, reload } = useLoad(() => api<ColumnReview>(`/api/review/${enc(ds)}/columns/${id}`), [ds, id]);
  const [done, setDone] = useState<ColumnDecision | null>(null);
  useEffect(() => {
    setDone(null);
  }, [id]);

  async function decided(d: ColumnDecision) {
    setDone(d);
    await reload();
  }

  return (
    <section>
      <ReviewNav ds={ds} />
      <Busy loading={loading && !item} />
      <ErrorNote error={error} />
      {item && (
        <>
          <h1>
            <Link to={`/review/${enc(ds)}/tables/${item.table.id}`}>{item.table.schema}.{item.table.name}</Link>.{item.column.name}
            <span className="muted small"> {item.column.data_type}</span>
          </h1>
          {item.review && item.review.status === "open" && <p><Reasons reasons={item.review.reasons} /></p>}
          {item.table.purpose && <p className="muted">{t("review.inTable", { purpose: item.table.purpose })}</p>}
          <div className="review-grid">
            <div>
              <h2>{t("review.summaryTitle")}</h2>
              <ul className="summary">{item.summary.map((line, i) => <Sentence key={i} line={line} />)}</ul>
              {item.meaning && (
                <table className="compact">
                  <tbody>
                    <tr><th>{t("review.fields.business_name")}</th><td>{item.meaning.business_name}</td></tr>
                    <tr><th>{t("review.fields.purpose")}</th><td>{item.meaning.purpose}</td></tr>
                    <tr><th>{t("review.fields.role")}</th><td>{item.meaning.role}</td></tr>
                    <tr><th>{t("review.fields.entity")}</th><td>{item.meaning.entity ?? "—"}</td></tr>
                    <tr><th>{t("review.fields.unit")}</th><td>{item.meaning.unit ?? "—"}</td></tr>
                    <tr><th>{t("review.fields.synonyms")}</th><td>{item.meaning.synonyms.join(", ") || "—"}</td></tr>
                    {Object.keys(item.meaning.value_meanings).length > 0 && (
                      <tr><th>{t("review.meaningsTitle")}</th><td>{Object.entries(item.meaning.value_meanings).map(([k, v]) => `${k} = ${v}`).join("; ")}</td></tr>
                    )}
                    <tr><th>{t("review.status")}</th><td><MeaningStatus status={item.meaning.status} /> <Band band={item.meaning.band} score={item.meaning.score} /></td></tr>
                    {item.meaning.reviewed_by && (
                      <tr><th>{t("review.decided")}</th><td>{t("review.by", { who: item.meaning.reviewed_by, when: when(item.meaning.reviewed_at, i18n.language) })}{item.meaning.review_note && ` — ${item.meaning.review_note}`}</td></tr>
                    )}
                  </tbody>
                </table>
              )}
            </div>
            <div>
              {done && <Outcome ds={ds} done={done} />}
              {!done && item.reviewable && <DecisionPanel ds={ds} item={item} onDone={decided} />}
              {!item.reviewable && item.meaning?.inherited_from && (
                <p className="note">{t("review.inherited", { base: item.meaning.inherited_from })}</p>
              )}
              {!item.meaning && <p className="note">{t("review.noMeaning")}</p>}
            </div>
          </div>

          <h2>{t("review.details")}</h2>
          <details open>
            <summary>{t("review.sections.facts")}</summary>
            <Facts item={item} />
          </details>
          <details open={item.meaning?.role === "code"}>
            <summary>{t("review.sections.profile")}</summary>
            <Profile item={item} />
          </details>
          <details>
            <summary>{t("review.sections.evidence", { count: item.ledger.length })}</summary>
            <table className="compact">
              <thead><tr><th>{t("review.evidence.direction")}</th><th>{t("review.evidence.kind")}</th><th>{t("review.evidence.strength")}</th><th>{t("review.evidence.source")}</th><th>{t("review.evidence.detail")}</th></tr></thead>
              <tbody>
                {item.ledger.map((e, i) => (
                  <tr key={i}><td className={e.direction === "contradicts" ? "error" : ""}>{t(`review.directions.${e.direction}`, { defaultValue: e.direction })}</td><td>{e.kind}</td><td>{e.strength.toFixed(2)}</td><td>{e.source}</td><td>{e.detail}</td></tr>
                ))}
              </tbody>
            </table>
            {item.breakdown && (
              <details>
                <summary>{t("review.sections.breakdown")}</summary>
                <pre className="small">{JSON.stringify(item.breakdown, null, 2)}</pre>
              </details>
            )}
          </details>
          <details>
            <summary>{t("review.sections.tests", { count: item.tests.length })}</summary>
            <table className="compact">
              <thead><tr><th>{t("review.tests.test")}</th><th>{t("review.tests.result")}</th><th>{t("review.tests.metric")}</th><th>{t("review.evidence.detail")}</th></tr></thead>
              <tbody>
                {item.tests.map((x) => (
                  <tr key={x.test}><td><code>{x.test}</code></td><td>{x.passed === null ? t("review.tests.notRun") : x.passed ? t("common.pass") : t("common.fail")}</td><td>{x.metric ?? "—"}</td><td>{x.detail}</td></tr>
                ))}
              </tbody>
            </table>
          </details>
          <details open={item.join_hints.some((h) => h.source === "llm")}>
            <summary>{t("review.sections.joins", { count: item.join_hints.length })}</summary>
            <table className="compact">
              <thead><tr><th>{t("review.joins.from")}</th><th>{t("review.joins.to")}</th><th>{t("review.joins.origin")}</th><th>{t("review.joins.confidence")}</th><th>{t("review.joins.why")}</th><th /></tr></thead>
              <tbody>{item.join_hints.map((h) => <HintRow key={h.id} ds={ds} hint={h} onChanged={reload} />)}</tbody>
            </table>
          </details>
          <details>
            <summary>{t("review.sections.lineage")}</summary>
            {item.lineage.comes_from.map((l, i) => (
              <p key={i}>{t("review.lineage.comesFrom", { how: t(`review.lineage.kinds.${l.how}`), source: l.source ?? l.inputs.join(", ") })}{l.expression && <code className="small"> {l.expression}</code>}</p>
            ))}
            {item.lineage.shown_in.length > 0 && (
              <ul>{item.lineage.shown_in.map((l) => <li key={`${l.view}.${l.column}`}>{t("review.lineage.shownIn", { view: l.view, column: l.column, how: t(`review.lineage.kinds.${l.how}`) })}</li>)}</ul>
            )}
            {item.lineage.comes_from.length === 0 && item.lineage.shown_in.length === 0 && <p className="muted">{t("review.lineage.none")}</p>}
          </details>
          <details>
            <summary>{t("review.sections.versions", { count: item.versions.length })}</summary>
            <table className="compact">
              <thead><tr><th>#</th><th>{t("review.versions.when")}</th><th>{t("review.status")}</th><th>{t("review.fields.business_name")}</th><th>{t("review.versions.who")}</th></tr></thead>
              <tbody>
                {item.versions.map((v) => (
                  <tr key={v.version}><td>{v.version}</td><td>{when(v.created_at, i18n.language)}</td><td><MeaningStatus status={v.status} /> <Band band={v.band} score={v.score} /></td><td>{v.business_name} <span className="muted">{v.role}</span></td><td>{v.reviewed_by ?? v.model ?? "—"}{v.review_note && <span className="muted"> — {v.review_note}</span>}</td></tr>
                ))}
              </tbody>
            </table>
          </details>
          <details>
            <summary>{t("review.sections.questions", { count: item.questions.length })}</summary>
            <p className="muted small">{t("review.questionsHelp")}</p>
            <ul>{item.questions.map((q, i) => <li key={i}>{q.question} <span className="muted">· {when(q.at, i18n.language)}</span></li>)}</ul>
          </details>
        </>
      )}
    </section>
  );
}
