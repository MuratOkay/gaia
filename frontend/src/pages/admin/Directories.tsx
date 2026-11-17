import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type DirectoryView } from "../../api/client";
import { Busy, Checks, ErrorNote, Status, useLoad, when } from "../../components/util";

const EMPTY = {
  name: "",
  urls: "ldaps://",
  start_tls: false,
  tls_verify: true,
  ca_file: "",
  flavor: "ad",
  bind_dn: "",
  base_dn: "",
  user_base: "",
  user_filter: "",
  login_attribute: "",
  role_kind: "group",
  group_base: "",
  group_filter: "",
  nested: true,
  nested_strategy: "client",
  attribute: "department",
  ou_base: "",
  password: "",
};

// What the form holds -> the settings the API validates (gaia.auth.ldap.LdapSettings).
export function toSettings(f: typeof EMPTY): Record<string, unknown> {
  const blank = (v: string) => (v.trim() ? v.trim() : undefined);
  const role: Record<string, unknown> = { kind: f.role_kind };
  if (f.role_kind === "group") {
    Object.assign(role, { group_base: blank(f.group_base), group_filter: blank(f.group_filter), nested: f.nested, nested_strategy: f.nested_strategy });
  } else if (f.role_kind === "attribute") {
    role.attribute = blank(f.attribute);
  } else {
    role.ou_base = blank(f.ou_base);
  }
  return {
    urls: f.urls.split(/[\s,]+/).filter(Boolean),
    start_tls: f.start_tls,
    tls_verify: f.tls_verify,
    ca_file: blank(f.ca_file),
    flavor: f.flavor,
    bind_dn: f.bind_dn.trim(),
    base_dn: f.base_dn.trim(),
    user_base: blank(f.user_base),
    user_filter: blank(f.user_filter),
    login_attribute: blank(f.login_attribute),
    role_source: role,
  };
}

