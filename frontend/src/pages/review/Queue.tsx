import { useTranslation } from "react-i18next";
import { Link, useParams, useSearchParams } from "react-router-dom";

import { api, type Queue, type QueueStatus } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";
import { Band, enc, MeaningStatus, Reasons, REASONS, ReviewNav } from "./common";

const PAGE = 50;
const TABS: QueueStatus[] = ["open", "done", "stale"];

/** A data source's review queue: the most important first, why each item is there. */
export function ReviewQueue() {
  const { t, i18n } = useTranslation();
  const { ds = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const status = (TABS.includes(params.get("status") as QueueStatus) ? params.get("status") : "open") as QueueStatus;
  const reason = params.get("reason") ?? "";
  const table = params.get("table") ?? "";
  const offset = Number(params.get("offset") ?? 0) || 0;
  const { data, error, loading } = useLoad(
    () => api<Queue>(`/api/review/${enc(ds)}/queue`, { query: { status, reason: reason || undefined, table: table || undefined, limit: PAGE, offset } }),
    [ds, status, reason, table, offset],
  );

  function set(changes: Record<string, string | undefined>) {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    if (!("offset" in changes)) next.delete("offset");
    setParams(next);
  }

  return (
    <section>
      <ReviewNav ds={ds} />
      <h1>{t("review.queueTitle", { ds })}</h1>
      <p className="muted">{t(`review.tabHelp.${status}`)}</p>
      <div className="tabs" role="tablist" aria-label={t("review.tabsLabel")}>
        {TABS.map((s) => (
          <button key={s} type="button" role="tab" aria-selected={s === status} className={s === status ? "tab active" : "tab"} onClick={() => set({ status: s === "open" ? undefined : s })}>
            {t(`review.tabs.${s}`)}
          </button>
        ))}
      </div>
      <form className="inline" onSubmit={(e) => e.preventDefault()} aria-label={t("review.filters")}>
        {status !== "stale" && (
          <label>
            {t("review.reason")}
            <select value={reason} onChange={(e) => set({ reason: e.target.value || undefined })}>
              <option value="">{t("review.anyReason")}</option>
              {REASONS.filter((r) => r !== "stale_decision" && r !== "blind").map((r) => <option key={r} value={r}>{t(`review.reasons.${r}`)}</option>)}
            </select>
          </label>
        )}
        <label>
          {t("review.table")}
          <input placeholder={t("review.tablePlaceholder")} defaultValue={table} onBlur={(e) => set({ table: e.target.value.trim() || undefined })}
                 onKeyDown={(e) => { if (e.key === "Enter") set({ table: (e.target as HTMLInputElement).value.trim() || undefined }); }} />
        </label>
      </form>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data && data.items.length === 0 && <p className="note">{t(`review.empty.${status}`)}</p>}
      {data && data.items.length > 0 && (
        <>
          <p className="muted">{t("review.showing", { from: offset + 1, to: offset + data.items.length, total: data.total })}</p>
          <table>
            <thead>
              <tr>
                <th>{t("review.column")}</th>
                <th>{t("review.gaiaSays")}</th>
                <th>{t("review.score")}</th>
                <th>{status === "open" ? t("review.why") : t("review.decided")}</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((i) => (
                <tr key={`${i.id ?? ""}${i.column_id ?? i.table_id}`}>
                  <td>
                    {i.column_id
                      ? <Link to={`/review/${enc(ds)}/columns/${i.column_id}`}>{i.table}.<strong>{i.column}</strong></Link>
                      : <Link to={`/review/${enc(ds)}/tables/${i.table_id}`}>{i.table}</Link>}
                    <div className="muted small">{i.data_type}</div>
                  </td>
                  <td>{i.business_name ?? "—"}{i.role && <span className="muted"> · {i.role}</span>}</td>
                  <td><Band band={i.band} score={i.score} /></td>
                  <td>
                    {status === "open"
                      ? <Reasons reasons={i.reasons} />
                      : <><MeaningStatus status={i.status} /> {i.reviewed_by && <span className="muted">{t("review.by", { who: i.reviewed_by, when: when(i.reviewed_at ?? i.resolved_at, i18n.language) })}</span>}</>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="actions">
            <button type="button" disabled={offset === 0} onClick={() => set({ offset: String(Math.max(0, offset - PAGE)) })}>{t("review.previous")}</button>
            <button type="button" disabled={offset + data.items.length >= data.total} onClick={() => set({ offset: String(offset + PAGE) })}>{t("review.nextPage")}</button>
          </div>
        </>
      )}
    </section>
  );
}
