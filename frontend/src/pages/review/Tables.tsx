import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useParams } from "react-router-dom";

import { api, type TableListEntry } from "../../api/client";
import { Busy, ErrorNote, useLoad } from "../../components/util";
import { enc, ReviewNav } from "./common";

/** Every table and view of the data source — a way to any column, queued or not. */
export function ReviewTables() {
  const { t } = useTranslation();
  const { ds = "" } = useParams();
  const { data, error, loading } = useLoad(() => api<TableListEntry[]>(`/api/review/${enc(ds)}/tables`), [ds]);
  const [find, setFind] = useState("");
  const needle = find.trim().toLowerCase();
  const rows = (data ?? []).filter((r) => !needle || r.name.toLowerCase().includes(needle) || (r.business_name ?? "").toLowerCase().includes(needle));
  return (
    <section>
      <ReviewNav ds={ds} />
      <h1>{t("review.tablesTitle", { ds })}</h1>
      <p className="muted">{t("review.tablesIntro")}</p>
      <label>{t("review.find")}<input value={find} onChange={(e) => setFind(e.target.value)} /></label>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data && (
        <table>
          <thead><tr><th>{t("review.table")}</th><th>{t("review.gaiaSays")}</th><th>{t("review.columns")}</th><th>{t("review.waiting")}</th><th>{t("review.decidedCount")}</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td><Link to={`/review/${enc(ds)}/tables/${r.id}`}>{r.name}</Link> <span className="muted small">{r.kind}</span></td>
                <td>{r.business_name ?? "—"}</td>
                <td>{r.columns}</td>
                <td>{r.queued || ""}</td>
                <td>{r.decided || ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
