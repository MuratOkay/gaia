import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { Check } from "../api/client";

export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<Error | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const load = useCallback(loader, deps);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await load());
      setError(undefined);
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }, [load]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

export function when(value: string | null | undefined, locale = "en"): string {
  if (!value) return "—";
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? value
    : new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(d);
}

export function ErrorNote({ error }: { error?: Error | null }) {
  const { t } = useTranslation();
  if (!error) return null;
  return (
    <p className="error" role="alert">
      {t("common.error")}: {error.message}
    </p>
  );
}

export function Status({ value }: { value: string }) {
  const { t } = useTranslation();
  return <span className={`status status-${value}`}>{t(`status.${value}`, { defaultValue: value })}</span>;
}

export function Checks({ checks }: { checks?: Check[] | null }) {
  const { t } = useTranslation();
  if (!checks?.length) return null;
  return (
    <ul className="checks">
      {checks.map((c) => (
        <li key={c.name} className={c.ok ? "pass" : "fail"}>
          <strong>{c.ok ? t("common.pass") : t("common.fail")}</strong> {c.name} — {c.detail}
        </li>
      ))}
    </ul>
  );
}

export function Busy({ loading }: { loading: boolean }) {
  const { t } = useTranslation();
  return loading ? <p className="muted">{t("common.loading")}</p> : null;
}
