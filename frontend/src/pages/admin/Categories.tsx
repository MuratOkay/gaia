import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type CategoryView, type DataSourceView, type ObjectView } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";

function words(text: string): string[] {
  return text.split(",").map((w) => w.trim()).filter(Boolean);
}

/** Need-to-know (5C): the data categories, what each object's data is, and — on the Roles &
 *  grants page — who may read which category. Nothing is readable until granted. */
export function Categories() {
  const { t, i18n } = useTranslation();
  const categories = useLoad(() => api<CategoryView[]>("/api/admin/categories"));
  const sources = useLoad(() => api<DataSourceView[]>("/api/admin/data-sources"));
  const [ds, setDs] = useState("");
  const objects = useLoad(() => (ds ? api<ObjectView[]>(`/api/admin/data-sources/${encodeURIComponent(ds)}/objects`) : Promise.resolve([])), [ds]);
  const [form, setForm] = useState({ name: "", keywords: "", description: "" });
  const [error, setError] = useState<Error | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    const live = sources.data?.filter((s) => s.status !== "tombstoned") ?? [];
    if (!ds && live.length) setDs(live[0].name);
  }, [sources.data, ds]);

  async function run(work: Promise<unknown>, done?: (r: unknown) => string | null) {
    setError(null);
    setNote(null);
    try {
      const r = await work;
      setNote(done ? done(r) : null);
      await Promise.all([categories.reload(), objects.reload()]);
    } catch (e) {
      setError(e as Error);
    }
  }

  function add(event: FormEvent) {
    event.preventDefault();
    void run(api("/api/admin/categories", { method: "POST", body: { name: form.name.trim(), description: form.description || undefined, keywords: words(form.keywords) } }))
      .then(() => setForm({ name: "", keywords: "", description: "" }));
  }

  function edit(c: CategoryView) {
    const keywords = window.prompt(t("categories.keywordsPrompt", { name: c.name }), c.keywords.join(", "));
    if (keywords === null) return;
    void run(api(`/api/admin/categories/${c.id}`, { method: "PATCH", body: { keywords: words(keywords) } }));
  }

  function retire(c: CategoryView) {
    if (!window.confirm(t("categories.confirmRetire", { name: c.name }))) return;
    void run(api(`/api/admin/categories/${c.id}`, { method: "DELETE" }));
  }

  function place(o: ObjectView, category: string) {
    if (!category) return;
    void run(api(`/api/admin/data-sources/${encodeURIComponent(ds)}/objects/${o.id}/category`, { method: "PUT", body: { category } }));
  }

  const missing = objects.data?.filter((o) => !o.category).length ?? 0;
  const live = categories.data ?? [];

  return (
    <section>
      <h1>{t("categories.title")}</h1>
      <p className="muted">{t("categories.intro")}</p>
      <ErrorNote error={categories.error ?? sources.error ?? objects.error ?? error} />
      {note && <p className="note" role="status">{note}</p>}

      <h2>{t("categories.list")}</h2>
      <Busy loading={categories.loading} />
      {live.length === 0 && <p className="note">{t("categories.none")}</p>}
      {live.length > 0 && (
        <table>
          <thead><tr><th>{t("categories.name")}</th><th>{t("categories.keywords")}</th><th>{t("categories.objects")}</th><th>{t("categories.grants")}</th><th /></tr></thead>
          <tbody>
            {live.map((c) => (
              <tr key={c.id}>
                <td><strong>{c.name}</strong>{c.description && <div className="muted small">{c.description}</div>}</td>
                <td>{c.keywords.join(", ") || "—"}</td>
                <td>{c.objects}</td>
                <td>{c.grants}</td>
                <td className="actions">
                  <button type="button" className="link" onClick={() => edit(c)}>{t("categories.editKeywords")}</button>
                  <button type="button" className="link" onClick={() => retire(c)}>{t("categories.retire")}</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form className="grid" onSubmit={add} aria-label={t("categories.add")}>
        <label>{t("categories.name")}<input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required /></label>
        <label>{t("categories.keywords")}<input value={form.keywords} onChange={(e) => setForm({ ...form, keywords: e.target.value })} /></label>
        <label className="wide">{t("categories.description")}<input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
        <div className="wide"><button type="submit">{t("categories.addButton")}</button></div>
      </form>

      <h2>{t("categories.objectsTitle")}</h2>
      <p className="muted">{t("categories.objectsIntro")}</p>
      <div className="actions">
        <label>{t("grants.dataSource")}
          <select value={ds} onChange={(e) => setDs(e.target.value)}>
            {sources.data?.filter((s) => s.status !== "tombstoned").map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
          </select>
        </label>
        <button type="button" disabled={!ds} onClick={() => void run(api(`/api/admin/data-sources/${encodeURIComponent(ds)}/categories/from-schemas`, { method: "POST" }),
          (r) => t("categories.fromSchemasDone", { count: (r as unknown[]).length }))}>{t("categories.fromSchemas")}</button>
        <button type="button" disabled={!ds || live.length === 0} onClick={() => void run(api(`/api/admin/data-sources/${encodeURIComponent(ds)}/categories/propose`, { method: "POST" }),
          (r) => { const x = r as { proposed: number; none: number; confirmed_kept: number }; return t("categories.proposeDone", { proposed: x.proposed, none: x.none, kept: x.confirmed_kept }); })}>{t("categories.propose")}</button>
      </div>
      {missing > 0 && <p className="note warn">{t("categories.missing", { count: missing })}</p>}
      <Busy loading={objects.loading} />
      {!!objects.data?.length && (
        <table>
          <thead><tr><th>{t("categories.object")}</th><th>{t("review.gaiaSays")}</th><th>{t("categories.category")}</th><th>{t("categories.decided")}</th></tr></thead>
          <tbody>
            {objects.data.map((o) => (
              <tr key={o.id}>
                <td>{o.name} <span className="muted small">{o.kind}</span></td>
                <td>{o.business_name ?? "—"}</td>
                <td>
                  <select aria-label={t("categories.categoryOf", { name: o.name })} value={o.category?.name ?? ""} onChange={(e) => place(o, e.target.value)}>
                    <option value="">—</option>
                    {live.map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
                  </select>
                </td>
                <td>
                  {o.category
                    ? <span className={`status placement-${o.category.status}`}>{o.category.status === "confirmed"
                        ? t("categories.confirmedBy", { who: o.category.decided_by, when: when(o.category.decided_at, i18n.language) })
                        : t("categories.proposedBecause", { why: o.category.reason ?? "" })}</span>
                    : <span className="warn small">{t("categories.nobody")}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
