import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type CategoryView, type DataSourceView, type DirectoryRoleView, type DirectoryView, type GrantView, type RoleSuggestion } from "../../api/client";
import { Busy, ErrorNote, useLoad, when } from "../../components/util";

export const PRIVILEGES = ["user", "sme", "admin", "auditor", "pii_processor", "category"] as const;

function GrantControl({ sources, categories, suggested, onGrant }: {
  sources: string[]; categories: string[]; suggested?: string[];
  onGrant: (privilege: string, dataSource?: string, category?: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [privilege, setPrivilege] = useState<string>(suggested?.length ? "category" : "user");
  const [source, setSource] = useState<string>(sources[0] ?? "");
  const [category, setCategory] = useState<string>(suggested?.[0] ?? categories[0] ?? "");
  useEffect(() => setSource((s) => s || sources[0] || ""), [sources]);
  useEffect(() => setCategory((c) => c || suggested?.[0] || categories[0] || ""), [categories, suggested]);
  return (
    <span className="grant-control">
      <select aria-label={t("grants.privilege")} value={privilege} onChange={(e) => setPrivilege(e.target.value)}>
        {PRIVILEGES.map((p) => <option key={p} value={p}>{t(`privilege.${p}`)}</option>)}
      </select>
      {privilege === "sme" && (
        <select aria-label={t("grants.dataSource")} value={source} onChange={(e) => setSource(e.target.value)}>
          {sources.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      )}
      {privilege === "category" && (
        <select aria-label={t("grants.category")} value={category} onChange={(e) => setCategory(e.target.value)}>
          {categories.map((c) => <option key={c} value={c}>{c}{suggested?.includes(c) ? ` — ${t("grants.suggestedShort")}` : ""}</option>)}
        </select>
      )}
      <button type="button" onClick={() => onGrant(privilege, privilege === "sme" ? source : undefined, privilege === "category" ? category : undefined)}>{t("grants.grant")}</button>
    </span>
  );
}

export function Grants() {
  const { t, i18n } = useTranslation();
  const directories = useLoad(() => api<DirectoryView[]>("/api/admin/directories"));
  const sources = useLoad(() => api<DataSourceView[]>("/api/admin/data-sources"));
  const categories = useLoad(() => api<CategoryView[]>("/api/admin/categories"));
  const suggestions = useLoad(() => api<RoleSuggestion[]>("/api/admin/category-suggestions"));
  const [directory, setDirectory] = useState<string>("");
  const [showRevoked, setShowRevoked] = useState(false);
  const roles = useLoad(
    () => (directory ? api<DirectoryRoleView[]>(`/api/admin/directories/${encodeURIComponent(directory)}/roles`) : Promise.resolve([])),
    [directory],
  );
  const grants = useLoad(() => api<GrantView[]>("/api/admin/grants", { query: { include_revoked: showRevoked } }), [showRevoked]);
  const [error, setError] = useState<Error | null>(null);
  const [account, setAccount] = useState({ login: "", privilege: "user", data_source: "", category: "" });

  useEffect(() => {
    if (!directory && directories.data?.length) setDirectory(directories.data[0].name);
  }, [directories.data, directory]);

  const names = (sources.data ?? []).filter((s) => s.status !== "tombstoned").map((s) => s.name);
  const categoryNames = (categories.data ?? []).map((c) => c.name);
  const suggestedFor = (roleId: string) => suggestions.data?.find((x) => x.directory_role_id === roleId)?.categories;

  async function grant(body: Record<string, unknown>) {
    setError(null);
    try {
      await api("/api/admin/grants", { method: "POST", body });
      await grants.reload();
    } catch (e) {
      setError(e as Error);
    }
  }

  async function revoke(g: GrantView) {
    const note = window.prompt(t("grants.revokeWhy", { privilege: t(`privilege.${g.privilege}`), subject: g.subject }));
    if (note === null) return;
    setError(null);
    try {
      await api(`/api/admin/grants/${g.id}`, { method: "DELETE", query: { note: note || undefined } });
      await grants.reload();
    } catch (e) {
      setError(e as Error);
    }
  }

  async function grantAccount(event: FormEvent) {
    event.preventDefault();
    await grant({ privilege: account.privilege, login: account.login.trim(), directory: directory || undefined,
                  data_source: account.privilege === "sme" ? account.data_source || names[0] : undefined,
                  category: account.privilege === "category" ? account.category || categoryNames[0] : undefined });
  }

  return (
    <section>
      <h1>{t("grants.title")}</h1>
      <p className="muted">{t("grants.intro")}</p>
      <ErrorNote error={directories.error ?? roles.error ?? grants.error ?? error} />

      <h2>{t("grants.byRole")}</h2>
      {directories.data?.length === 0 && <p className="note">{t("grants.noDirectory")}</p>}
      {!!directories.data?.length && (
        <label>
          {t("grants.directory")}
          <select value={directory} onChange={(e) => setDirectory(e.target.value)}>
            {directories.data.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
          </select>
        </label>
      )}
      <Busy loading={roles.loading} />
      {roles.data?.length === 0 && directory && <p className="note">{t("grants.noRoles")}</p>}
      {!!roles.data?.length && (
        <table>
          <thead><tr><th>{t("grants.role")}</th><th>{t("grants.source")}</th><th>{t("grants.members")}</th><th /></tr></thead>
          <tbody>
            {roles.data.map((r) => (
              <tr key={r.id}>
                <td>
                  {r.name}{r.description && <span className="muted"> — {r.description}</span>}
                  {!!suggestedFor(r.id)?.length && <div className="muted small">{t("grants.suggested", { categories: suggestedFor(r.id)!.join(", ") })}</div>}
                </td>
                <td>{t(`directories.roleKinds.${r.source}`)}</td>
                <td>{r.members ?? "—"}</td>
                <td><GrantControl sources={names} categories={categoryNames} suggested={suggestedFor(r.id)}
                                  onGrant={(privilege, ds, category) => grant({ privilege, directory_role_id: r.id, data_source: ds, category })} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>{t("grants.byAccount")}</h2>
      <form className="inline" onSubmit={grantAccount} aria-label={t("grants.byAccount")}>
        <label>{t("grants.login")}<input value={account.login} onChange={(e) => setAccount({ ...account, login: e.target.value })} required /></label>
        <label>
          {t("grants.privilege")}
          <select value={account.privilege} onChange={(e) => setAccount({ ...account, privilege: e.target.value })}>
            {PRIVILEGES.map((p) => <option key={p} value={p}>{t(`privilege.${p}`)}</option>)}
          </select>
        </label>
        {account.privilege === "sme" && (
          <label>
            {t("grants.dataSource")}
            <select value={account.data_source} onChange={(e) => setAccount({ ...account, data_source: e.target.value })}>
              {names.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        )}
        {account.privilege === "category" && (
          <label>
            {t("grants.category")}
            <select value={account.category} onChange={(e) => setAccount({ ...account, category: e.target.value })}>
              {categoryNames.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
        )}
        <button type="submit">{t("grants.grant")}</button>
      </form>

      <h2>{t("grants.current")}</h2>
      <label className="check"><input type="checkbox" checked={showRevoked} onChange={(e) => setShowRevoked(e.target.checked)} />{t("grants.showRevoked")}</label>
      <Busy loading={grants.loading} />
      <table>
        <thead>
          <tr><th>{t("grants.privilege")}</th><th>{t("grants.subject")}</th><th>{t("grants.scope")}</th><th>{t("grants.grantedBy")}</th><th>{t("grants.note")}</th><th /></tr>
        </thead>
        <tbody>
          {grants.data?.map((g) => (
            <tr key={g.id} className={g.revoked_at ? "revoked" : undefined}>
              <td>{t(`privilege.${g.privilege}`)}</td>
              <td>{g.subject}</td>
              <td>{g.data_source ?? g.category ?? "—"}</td>
              <td>{g.granted_by} · {when(g.granted_at, i18n.language)}</td>
              <td>{g.note ?? ""}</td>
              <td>
                {g.revoked_at
                  ? <span className="muted">{t("grants.revokedBy", { who: g.revoked_by, when: when(g.revoked_at, i18n.language) })}</span>
                  : <button type="button" className="danger" onClick={() => revoke(g)}>{t("grants.revoke")}</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
