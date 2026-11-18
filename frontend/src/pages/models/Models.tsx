import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ModelPurpose, type ModelsView, type ModelView } from "../../api/client";
import { Busy, Checks, ErrorNote, Status, useLoad, when } from "../../components/util";

type TrialPurpose = ModelPurpose | "both";

/** Models (7D): the configured model and those registered beside it — what each serves, its
 * probe, its trials, and the switch with its rollback. Administrators change; auditors read. */
export function Models({ admin = false }: { admin?: boolean }) {
  const { t, i18n } = useTranslation();
  const { data, error, loading, reload } = useLoad(() => api<ModelsView>("/api/models"), []);
  const [failure, setFailure] = useState<Error | null>(null);
  const [done, setDone] = useState<string | null>(null);
  // the add form
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [modelId, setModelId] = useState("");
  const [keyEnv, setKeyEnv] = useState("");
  const [key, setKey] = useState("");
  const [locality, setLocality] = useState<"remote" | "local">("remote");
  // the trial and switch form
  const [chosen, setChosen] = useState("");
  const [ds, setDs] = useState("");
  const [trialPurpose, setTrialPurpose] = useState<TrialPurpose>("both");
  const [purpose, setPurpose] = useState<ModelPurpose>("answering");
  const [force, setForce] = useState(false);
  const [note, setNote] = useState("");

  async function act(work: () => Promise<unknown>, message: string) {
    setFailure(null);
    setDone(null);
    try {
      await work();
      setDone(message);
      await reload();
    } catch (e) {
      setFailure(e as Error);
    }
  }

  const models = Array.isArray(data?.models) ? data.models : [];
  const live = models.filter((m) => m.status !== "retired");
  const usable = live.filter((m) => m.usable);
  const sources = Array.isArray(data?.data_sources) ? data.data_sources : [];
  const source = ds || sources[0] || "";
  const target = chosen || usable.find((m) => m.origin !== "config")?.name || usable[0]?.name || "";
  const trials = Array.isArray(data?.trials) ? data.trials : [];
  const meanings = data?.meanings ?? {};
  const now = Date.now();

  function keyOf(m: ModelView): string {
    if (m.origin === "config") return t("models.keyConfig");
    if (m.key_env) return t("models.keyEnv", { name: m.key_env }) + (m.has_key ? "" : ` — ${t("models.keyUnset")}`);
    return m.has_key ? (m.locality === "local" ? t("models.keyNone") : t("models.keyStored")) : t("models.keyWiped");
  }

  return (
    <section aria-label={t("models.title")}>
      <h1>{t("models.title")}</h1>
      <p className="muted">{t("models.intro")}</p>
      <Busy loading={loading} />
      <ErrorNote error={failure ?? error} />
      {done && <p className="note" role="status">{done}</p>}

      {data && (
        <>
          <h2>{t("models.serving")}</h2>
          <table>
            <thead><tr><th>{t("models.purpose")}</th><th>{t("models.model")}</th><th>{t("models.since")}</th><th>{t("models.replacing")}</th><th /></tr></thead>
            <tbody>
              {data.purposes.map((p) => {
                const a = data.assignments[p];
                const canRollBack = !!a?.previous && !!a.rollback_until && new Date(a.rollback_until).getTime() > now;
                return (
                  <tr key={p}>
                    <td>{t(`models.purposes.${p}`)}</td>
                    <td><strong>{data.serving[p]}</strong>{a?.forced && <span className="muted"> · {t("models.forced")}</span>}</td>
                    <td>{a ? `${when(a.since, i18n.language)} · ${a.by}` : t("models.fromConfig")}</td>
                    <td>
                      {a?.previous ?? "—"}
                      {canRollBack && <div className="muted">{t("models.rollbackUntil", { date: when(a.rollback_until, i18n.language) })}</div>}
                      {a?.note && <div className="muted">{a.note}</div>}
                    </td>
                    <td>{admin && canRollBack && (
                      <button type="button" className="link" onClick={() => void act(() => api("/api/models/rollback", { method: "POST", body: { purpose: p } }), t("models.rolledBack", { purpose: t(`models.purposes.${p}`), model: a?.previous }))}>
                        {t("models.rollback", { model: a?.previous })}
                      </button>
                    )}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <h2>{t("models.registered")}</h2>
          <table>
            <thead><tr><th>{t("models.name")}</th><th>{t("models.modelId")}</th><th>{t("models.locality")}</th><th>{t("models.key")}</th><th>{t("queries.status")}</th><th>{t("models.serves")}</th><th /></tr></thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.name} className={m.status === "retired" ? "muted" : undefined}>
                  <td><strong>{m.name}</strong><div className="muted">{m.base_url}</div></td>
                  <td>{m.model_id}</td>
                  <td>{t(`models.localities.${m.locality}`)}</td>
                  <td>{keyOf(m)}</td>
                  <td>
                    <Status value={m.status} />
                    {m.probed_at && <div className="muted">{t("models.probedAt", { date: when(m.probed_at, i18n.language) })}</div>}
                    <Checks checks={m.probe} />
                  </td>
                  <td>{m.serves.map((p) => t(`models.purposes.${p}`)).join(", ") || "—"}</td>
                  <td>{admin && m.origin !== "config" && m.status !== "retired" && (
                    <>
                      <button type="button" className="link" onClick={() => void act(() => api(`/api/models/${encodeURIComponent(m.name)}/probe`, { method: "POST" }), t("models.probed", { name: m.name }))}>{t("models.probe")}</button>
                      {m.serves.length === 0 && (
                        <button type="button" className="link" onClick={() => void act(() => api(`/api/models/${encodeURIComponent(m.name)}`, { method: "DELETE" }), t("models.retired", { name: m.name }))}>{t("models.retire")}</button>
                      )}
                    </>
                  )}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {admin && usable.length > 0 && (
            <div className="panel" role="region" aria-label={t("models.change")}>
              <h2>{t("models.change")}</h2>
              <p className="muted">{t("models.changeIntro", { days: data.trial_valid_days, rollback: data.rollback_days })}</p>
              <form className="inline" onSubmit={(e) => { e.preventDefault(); void act(() => api(`/api/models/${encodeURIComponent(target)}/trial`, { method: "POST", body: { data_source: source, purpose: trialPurpose } }), t("models.trialQueued", { name: target })); }}>
                <label>{t("models.model")}
                  <select value={target} onChange={(e) => setChosen(e.target.value)}>
                    {usable.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
                  </select>
                </label>
                <label>{t("queries.dataSource")}
                  <select value={source} onChange={(e) => setDs(e.target.value)}>
                    {sources.map((n) => <option key={n} value={n}>{n}</option>)}
                  </select>
                </label>
                <label>{t("models.trialFor")}
                  <select value={trialPurpose} onChange={(e) => setTrialPurpose(e.target.value as TrialPurpose)}>
                    <option value="both">{t("models.purposes.both")}</option>
                    {data.purposes.map((p) => <option key={p} value={p}>{t(`models.purposes.${p}`)}</option>)}
                  </select>
                </label>
                <button type="submit">{t("models.startTrial")}</button>
              </form>
              <form className="inline" onSubmit={(e) => { e.preventDefault(); void act(() => api("/api/models/switch", { method: "POST", body: { purpose, model: target, force, note: note || null } }), t("models.switched", { purpose: t(`models.purposes.${purpose}`), name: target })); }}>
                <label>{t("models.purpose")}
                  <select value={purpose} onChange={(e) => setPurpose(e.target.value as ModelPurpose)}>
                    {data.purposes.map((p) => <option key={p} value={p}>{t(`models.purposes.${p}`)}</option>)}
                  </select>
                </label>
                <label>{t("models.note")}<input value={note} onChange={(e) => setNote(e.target.value)} /></label>
                <label className="check"><input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} /> {t("models.force")}</label>
                <button type="submit">{t("models.switch", { name: target })}</button>
              </form>
            </div>
          )}

          <h2>{t("models.trials")}</h2>
          {trials.length === 0 && <p className="muted">{t("models.noTrials")}</p>}
          {trials.map((j) => (
            <article key={j.id} className="card" aria-label={t("models.trialOf", { name: String(j.params.model ?? "") })}>
              <h3>{t("models.trialOf", { name: String(j.params.model ?? "") })} · {j.data_source} · <span className={`status status-${j.state}`}>{t(`jobs.states.${j.state}`)}</span></h3>
              <p className="muted">{when(j.created_at, i18n.language)} · {j.requested_by} · {t(`models.purposes.${String(j.params.purpose ?? "both") as TrialPurpose}`)}</p>
              {j.error && <p className="error">{j.error}</p>}
              {j.lines.length > 0 && <pre className="trial">{j.lines.join("\n")}</pre>}
            </article>
          ))}

          {Object.keys(meanings).length > 0 && (
            <>
              <h2>{t("models.meanings")}</h2>
              <p className="muted">{t("models.meaningsIntro")}</p>
              <table>
                <thead><tr><th>{t("queries.dataSource")}</th><th>{t("models.byModel")}</th></tr></thead>
                <tbody>
                  {Object.entries(meanings).map(([name, counts]) => (
                    <tr key={name}><td>{name}</td><td>{Object.entries(counts).map(([m, n]) => `${m} ${n.toLocaleString(i18n.language)}`).join(" · ")}</td></tr>
                  ))}
                </tbody>
              </table>
            </>
          )}

          {admin && (
            <form className="panel" aria-label={t("models.add")} onSubmit={(e) => {
              e.preventDefault();
              void act(() => api("/api/models", { method: "POST", body: { name, base_url: baseUrl, model_id: modelId, key_env: keyEnv || null, key: key || null, locality } }), t("models.added", { name })).then(() => setKey(""));
            }}>
              <h2>{t("models.add")}</h2>
              <p className="muted">{t("models.addIntro")}</p>
              <label>{t("models.name")}<input value={name} onChange={(e) => setName(e.target.value)} required /></label>
              <label>{t("models.baseUrl")}<input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…/v1" required /></label>
              <label>{t("models.modelId")}<input value={modelId} onChange={(e) => setModelId(e.target.value)} required /></label>
              <label>{t("models.keyEnvField")}<input value={keyEnv} onChange={(e) => setKeyEnv(e.target.value)} placeholder="DEEPSEEK_API_KEY" /></label>
              <label>{t("models.keyField")}<input type="password" autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} /></label>
              <label>{t("models.locality")}
                <select value={locality} onChange={(e) => setLocality(e.target.value as "remote" | "local")}>
                  <option value="remote">{t("models.localities.remote")}</option>
                  <option value="local">{t("models.localities.local")}</option>
                </select>
              </label>
              <button type="submit">{t("models.addButton")}</button>
            </form>
          )}

          {data.history.length > 0 && (
            <>
              <h2>{t("models.history")}</h2>
              <table>
                <thead><tr><th>{t("models.since")}</th><th>{t("models.purpose")}</th><th>{t("models.model")}</th><th>{t("models.replacing")}</th><th>{t("jobs.by")}</th></tr></thead>
                <tbody>
                  {data.history.map((a, i) => (
                    <tr key={`${a.purpose}-${a.since}-${i}`}>
                      <td>{when(a.since, i18n.language)}</td><td>{t(`models.purposes.${a.purpose}`)}</td>
                      <td>{a.model}{a.forced && <span className="muted"> · {t("models.forced")}</span>}</td>
                      <td>{a.previous ?? "—"}</td><td>{a.by}{a.note && <span className="muted"> — {a.note}</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </section>
  );
}
