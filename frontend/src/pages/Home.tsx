import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { Me } from "../api/client";

export function Home({ me }: { me: Me }) {
  const { t } = useTranslation();
  return (
    <section>
      <h1>{t("home.title", { name: me.display_name ?? me.login })}</h1>
      <ul className="cards">
        {me.roles.map((r) => (
          <li key={r}>
            <h2>{t(`role.${r}`)}</h2>
            <p>{r === "admin" && me.local ? t("home.adminLocal") : t(`home.role.${r}`)}</p>
            {r === "sme" && me.sme_sources.length > 0 && (
              <p className="muted">{t("home.smeSources", { count: me.sme_sources.length })}</p>
            )}
          </li>
        ))}
      </ul>
      {!me.local && me.roles.some((r) => r === "user" || r === "sme" || r === "admin") && (
        me.category_names?.length
          ? <p className="note">{t("home.categories", { names: me.category_names.join(", ") })}</p>
          : me.roles.includes("admin")
            ? <p className="note warn">{t("home.noCategoriesAdmin")} <Link to="/admin/grants">{t("chat.toGrants")}</Link></p>
            : <p className="note warn">{t("home.noCategories")}</p>
      )}
      {me.pii_processor && <p className="note">{t("home.pii")}</p>}
      {me.local && <p className="note warn">{t("home.local")}</p>}
      {me.conflicts.map((c) => (
        <p key={c} className="note warn">{t("home.conflict")}: {c}</p>
      ))}
      <p className="muted">{t("home.next")}</p>
    </section>
  );
}
