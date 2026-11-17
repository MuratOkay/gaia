import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { BrowserRouter, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { api, type Me, type Role, SIGNED_OUT_EVENT } from "./api/client";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Accounts } from "./pages/admin/Accounts";
import { Categories } from "./pages/admin/Categories";
import { DataSources } from "./pages/admin/DataSources";
import { Directories } from "./pages/admin/Directories";
import { Grants } from "./pages/admin/Grants";
import { AuditAccess, AuditCategories, AuditEvents, AuditGrants, AuditPii, AuditRefusals, AuditSummaryPage } from "./pages/audit/Audit";
import { ChatPage } from "./pages/chat/Chat";
import { Home } from "./pages/Home";
import { Login } from "./pages/Login";
import { Queries, QueryPage, Runs } from "./pages/queries/Queries";
import { ColumnPage } from "./pages/review/Column";
import { ReviewGlossary } from "./pages/review/Glossary";
import { ReviewQueue } from "./pages/review/Queue";
import { ReviewSources } from "./pages/review/Sources";
import { TablePage } from "./pages/review/Table";
import { ReviewTables } from "./pages/review/Tables";

export function App() {
  const [me, setMe] = useState<Me | null | false>(null); // null: checking; false: signed out

  useEffect(() => {
    api<Me>("/api/auth/me").then(setMe).catch(() => setMe(false));
    const out = () => setMe(false);
    window.addEventListener(SIGNED_OUT_EVENT, out);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, out);
  }, []);

  if (me === null) return <LoadingShell />;
  if (me === false) return <Login onSignedIn={setMe} />;
  return (
    <BrowserRouter>
      <Shell me={me} onSignedOut={() => setMe(false)} />
    </BrowserRouter>
  );
}

function LoadingShell() {
  const { t } = useTranslation();
  return <p className="muted loading-shell" role="status">{t("common.loading")}</p>;
}

export function Shell({ me, onSignedOut }: { me: Me; onSignedOut: () => void }) {
  const { t } = useTranslation();
  const location = useLocation();
  const has = useCallback((...roles: Role[]) => roles.some((r) => me.roles.includes(r)), [me]);
  // the local account is an administrator and nothing else: it holds no data category and
  // cannot chat (D68, D76) — its Chat page only explains that
  const chats = has("user", "sme", "admin") && !me.local;

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
          {chats && <NavLink to="/chat">{t("nav.chat")}</NavLink>}
          {has("sme") && <NavLink to="/review">{t("nav.review")}</NavLink>}
          {has("sme", "admin") && <NavLink to="/queries">{t("nav.queries")}</NavLink>}
          {has("admin") && (
            <>
              <NavLink to="/admin/data-sources">{t("nav.dataSources")}</NavLink>
              <NavLink to="/admin/directories">{t("nav.directories")}</NavLink>
              <NavLink to="/admin/categories">{t("nav.categories")}</NavLink>
              <NavLink to="/admin/grants">{t("nav.grants")}</NavLink>
              <NavLink to="/admin/accounts">{t("nav.accounts")}</NavLink>
              <NavLink to="/runs">{t("nav.runs")}</NavLink>
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
        <ErrorBoundary resetKey={location.pathname}>
        <Routes>
          <Route path="/" element={<Home me={me} />} />
          <Route path="/chat" element={<Only when={has("user", "sme", "admin")}><ChatPage me={me} /></Only>} />
          <Route path="/chat/:id" element={<Only when={has("user", "sme", "admin")}><ChatPage me={me} /></Only>} />
          <Route path="/queries" element={<Only when={has("sme", "admin", "auditor")}><Queries /></Only>} />
          <Route path="/queries/:id" element={<Only when={has("sme", "admin", "auditor")}><QueryPage /></Only>} />
          <Route path="/runs" element={<Only when={has("admin", "auditor")}><Runs /></Only>} />
          <Route path="/admin/categories" element={<Only when={has("admin")}><Categories /></Only>} />
          <Route path="/audit/categories" element={<Only when={has("auditor")}><AuditCategories /></Only>} />
          <Route path="/audit/refusals" element={<Only when={has("auditor")}><AuditRefusals /></Only>} />
          <Route path="/admin/data-sources" element={<Only when={has("admin")}><DataSources /></Only>} />
          <Route path="/admin/directories" element={<Only when={has("admin")}><Directories /></Only>} />
          <Route path="/admin/grants" element={<Only when={has("admin")}><Grants /></Only>} />
          <Route path="/admin/accounts" element={<Only when={has("admin")}><Accounts /></Only>} />
          <Route path="/review" element={<Only when={has("sme")}><ReviewSources /></Only>} />
          <Route path="/review/:ds" element={<Only when={has("sme")}><ReviewQueue /></Only>} />
          <Route path="/review/:ds/columns/:id" element={<Only when={has("sme")}><ColumnPage /></Only>} />
          <Route path="/review/:ds/tables" element={<Only when={has("sme")}><ReviewTables /></Only>} />
          <Route path="/review/:ds/tables/:id" element={<Only when={has("sme")}><TablePage /></Only>} />
          <Route path="/review/:ds/glossary" element={<Only when={has("sme")}><ReviewGlossary /></Only>} />
          <Route path="/audit" element={<Only when={has("auditor")}><AuditSummaryPage /></Only>} />
          <Route path="/audit/access" element={<Only when={has("auditor")}><AuditAccess /></Only>} />
          <Route path="/audit/pii" element={<Only when={has("auditor")}><AuditPii /></Only>} />
          <Route path="/audit/grants" element={<Only when={has("auditor")}><AuditGrants /></Only>} />
          <Route path="/audit/events" element={<Only when={has("auditor")}><AuditEvents /></Only>} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </ErrorBoundary>
      </main>
    </div>
  );
}

function Only({ when, children }: { when: boolean; children: React.ReactNode }) {
  const { t } = useTranslation();
  return when ? <>{children}</> : <p className="note">{t("common.notAllowed")}</p>;
}
