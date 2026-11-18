// Pieces the review screens share (Phase 5B).
import { useTranslation } from "react-i18next";
import { NavLink } from "react-router-dom";

export const REASONS = ["abstain", "evidence_exhausted", "contradiction", "critique_disagrees", "test_not_run", "input_reviewed", "stale_decision", "blind", "calibration_sample"] as const;
export const BANDS = ["accepted", "deep_profile", "deep_profile_flagged", "abstain"] as const;
export const MEANING_STATUSES = ["draft", "auto_approved", "sme_reviewed", "sme_rejected", "stale"] as const;

export function enc(value: string): string {
  return encodeURIComponent(value);
}

export function ReviewNav({ ds }: { ds: string }) {
  const { t } = useTranslation();
  return (
    <nav className="subnav" aria-label={t("review.nav")}>
      <NavLink to="/review" end>{t("review.sources")}</NavLink>
      <NavLink to={`/review/${enc(ds)}`} end>{t("review.queue", { ds })}</NavLink>
      <NavLink to={`/review/${enc(ds)}/tables`} end>{t("review.tables")}</NavLink>
      <NavLink to={`/review/${enc(ds)}/glossary`}>{t("review.glossary")}</NavLink>
      <NavLink to={`/review/${enc(ds)}/answers`}>{t("review.answers.nav")}</NavLink>
    </nav>
  );
}

export function Reasons({ reasons }: { reasons: string[] }) {
  const { t } = useTranslation();
  return (
    <span className="chips">
      {reasons.map((r) => (
        <span key={r} className={`chip reason-${r}`} title={t(`review.reasonHelp.${r}`, { defaultValue: "" })}>
          {t(`review.reasons.${r}`, { defaultValue: r })}
        </span>
      ))}
    </span>
  );
}

/** A band and its score. ``calibrated`` (6C): the score is P(right) from a fitted calibrator —
 *  shown as a percentage with its lower bound; otherwise a ranking, marked "uncalibrated"
 *  where asked (``mark``). */
export function Band({ band, score, calibrated, lower, mark = false }: {
  band: string | null; score: number | null; calibrated?: boolean; lower?: number | null; mark?: boolean;
}) {
  const { t } = useTranslation();
  if (!band) return <span className="muted">—</span>;
  return (
    <span className={`status band-${band}`}>
      {t(`review.bands.${band}`, { defaultValue: band })}
      {score !== null && (calibrated
        ? ` · ${t("review.probability", { p: Math.round(score * 100) })}${lower !== undefined && lower !== null ? ` ${t("review.atLeast", { lo: Math.round(lower * 100) })}` : ""}`
        : ` · ${score.toFixed(2)}`)}
      {score !== null && !calibrated && mark && <span className="muted" title={t("review.uncalibratedHelp")}> {t("review.uncalibrated")}</span>}
    </span>
  );
}

export function MeaningStatus({ status }: { status: string | null }) {
  const { t } = useTranslation();
  if (!status) return null;
  return <span className={`status meaning-${status}`}>{t(`review.statuses.${status}`, { defaultValue: status })}</span>;
}

/** "a, b, c" <-> ["a", "b", "c"] */
export function splitList(text: string): string[] {
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}
