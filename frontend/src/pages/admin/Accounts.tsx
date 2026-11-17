import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type AccountView } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";

// Accounts as the administrator and the auditor see them: directory roles, the Gaia roles
// in force (grants applied, separation of duties applied), and every conflict.
export function AccountTable({ rows, onIdentity }: {
  rows: AccountView[];
  /** the administrator's decision when another directory object carries a login (D78) */
  onIdentity?: (a: AccountView, action: "relink" | "renew") => void;
}) {
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
              {!!a.categories?.length && <div className="muted">{t("accounts.reads", { categories: a.categories.join(", ") })}</div>}
              {a.conflicts.map((c) => <div key={c} className="warn">{c}</div>)}
              {a.identity_changed_at && (
                <div className="warn" role="alert">
                  {t("accounts.identityChanged", { when: when(a.identity_changed_at, i18n.language) })}
                  {onIdentity && (
                    <div className="actions">
                      <button type="button" onClick={() => onIdentity(a, "relink")} title={t("accounts.relinkHelp")}>{t("accounts.relink")}</button>
                      <button type="button" className="danger" onClick={() => onIdentity(a, "renew")} title={t("accounts.renewHelp")}>{t("accounts.renew")}</button>
                    </div>
                  )}
                </div>
              )}
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
  const { data, error, loading, reload } = useLoad(() => api<AccountView[]>("/api/admin/accounts"));
  const [actionError, setActionError] = useState<Error | null>(null);

  async function decide(a: AccountView, action: "relink" | "renew") {
    const who = `${a.directory}/${a.login}`;
    if (!window.confirm(t(action === "relink" ? "accounts.confirmRelink" : "accounts.confirmRenew", { who }))) return;
    setActionError(null);
    try {
      await api("/api/admin/accounts/identity", { method: "POST", body: { directory: a.directory, login: a.login, action } });
      await reload();
    } catch (e) {
      setActionError(e as Error);
    }
  }

  return (
    <section>
      <h1>{t("accounts.title")}</h1>
      <p className="muted">{t("accounts.intro")}</p>
      <Busy loading={loading} />
      <ErrorNote error={error ?? actionError} />
      {data && <AccountTable rows={data} onIdentity={(a, action) => void decide(a, action)} />}
    </section>
  );
}
