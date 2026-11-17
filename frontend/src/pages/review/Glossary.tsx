import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { useParams } from "react-router-dom";

import { api, type GlossaryEntry } from "../../api/client";
import { Busy, ErrorNote, useLoad } from "../../components/util";
import { enc, ReviewNav, splitList } from "./common";

/** Business terms and what they mean here: a question that uses one reaches the SQL
 *  generator with its definition (D61 c). */
export function ReviewGlossary() {
  const { t } = useTranslation();
  const { ds = "" } = useParams();
  const { data, error, loading, reload } = useLoad(() => api<GlossaryEntry[]>(`/api/review/${enc(ds)}/glossary`), [ds]);
  const [form, setForm] = useState({ term: "", definition: "", synonyms: "" });
  const [failure, setFailure] = useState<Error | null>(null);

  async function run(work: Promise<unknown>) {
    setFailure(null);
    try {
      await work;
      await reload();
      return true;
    } catch (e) {
      setFailure(e as Error);
      return false;
    }
  }

  async function add(event: FormEvent) {
    event.preventDefault();
    const ok = await run(api(`/api/review/${enc(ds)}/glossary`, { method: "POST", body: { term: form.term.trim(), definition: form.definition.trim(), synonyms: splitList(form.synonyms) } }));
    if (ok) setForm({ term: "", definition: "", synonyms: "" });
  }

  async function edit(g: GlossaryEntry) {
    const definition = window.prompt(t("glossary.editPrompt", { term: g.term }), g.definition);
    if (definition === null) return;
    await run(api(`/api/review/${enc(ds)}/glossary/${g.id}`, { method: "PATCH", body: { definition } }));
  }

  async function withdraw(g: GlossaryEntry) {
    if (!window.confirm(t("glossary.confirmWithdraw", { term: g.term }))) return;
    await run(api(`/api/review/${enc(ds)}/glossary/${g.id}`, { method: "DELETE" }));
  }

  return (
    <section>
      <ReviewNav ds={ds} />
      <h1>{t("glossary.title", { ds })}</h1>
      <p className="muted">{t("glossary.intro")}</p>
      <Busy loading={loading} />
      <ErrorNote error={error ?? failure} />
      {data?.length === 0 && <p className="note">{t("glossary.none")}</p>}
      {!!data?.length && (
        <table>
          <thead><tr><th>{t("glossary.term")}</th><th>{t("glossary.definition")}</th><th>{t("glossary.synonyms")}</th><th /></tr></thead>
          <tbody>
            {data.map((g) => (
              <tr key={g.id}>
                <td><strong>{g.term}</strong>{g.everywhere && <div className="muted small">{t("glossary.everywhere")}</div>}</td>
                <td>{g.definition}</td>
                <td>{g.synonyms.join(", ")}</td>
                <td>
                  {g.editable && (
                    <span className="actions">
                      <button type="button" className="link" onClick={() => edit(g)}>{t("glossary.edit")}</button>
                      <button type="button" className="link" onClick={() => withdraw(g)}>{t("glossary.withdraw")}</button>
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h2>{t("glossary.add")}</h2>
      <form className="grid" onSubmit={add} aria-label={t("glossary.add")}>
        <label>{t("glossary.term")}<input value={form.term} onChange={(e) => setForm({ ...form, term: e.target.value })} required /></label>
        <label>{t("glossary.synonyms")}<input value={form.synonyms} onChange={(e) => setForm({ ...form, synonyms: e.target.value })} /></label>
        <label className="wide">{t("glossary.definition")}<textarea rows={2} value={form.definition} onChange={(e) => setForm({ ...form, definition: e.target.value })} required /></label>
        <div className="wide"><button type="submit">{t("glossary.addButton")}</button></div>
      </form>
    </section>
  );
}
