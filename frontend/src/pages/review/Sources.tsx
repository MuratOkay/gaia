import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, type ReviewSource } from "../../api/client";
import { Busy, ErrorNote, useLoad } from "../../components/util";
import { enc } from "./common";

/** The data sources this SME reviews, with what is waiting in each. */
export function ReviewSources() {
  const { t } = useTranslation();
  const { data, error, loading } = useLoad(() => api<ReviewSource[]>("/api/review"));
  return (
    <section>
      <h1>{t("review.title")}</h1>
      <p className="muted">{t("review.intro")}</p>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data?.length === 0 && <p className="note">{t("review.noSources")}</p>}
      <ul className="cards">
        {data?.map((d) => (
          <li key={d.id}>
            <h2><Link to={`/review/${enc(d.name)}`}>{d.name}</Link></h2>
            <p>{t("review.openCount", { count: d.open })}</p>
            <p className="muted">{t("review.reviewedCount", { count: d.reviewed })}</p>
            {d.stale > 0 && (
              <p className="warn"><Link to={`/review/${enc(d.name)}?status=stale`}>{t("review.staleCount", { count: d.stale })}</Link></p>
            )}
            {(d.feedback_open ?? 0) > 0 && (
              <p className="warn"><Link to={`/review/${enc(d.name)}/answers`}>{t("review.answers.count", { count: d.feedback_open })}</Link></p>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
