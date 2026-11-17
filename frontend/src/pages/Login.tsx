import { type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError, type Me, type SignInOptions } from "../api/client";

const LOCAL = "__local__";

export function Login({ onSignedIn }: { onSignedIn: (me: Me) => void }) {
  const { t } = useTranslation();
  const [options, setOptions] = useState<SignInOptions | null>(null);
  const [source, setSource] = useState<string>("");
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [setupMode, setSetupMode] = useState(false);

  useEffect(() => {
    api<SignInOptions>("/api/auth/options")
      .then((o) => {
        setOptions(o);
        setSource(o.directories[0]?.name ?? (o.local ? LOCAL : ""));
        setSetupMode(o.local_setup_pending && !o.local);
      })
      .catch(() => setError(t("login.unavailable")));
  }, [t]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const local = source === LOCAL;
      const me = await api<Me>("/api/auth/login", {
        method: "POST",
        body: { login: local ? "gaia-admin" : login, password, directory: local ? undefined : source || undefined },
      });
      setPassword("");
      onSignedIn(me);
    } catch (e) {
      setError(e instanceof ApiError && e.status === 401 ? t("login.failed") : (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (setupMode) return <LocalSetup onDone={() => { setSetupMode(false); setSource(LOCAL); }} onCancel={() => setSetupMode(false)} />;

  const nothing = options && !options.directories.length && !options.local;
  return (
    <main className="login">
      <h1>{t("app.name")}</h1>
      <p className="muted">{t("login.intro")}</p>
      {nothing && <p className="note">{t("login.nothing")}</p>}
      <form onSubmit={submit} aria-label={t("login.title")}>
        {options && (options.directories.length > 1 || (options.directories.length > 0 && options.local)) && (
          <label>
            {t("login.directory")}
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              {options.directories.map((d) => (
                <option key={d.name} value={d.name}>{d.name}</option>
              ))}
              {options.local && <option value={LOCAL}>{t("login.localAccount")}</option>}
            </select>
          </label>
        )}
        {source !== LOCAL && (
          <label>
            {t("login.login")}
            <input autoComplete="username" value={login} onChange={(e) => setLogin(e.target.value)} required />
          </label>
        )}
        {source === LOCAL && <p className="muted">{t("login.asLocal")}</p>}
        <label>
          {t("login.password")}
          <input type="password" autoComplete="current-password" value={password}
                 onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit" disabled={busy || !!nothing}>{busy ? t("common.wait") : t("login.submit")}</button>
      </form>
      {options?.local_setup_pending && (
        <button type="button" className="link" onClick={() => setSetupMode(true)}>{t("login.haveCode")}</button>
      )}
    </main>
  );
}

function LocalSetup({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const { t } = useTranslation();
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (password !== again) {
      setError(t("setup.differ"));
      return;
    }
    try {
      await api("/api/auth/setup", { method: "POST", body: { code: code.trim(), password } });
      setDone(true);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (done) {
    return (
      <main className="login">
        <h1>{t("setup.title")}</h1>
        <p>{t("setup.done")}</p>
        <button type="button" onClick={onDone}>{t("setup.toSignIn")}</button>
      </main>
    );
  }
  return (
    <main className="login">
      <h1>{t("setup.title")}</h1>
      <p className="muted">{t("setup.intro")}</p>
      <form onSubmit={submit} aria-label={t("setup.title")}>
        <label>
          {t("setup.code")}
          <input value={code} onChange={(e) => setCode(e.target.value)} required autoComplete="one-time-code" />
        </label>
        <label>
          {t("setup.password")}
          <input type="password" autoComplete="new-password" minLength={12} value={password}
                 onChange={(e) => setPassword(e.target.value)} required />
        </label>
        <label>
          {t("setup.again")}
          <input type="password" autoComplete="new-password" value={again}
                 onChange={(e) => setAgain(e.target.value)} required />
        </label>
        {error && <p className="error" role="alert">{error}</p>}
        <button type="submit">{t("setup.submit")}</button>
        <button type="button" className="link" onClick={onCancel}>{t("common.cancel")}</button>
      </form>
    </main>
  );
}
