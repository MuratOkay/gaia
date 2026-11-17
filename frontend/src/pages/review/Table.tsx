import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";

import { api, type TableMeaning, type TableReview } from "../../api/client";
import { CategoryPanel } from "./CategoryPanel";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";
import { Band, enc, MeaningStatus, Reasons, ReviewNav, splitList } from "./common";

const FIELDS = ["business_name", "purpose", "grain", "entity"] as const;

/** Only the changed fields of the table's meaning. */
export function tableChanges(meaning: TableMeaning, draft: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of FIELDS) if ((draft[k] ?? "").trim() !== (meaning[k] ?? "")) out[k] = draft[k].trim();
  const synonyms = splitList(draft.synonyms ?? "");
  if (synonyms.join("|") !== meaning.synonyms.join("|")) out.synonyms = synonyms;
  return out;
}

/** A table's own meaning — name, purpose, grain — reviewed on the same screen as its
 *  columns (P5-4). */
export function TablePage() {
  const { t, i18n } = useTranslation();
  const { ds = "", id = "" } = useParams();
  const { data, error, loading, reload } = useLoad(() => api<TableReview>(`/api/review/${enc(ds)}/tables/${id}`), [ds, id]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [failure, setFailure] = useState<Error | null>(null);
  const [saved, setSaved] = useState(false);
  const m = data?.meaning;
  useEffect(() => {
    if (m) setDraft({ business_name: m.business_name ?? "", purpose: m.purpose ?? "", grain: m.grain ?? "", entity: m.entity ?? "", synonyms: m.synonyms.join(", ") });
  }, [m]);

  async function decide(event: FormEvent) {
    event.preventDefault();
    if (!m) return;
    const changes = tableChanges(m, draft);
    setFailure(null);
    setSaved(false);
    try {
      await api(`/api/review/${enc(ds)}/tables/${id}/decision`, {
        method: "POST",
        body: { action: Object.keys(changes).length ? "correct" : "accept", changes: Object.keys(changes).length ? changes : undefined, note: note.trim() || undefined },
      });
      setSaved(true);
      setNote("");
      await reload();
    } catch (e) {
      setFailure(e as Error);
    }
  }

  return (
    <section>
      <ReviewNav ds={ds} />
      <Busy loading={loading && !data} />
      <ErrorNote error={error} />
      {data && (
        <>
          <h1>{data.schema}.{data.name} <span className="muted small">{data.kind} · {t("review.rows", { count: data.rows ?? 0 })}</span></h1>
          {m ? (
            <form className="card decision" onSubmit={decide} aria-label={t("review.tableMeaning")}>
              <h2>{t("review.tableMeaning")}</h2>
              <p><MeaningStatus status={m.status} /> {m.reviewed_by && <span className="muted">{t("review.by", { who: m.reviewed_by, when: when(m.reviewed_at, i18n.language) })}{m.review_note && ` — ${m.review_note}`}</span>}</p>
              <fieldset>
                <label>{t("review.fields.business_name")}<input value={draft.business_name ?? ""} onChange={(e) => setDraft({ ...draft, business_name: e.target.value })} /></label>
                <label>{t("review.fields.grain")}<input value={draft.grain ?? ""} onChange={(e) => setDraft({ ...draft, grain: e.target.value })} /></label>
                <label className="wide">{t("review.fields.purpose")}<textarea rows={3} value={draft.purpose ?? ""} onChange={(e) => setDraft({ ...draft, purpose: e.target.value })} /></label>
                <label>{t("review.fields.entity")}<input value={draft.entity ?? ""} onChange={(e) => setDraft({ ...draft, entity: e.target.value })} /></label>
                <label>{t("review.fields.synonyms")}<input value={draft.synonyms ?? ""} onChange={(e) => setDraft({ ...draft, synonyms: e.target.value })} /></label>
              </fieldset>
              {m.unknowns.length > 0 && <p className="muted">{t("review.unknowns", { items: m.unknowns.join("; ") })}</p>}
              <label>{t("review.note")}<textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} /></label>
              <ErrorNote error={failure} />
              {saved && <p className="note" role="status">{t("review.tableSaved")}</p>}
              <div className="actions">
                <button type="submit">{Object.keys(tableChanges(m, draft)).length ? t("review.saveCorrection") : t("review.acceptTable")}</button>
              </div>
            </form>
          ) : <p className="note">{t("review.noMeaning")}</p>}

          <CategoryPanel ds={ds} table={data} onDone={reload} />

          <h2>{t("review.columns")}</h2>
          <table>
            <thead><tr><th>{t("review.column")}</th><th>{t("review.gaiaSays")}</th><th>{t("review.score")}</th><th>{t("review.status")}</th></tr></thead>
            <tbody>
              {data.columns.map((c) => (
                <tr key={c.id}>
                  <td><Link to={`/review/${enc(ds)}/columns/${c.id}`}>{c.name}</Link> <span className="muted small">{c.data_type}</span></td>
                  <td>{c.business_name ?? "—"}{c.role && <span className="muted"> · {c.role}</span>}</td>
                  <td><Band band={c.band} score={c.score} /></td>
                  <td>
                    <MeaningStatus status={c.status} />
                    {c.inherited && <span className="muted small"> {t("review.inheritedShort")}</span>}
                    {c.queued && <> <Reasons reasons={c.reasons} /></>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  );
}
