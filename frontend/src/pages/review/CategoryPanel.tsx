import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type TableReview } from "../../api/client";
import { ErrorNote, when } from "../../components/util";
import { enc } from "./common";

/** What the object's data is (5C): the SME confirms its category. Who may see that
 *  category is the administrator's decision (D70). */
export function CategoryPanel({ ds, table, onDone }: { ds: string; table: TableReview; onDone: () => void }) {
  const { t, i18n } = useTranslation();
  const current = table.category ?? null;
  const [choice, setChoice] = useState(current?.name ?? table.categories?.[0]?.name ?? "");
  const [error, setError] = useState<Error | null>(null);
  useEffect(() => {
    setChoice(current?.name ?? table.categories?.[0]?.name ?? "");
  }, [current, table.categories]);
  if (!table.categories?.length) return <p className="muted">{t("review.noCategories")}</p>;

  async function confirm() {
    setError(null);
    try {
      await api(`/api/review/${enc(ds)}/tables/${table.id}/category`, { method: "POST", body: { category: choice } });
      onDone();
    } catch (e) {
      setError(e as Error);
    }
  }

  return (
    <div className="card" aria-label={t("review.categoryTitle")}>
      <h2>{t("review.categoryTitle")}</h2>
      <p className="muted small">{t("review.categoryIntro")}</p>
      <p>
        {current
          ? current.status === "confirmed"
            ? t("review.categoryConfirmed", { name: current.name, who: current.decided_by, when: when(current.decided_at, i18n.language) })
            : t("review.categoryProposed", { name: current.name, why: current.reason ?? "" })
          : <span className="warn">{t("review.categoryNone")}</span>}
      </p>
      <div className="actions">
        <select aria-label={t("review.categoryTitle")} value={choice} onChange={(e) => setChoice(e.target.value)}>
          {table.categories.map((c) => <option key={c.id} value={c.name}>{c.name}</option>)}
        </select>
        <button type="button" onClick={confirm} disabled={!choice}>{t("review.categoryConfirm")}</button>
      </div>
      <ErrorNote error={error} />
    </div>
  );
}
