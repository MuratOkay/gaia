import { useTranslation } from "react-i18next";

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
            <p>{t(`home.role.${r}`)}</p>
            {r === "sme" && me.sme_sources.length > 0 && (
              <p className="muted">{t("home.smeSources", { count: me.sme_sources.length })}</p>
            )}
          </li>
        ))}
      </ul>
      {me.pii_processor && <p className="note">{t("home.pii")}</p>}
      {me.local && <p className="note warn">{t("home.local")}</p>}
      {me.conflicts.map((c) => (
        <p key={c} className="note warn">{t("home.conflict")}: {c}</p>
      ))}
      <p className="muted">{t("home.next")}</p>
    </section>
  );
}