export function Directories() {
  const { t, i18n } = useTranslation();
  const { data, error, loading, reload } = useLoad(() => api<DirectoryView[]>("/api/admin/directories"));
  const [form, setForm] = useState(EMPTY);
  const [actionError, setActionError] = useState<Error | null>(null);
  const [working, setWorking] = useState<string | null>(null);

  async function act(name: string, what: string) {
    setActionError(null);
    setWorking(`${name}/${what}`);
    try {
      await api(`/api/admin/directories/${encodeURIComponent(name)}/${what}`, { method: "POST" });
      await reload();
    } catch (e) {
      setActionError(e as Error);
    } finally {
      setWorking(null);
    }
  }

  async function add(event: FormEvent) {
    event.preventDefault();
    setActionError(null);
    try {
      await api("/api/admin/directories", {
        method: "POST",
        body: { name: form.name.trim(), kind: "ldap", settings: toSettings(form), password: form.password },
      });
      setForm(EMPTY);
      await reload();
    } catch (e) {
      setActionError(e as Error);
    }
  }

  const text = (key: keyof typeof EMPTY) => ({
    value: String(form[key]),
    onChange: (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value }),
  });
  const flag = (key: "start_tls" | "tls_verify" | "nested") => ({
    checked: form[key],
    onChange: (e: { target: { checked: boolean } }) => setForm({ ...form, [key]: e.target.checked }),
  });

  return (
    <section>
      <h1>{t("directories.title")}</h1>
      <p className="muted">{t("directories.intro")}</p>
      <Busy loading={loading} />
      <ErrorNote error={error ?? actionError} />
      {data?.length === 0 && <p className="note">{t("directories.none")}</p>}
      {data?.map((d) => (
        <article key={d.name} className="card">
          <h2>
            {d.name} <Status value={d.status} />
          </h2>
          <p className="muted">
            {t("directories.probed")}: {when(d.probed_at, i18n.language)} · {t("directories.synced")}: {when(d.synced_at, i18n.language)}
          </p>
          <Checks checks={d.probe} />
          {d.sync && (
            <p>{t("directories.syncResult", { users: d.sync.users, roles: d.sync.roles, gone: d.sync.roles_gone, disabled: d.sync.accounts_disabled })}</p>
          )}
          <div className="actions">
            <button type="button" disabled={!!working} onClick={() => act(d.name, "probe")}>{t("directories.probe")}</button>
            <button type="button" disabled={!!working || d.status !== "verified"} onClick={() => act(d.name, "sync")}>{t("directories.sync")}</button>
            {d.status === "disabled"
              ? <button type="button" onClick={() => act(d.name, "enable")}>{t("common.enable")}</button>
              : <button type="button" className="danger" onClick={() => window.confirm(t("directories.confirmDisable", { name: d.name })) && act(d.name, "disable")}>{t("common.disable")}</button>}
          </div>
        </article>
      ))}

      <h2>{t("directories.add")}</h2>
      <p className="muted">{t("directories.addIntro")}</p>
      <form className="grid" onSubmit={add} aria-label={t("directories.add")}>
        <label>{t("directories.name")}<input {...text("name")} required /></label>
        <label>
          {t("directories.flavor")}
          <select {...text("flavor")}>
            <option value="ad">{t("directories.flavors.ad")}</option>
            <option value="openldap">{t("directories.flavors.openldap")}</option>
          </select>
        </label>
        <label className="wide">{t("directories.urls")}<input {...text("urls")} required placeholder="ldaps://dc1.example.com" /></label>
        <label className="check"><input type="checkbox" {...flag("start_tls")} />{t("directories.startTls")}</label>
        <label className="check"><input type="checkbox" {...flag("tls_verify")} />{t("directories.tlsVerify")}</label>
        <label>{t("directories.caFile")}<input {...text("ca_file")} /></label>
        <label className="wide">{t("directories.bindDn")}<input {...text("bind_dn")} required /></label>
        <label>{t("directories.password")}<input type="password" autoComplete="new-password" {...text("password")} required /></label>
        <label className="wide">{t("directories.baseDn")}<input {...text("base_dn")} required placeholder="dc=example,dc=com" /></label>
        <label className="wide">{t("directories.userBase")}<input {...text("user_base")} /></label>
        <label className="wide">{t("directories.userFilter")}<input {...text("user_filter")} /></label>
        <label>{t("directories.loginAttribute")}<input {...text("login_attribute")} /></label>
        <fieldset className="wide">
          <legend>{t("directories.roles")}</legend>
          <p className="muted">{t("directories.rolesIntro")}</p>
          <label>
            {t("directories.roleKind")}
            <select {...text("role_kind")}>
              <option value="group">{t("directories.roleKinds.group")}</option>
              <option value="attribute">{t("directories.roleKinds.attribute")}</option>
              <option value="ou">{t("directories.roleKinds.ou")}</option>
            </select>
          </label>
          {form.role_kind === "group" && (
            <>
              <label>{t("directories.groupBase")}<input {...text("group_base")} /></label>
              <label>{t("directories.groupFilter")}<input {...text("group_filter")} /></label>
              <label className="check"><input type="checkbox" {...flag("nested")} />{t("directories.nested")}</label>
              <label>
                {t("directories.nestedStrategy")}
                <select {...text("nested_strategy")}>
                  <option value="client">{t("directories.strategies.client")}</option>
                  <option value="ad_in_chain">{t("directories.strategies.ad_in_chain")}</option>
                </select>
              </label>
            </>
          )}
          {form.role_kind === "attribute" && (
            <label>{t("directories.attribute")}<input {...text("attribute")} required /></label>
          )}
          {form.role_kind === "ou" && (
            <label>{t("directories.ouBase")}<input {...text("ou_base")} /></label>
          )}
        </fieldset>
        <button type="submit">{t("directories.addButton")}</button>
      </form>
    </section>
  );
}
