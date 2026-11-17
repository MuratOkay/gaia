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
    // back from Microsoft after a refused sign-in (Entra ID, 5A.2)
    if (new URLSearchParams(window.location.search).get("signin") === "failed") {
      setError(t("login.failed"));
      window.history.replaceState(null, "", window.location.pathname);
    }
    api<SignInOptions>("/api/auth/options")
      .then((o) => {
        setOptions(o);
        const ldap = o.directories.filter((d) => d.kind !== "entra");
        setSource(ldap[0]?.name ?? (o.local ? LOCAL : ""));
        setSetupMode(o.local_setup_pending && !o.local);
      })
      .catch(() => setError(t("login.unavailable")));
  }, [t]);

  const passwordDirectories = options?.directories.filter((d) => d.kind !== "entra") ?? [];
  const microsoft = options?.directories.filter((d) => d.kind === "entra") ?? [];

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
  const passwords = passwordDirectories.length > 0 || !!options?.local;
  return (
    <main className="login">
      <h1>{t("app.name")}</h1>
      <p className="muted">{t("login.intro")}</p>
      {nothing && <p className="note">{t("login.nothing")}</p>}
      {microsoft.map((d) => (
        <button key={d.name} type="button" className="microsoft"
                onClick={() => window.location.assign(`/api/auth/oidc/start?directory=${encodeURIComponent(d.name)}`)}>
          {t("login.microsoft", { name: d.name })}
        </button>
      ))}
      {microsoft.length > 0 && passwords && <p className="muted or">{t("login.or")}</p>}
      {microsoft.length > 0 && !passwords && error && <p className="error" role="alert">{error}</p>}
      {passwords && (
      <form onSubmit={submit} aria-label={t("login.title")}>
        {options && (passwordDirectories.length > 1 || (passwordDirectories.length > 0 && options.local)) && (
          <label>
            {t("login.directory")}
            <select value={source} onChange={(e) => setSource(e.target.value)}>
              {passwordDirectories.map((d) => (
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
      )}
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
