import { useState } from "react";
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router-dom";

import { api, type AccountView, type AuditEvent, type AuditSummary, type GrantView, url } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";
import { AccountTable } from "../admin/Accounts";

function AuditNav() {
  const { t } = useTranslation();
  return (
    <nav className="subnav" aria-label={t("audit.nav")}>
      <NavLink to="/audit" end>{t("audit.summary")}</NavLink>
      <NavLink to="/audit/access">{t("audit.access")}</NavLink>
      <NavLink to="/audit/pii">{t("audit.pii")}</NavLink>
      <NavLink to="/audit/grants">{t("audit.grants")}</NavLink>
      <NavLink to="/audit/events">{t("audit.events")}</NavLink>
    </nav>
  );
}

function Csv({ path, query }: { path: string; query?: Record<string, string | undefined> }) {
  const { t } = useTranslation();
  return <a className="button" href={url(path, { ...query, format: "csv" })} download>{t("audit.csv")}</a>;
}

export function AuditSummaryPage() {
  const { t } = useTranslation();
  const { data, error, loading } = useLoad(() => api<AuditSummary>("/api/audit/summary"));
  return (
    <section>
      <AuditNav />
      <h1>{t("audit.summaryTitle")}</h1>
      <p className="muted">{t("audit.intro")}</p>
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data && (
        <>
          <article className={`card ${data.chain.ok ? "" : "alarm"}`}>
            <h2>{data.chain.ok ? t("audit.chainOk") : t("audit.chainBroken")}</h2>
            <p>{t("audit.chainEvents", { count: data.chain.events })}</p>
            {!data.chain.ok && <p className="error">{t("audit.brokenAt", { seq: data.chain.broken_at, reason: data.chain.reason })}</p>}
            <p>{t("audit.head")}: <code>{data.chain.head}</code></p>
            <p className="muted">{t("audit.headWhy")}</p>
          </article>
          <table>
            <tbody>
              <tr><th>{t("audit.accounts")}</th><td>{data.accounts}</td></tr>
              {(["user", "sme", "admin", "auditor"] as const).map((r) => (
                <tr key={r}><th>{t(`role.${r}`)}</th><td>{data.by_role[r]}</td></tr>
              ))}
              <tr><th>{t("audit.piiProcessors")}</th><td>{data.pii_processors}</td></tr>
              <tr><th>{t("audit.localSignIns")}</th><td>{data.local_sign_ins}</td></tr>
              <tr><th>{t("audit.refusals")}</th><td>{data.refusals}</td></tr>
            </tbody>
          </table>
          {data.conflicts.length > 0 && (
            <>
              <h2>{t("audit.conflicts")}</h2>
              <ul>{data.conflicts.map((c) => <li key={c} className="warn">{c}</li>)}</ul>
            </>
          )}
        </>
      )}
    </section>
  );
}

export function AuditAccess() {
  const { t } = useTranslation();
  const { data, error, loading } = useLoad(() => api<AccountView[]>("/api/audit/access"));
  return (
    <section>
      <AuditNav />
      <h1>{t("audit.accessTitle")}</h1>
      <p className="muted">{t("audit.accessIntro")}</p>
      <Csv path="/api/audit/access" />
      <Busy loading={loading} />
      <ErrorNote error={error} />
      {data && <AccountTable rows={data} />}
    </section>
  );
}

export function AuditPii() {
  const { t } = useTranslation();
  const { data, error, loading } = useLoad(() =>
    api<{ login: string; directory: string | null; display_name: string | null; enabled: boolean; through: string[] }[]>("/api/audit/pii-processors"));
  return (
    <section>
      <AuditNav />
      <h1>{t("audit.piiTitle")}</h1>
      <p className="muted">{t("audit.piiIntro")}</p>
      <Csv path="/api/audit/pii-processors" />
      <Busy loading={loading} />
      <ErrorNote error={error} />
      <table>
        <thead><tr><th>{t("accounts.account")}</th><th>{t("audit.through")}</th><th>{t("common.status")}</th></tr></thead>
        <tbody>
          {data?.map((p) => (
            <tr key={`${p.directory}/${p.login}`}>
              <td>{p.display_name ?? p.login} <span className="muted">{p.directory}/{p.login}</span></td>
              <td>{p.through.join(", ")}</td>
              <td>{p.enabled ? t("accounts.enabled") : t("accounts.disabled")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function AuditGrants() {
  const { t, i18n } = useTranslation();
  const { data, error, loading } = useLoad(() => api<GrantView[]>("/api/audit/grants"));
  return (
    <section>
      <AuditNav />
      <h1>{t("audit.grantsTitle")}</h1>
      <Csv path="/api/audit/grants" />
      <Busy loading={loading} />
      <ErrorNote error={error} />
      <table>
        <thead>
          <tr><th>{t("grants.privilege")}</th><th>{t("grants.subject")}</th><th>{t("grants.dataSource")}</th><th>{t("audit.granted")}</th><th>{t("audit.revoked")}</th><th>{t("grants.note")}</th></tr>
        </thead>
        <tbody>
          {data?.map((g) => (
            <tr key={g.id} className={g.revoked_at ? "revoked" : undefined}>
              <td>{t(`privilege.${g.privilege}`)}</td>
              <td>{g.subject}</td>
              <td>{g.data_source ?? "—"}</td>
              <td>{g.granted_by} · {when(g.granted_at, i18n.language)}</td>
              <td>{g.revoked_at ? `${g.revoked_by} · ${when(g.revoked_at, i18n.language)}` : "—"}</td>
              <td>{g.note ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function AuditEvents() {
  const { t, i18n } = useTranslation();
  const [action, setAction] = useState("");
  const { data, error, loading } = useLoad(
    () => api<AuditEvent[]>("/api/audit/events", { query: { action: action || undefined, limit: 500 } }),
    [action],
  );
  return (
    <section>
      <AuditNav />
      <h1>{t("audit.eventsTitle")}</h1>
      <label>
        {t("audit.action")}
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">{t("audit.allActions")}</option>
          {["sign_in", "sign_in_refused", "forbidden", "grant_added", "grant_revoked", "directory_added",
            "directory_changed", "directory_probed", "directory_synced", "local_password_set",
            "local_setup_code_issued", "privileges_changed", "session_ended"].map((a) => (
            <option key={a} value={a}>{t(`audit.actions.${a}`)}</option>
          ))}
        </select>
      </label>
      <Csv path="/api/audit/events" query={{ action: action || undefined }} />
      <Busy loading={loading} />
      <ErrorNote error={error} />
      <table>
        <thead><tr><th>#</th><th>{t("audit.when")}</th><th>{t("audit.who")}</th><th>{t("audit.action")}</th><th>{t("audit.subject")}</th><th>{t("audit.detail")}</th></tr></thead>
        <tbody>
          {data?.map((e) => (
            <tr key={e.seq}>
              <td>{e.seq}</td>
              <td>{when(e.at, i18n.language)}</td>
              <td>{e.actor ?? "—"}{e.ip && <span className="muted"> · {e.ip}</span>}</td>
              <td>{t(`audit.actions.${e.action}`, { defaultValue: e.action })}</td>
              <td>{e.subject ?? ""}</td>
              <td><code className="small">{JSON.stringify(e.detail)}</code></td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
