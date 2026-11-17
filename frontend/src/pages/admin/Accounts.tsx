import { useTranslation } from "react-i18next";

import { api, type AccountView } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";

// Accounts as the administrator and the auditor see them: directory roles, the Gaia roles
// in force (grants applied, separation of duties applied), and every conflict.
export function AccountTable({ rows }: { rows: AccountView[] }) {
  const { t, i18n } = useTranslation();
  return (
    <table>
      <thead>
        <tr>
          <th>{t("accounts.account")}</th><th>{t("accounts.directoryRoles")}</th><th>{t("accounts.gaiaRoles")}</th>
          <th>{t("accounts.pii")}</th><th>{t("accounts.lastSignIn")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((a) => (
          <tr key={`${a.directory}/${a.login}`} className={a.enabled ? undefined : "revoked"}>
            <td>
              <strong>{a.display_name ?? a.login}</strong>
              <div className="muted">{a.local ? t("accounts.local") : `${a.directory}/${a.login}`}{!a.enabled && ` · ${t("accounts.disabled")}`}</div>
            </td>
            <td>{a.directory_roles.join(", ") || "—"}</td>
            <td>
              {a.roles.map((r) => t(`role.${r}`)).join(", ") || <span className="muted">{t("accounts.noRole")}</span>}
              {a.sme_sources.length > 0 && <div className="muted">{t("accounts.smeOf", { sources: a.sme_sources.join(", ") })}</div>}
              {a.conflicts.map((c) => <div key={c} className="warn">{c}</div>)}
            </td>
            <td>{a.pii_processor ? t("common.yes") : t("common.no")}</td>
            <td>{when(a.last_sign_in_at, i18n.language)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Accounts() {
  const { t } = useTranslation();
  const { data, error, loading } = useLoad(() => api<AccountView[]>("/api/admin/accounts"));
  return (
    <section>
      <h1>{t("accounts.title")}</h1>
      <p className="muted">{t("accounts.intro")}</p>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data && <AccountTable rows={data} />}
    </section>
  );
}
