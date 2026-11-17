import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type DataSourceView } from "../../api/client";
import { Busy, Checks, ErrorNote, Status, useLoad, when } from "../../components/util";

const EMPTY = { name: "", dialect: "postgres", host: "", port: "5432", database: "", username: "", password: "" };

export function DataSources() {
  const { t, i18n } = useTranslation();
  const { data, error, loading, reload } = useLoad(() => api<DataSourceView[]>("/api/admin/data-sources"));
  const [form, setForm] = useState(EMPTY);
  const [actionError, setActionError] = useState<Error | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  async function act(path: string, method = "POST") {
    setActionError(null);
    try {
      await api(path, { method });
      await reload();
    } catch (e) {
      setActionError(e as Error);
    }
  }

  async function add(event: FormEvent) {
    event.preventDefault();
    setActionError(null);
    try {
      await api("/api/admin/data-sources", {
        method: "POST",
        body: { ...form, port: Number(form.port) },
      });
      setForm(EMPTY);
      await reload();
    } catch (e) {
      setActionError(e as Error);
    }
  }

  const field = (key: keyof typeof EMPTY) => ({
    value: form[key],
    onChange: (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value }),
  });

  return (
    <section>
      <h1>{t("dataSources.title")}</h1>
      <p className="muted">{t("dataSources.intro")}</p>
      <Busy loading={loading} />
      <ErrorNote error={error ?? actionError} />
      <table>
        <thead>
          <tr>
            <th>{t("dataSources.name")}</th><th>{t("dataSources.dialect")}</th><th>{t("dataSources.where")}</th>
            <th>{t("dataSources.origin")}</th><th>{t("common.status")}</th><th>{t("dataSources.probed")}</th><th />
          </tr>
        </thead>
        <tbody>
          {data?.map((d) => (
            <tr key={d.name}>
              <td>
                <button type="button" className="link" onClick={() => setOpen(open === d.name ? null : d.name)}>{d.name}</button>
                {open === d.name && <Checks checks={d.probe} />}
              </td>
              <td>{d.dialect}</td>
              <td>{d.host}:{d.port}/{d.database} · {d.username}</td>
              <td>{t(`dataSources.origins.${d.origin}`, { defaultValue: d.origin })}</td>
              <td><Status value={d.status} /></td>
              <td>{when(d.probed_at, i18n.language)}</td>
              <td className="actions">
                {d.origin !== "config" && (
                  <>
                    <button type="button" onClick={() => act(`/api/admin/data-sources/${d.name}/probe`)}>{t("dataSources.probe")}</button>
                    {d.status === "disabled"
                      ? <button type="button" onClick={() => act(`/api/admin/data-sources/${d.name}/enable`)}>{t("common.enable")}</button>
                      : <button type="button" onClick={() => act(`/api/admin/data-sources/${d.name}/disable`)}>{t("common.disable")}</button>}
                    <button type="button" className="danger"
                            onClick={() => window.confirm(t("dataSources.confirmRemove", { name: d.name })) && act(`/api/admin/data-sources/${d.name}`, "DELETE")}>
                      {t("common.remove")}
                    </button>
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <h2>{t("dataSources.add")}</h2>
      <p className="muted">{t("dataSources.addIntro")}</p>
      <form className="grid" onSubmit={add} aria-label={t("dataSources.add")}>
        <label>{t("dataSources.name")}<input {...field("name")} required /></label>
        <label>
          {t("dataSources.dialect")}
          <select {...field("dialect")}>
            <option value="postgres">PostgreSQL</option>
            <option value="mssql">SQL Server</option>
          </select>
        </label>
        <label>{t("dataSources.host")}<input {...field("host")} required /></label>
        <label>{t("dataSources.port")}<input {...field("port")} inputMode="numeric" required /></label>
        <label>{t("dataSources.database")}<input {...field("database")} required /></label>
        <label>{t("dataSources.username")}<input {...field("username")} required /></label>
        <label>{t("dataSources.password")}<input type="password" autoComplete="new-password" {...field("password")} required /></label>
        <button type="submit">{t("dataSources.addButton")}</button>
      </form>
    </section>
  );
}
