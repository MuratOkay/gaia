import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { BrowserRouter, Navigate, NavLink, Route, Routes } from "react-router-dom";

import { api, type Me, type Role, SIGNED_OUT_EVENT } from "./api/client";
import { Accounts } from "./pages/admin/Accounts";
import { DataSources } from "./pages/admin/DataSources";
import { Directories } from "./pages/admin/Directories";
import { Grants } from "./pages/admin/Grants";
import { AuditAccess, AuditEvents, AuditGrants, AuditPii, AuditSummaryPage } from "./pages/audit/Audit";
import { Home } from "./pages/Home";
import { Login } from "./pages/Login";

export function App() {
  const [me, setMe] = useState<Me | null | false>(null); // null: checking; false: signed out

  useEffect(() => {
    api<Me>("/api/auth/me").then(setMe).catch(() => setMe(false));
    const out = () => setMe(false);
    window.addEventListener(SIGNED_OUT_EVENT, out);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, out);
  }, []);

  if (me === null) return null;
  if (me === false) return <Login onSignedIn={setMe} />;
  return (
    <BrowserRouter>
      <Shell me={me} onSignedOut={() => setMe(false)} />
    </BrowserRouter>
  );
}

export function Shell({ me, onSignedOut }: { me: Me; onSignedOut: () => void }) {
  const { t } = useTranslation();
  const has = useCallback((...roles: Role[]) => roles.some((r) => me.roles.includes(r)), [me]);

  async function signOut() {
    try {
      await api("/api/auth/logout", { method: "POST" });
    } finally {
      onSignedOut();
    }
  }

  return (
    <div className="shell">
      <header>
        <strong className="brand">{t("app.name")}</strong>
        <nav aria-label={t("nav.label")}>
          <NavLink to="/" end>{t("nav.home")}</NavLink>
          {has("admin") && (
            <>
              <NavLink to="/admin/data-sources">{t("nav.dataSources")}</NavLink>
              <NavLink to="/admin/directories">{t("nav.directories")}</NavLink>
              <NavLink to="/admin/grants">{t("nav.grants")}</NavLink>
              <NavLink to="/admin/accounts">{t("nav.accounts")}</NavLink>
            </>
          )}
          {has("auditor") && <NavLink to="/audit">{t("nav.audit")}</NavLink>}
        </nav>
        <span className="who">
          {me.display_name ?? me.login}
          <span className="muted"> · {me.roles.map((r) => t(`role.${r}`)).join(", ")}</span>
          <button type="button" className="link" onClick={signOut}>{t("nav.signOut")}</button>
        </span>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Home me={me} />} />
          <Route path="/admin/data-sources" element={<Only when={has("admin")}><DataSources /></Only>} />
          <Route path="/admin/directories" element={<Only when={has("admin")}><Directories /></Only>} />
          <Route path="/admin/grants" element={<Only when={has("admin")}><Grants /></Only>} />
          <Route path="/admin/accounts" element={<Only when={has("admin")}><Accounts /></Only>} />
          <Route path="/audit" element={<Only when={has("auditor")}><AuditSummaryPage /></Only>} />
          <Route path="/audit/access" element={<Only when={has("auditor")}><AuditAccess /></Only>} />
          <Route path="/audit/pii" element={<Only when={has("auditor")}><AuditPii /></Only>} />
          <Route path="/audit/grants" element={<Only when={has("auditor")}><AuditGrants /></Only>} />
          <Route path="/audit/events" element={<Only when={has("auditor")}><AuditEvents /></Only>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

function Only({ when, children }: { when: boolean; children: React.ReactNode }) {
  const { t } = useTranslation();
  return when ? <>{children}</> : <p className="note">{t("common.notAllowed")}</p>;
}
