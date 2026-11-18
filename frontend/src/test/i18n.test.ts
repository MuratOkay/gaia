// "No hard-coded English strings outside locale files" (Phase 5): every key the code asks
// for exists in en.json; dynamic keys are checked against the values they can take.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import { FEEDBACK_CAUSES } from "../api/client";
import en from "../i18n/locales/en.json";
import { STAGES } from "../components/chat";
import { BANDS, MEANING_STATUSES, REASONS } from "../pages/review/common";

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((f: string) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? files(p) : /\.tsx?$/.test(f) && !f.endsWith(".test.ts") && !f.endsWith(".test.tsx") ? [p] : [];
  });
}

function has(key: string): boolean {
  let node: unknown = en;
  for (const part of key.split(".")) {
    if (typeof node !== "object" || node === null || !(part in node)) return false;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string";
}

const DYNAMIC: Record<string, string[]> = {
  "role.": ["user", "sme", "admin", "auditor"],
  "privilege.": ["user", "sme", "admin", "auditor", "pii_processor", "category"],
  "home.role.": ["user", "sme", "admin", "auditor"],
  "directories.roleKinds.": ["group", "attribute", "ou"],
  "status.": ["config", "unverified", "verified", "failed", "disabled", "tombstoned"],
  "dataSources.origins.": ["config", "admin"],
  "audit.actions.": ["sign_in", "sign_in_refused", "forbidden", "grant_added", "grant_revoked", "session_ended", "privileges_changed",
                     "review_decision", "glossary_term_added", "glossary_term_changed", "glossary_term_removed",
                     "category_added", "category_changed", "category_retired", "object_category_set",
                     "categories_proposed", "access_refused", "identity_changed", "account_relinked",
                     "account_renewed", "feedback_decided"],
  "review.reasons.": [...REASONS],
  "review.reasonHelp.": [...REASONS],
  "review.bands.": [...BANDS],
  "review.statuses.": [...MEANING_STATUSES],
  "review.verdicts.": ["supported", "plausible", "doubtful", "contradicted"],
  "review.summary.": ["none", "reads_as", "decided", "score", "critique", "tests", "distinct", "personal", "unsure", "blind"],
  "review.tabs.": ["open", "done", "stale"],
  "review.tabHelp.": ["open", "done", "stale"],
  "review.empty.": ["open", "done", "stale"],
  "review.actions.": ["accept", "correct", "reject", "unused"],
  "review.actionHelp.": ["accept", "correct", "reject", "unused"],
  "review.fields.": ["business_name", "entity", "unit", "personal"],
  "review.directions.": ["supports", "contradicts"],
  "review.hintSources.": ["fk", "llm", "sme"],
  "review.hintStatuses.": ["confirmed", "inferred", "rejected", "refuted", "stale"],
  "review.lineage.kinds.": ["pass_through", "computed", "unresolved"],
  "chat.confidence.": ["high", "medium", "low"],
  "chat.stages.": [...STAGES],
  "chat.kinds.": ["cannot_answer", "failed", "refused"],
  "queries.statuses.": ["ok", "rejected", "refused", "error", "timeout", "lock_timeout"],
  "audit.refusalCodes.": ["forbidden", "personal_data"],
  "categories.basis.": ["direct", "view", "related", "weak"],
  "evaluation.verdicts.": ["passes", "fails", "cannot_tell"],
  "evaluation.modes.": ["fresh", "replay", "mixed", "imported"],
  "categories.basisHelp.": ["direct", "view", "related", "weak"],
  "chat.feedback.given.": ["right", "partly", "wrong"],
  "chat.feedback.verdicts.": ["right", "partly", "wrong"],
  "chat.feedback.decisions.": ["right", "partly", "wrong"],
  "chat.feedback.causes.": [...FEEDBACK_CAUSES],
  "review.answers.tabs.": ["open", "decided"],
  "review.answers.empty.": ["open", "decided"],
  "review.answers.verdicts.": ["right", "partly", "wrong"],
  "review.answers.causes.": [...FEEDBACK_CAUSES],
  "evaluation.calibration.kinds.": ["meaning", "answer"],
};

describe("locale keys", () => {
  const source = files(join(__dirname, "..")).map((f) => readFileSync(f, "utf8")).join("\n");

  it("every static key exists", () => {
    const keys = [...source.matchAll(/\bt\(\s*"([a-zA-Z0-9_.]+)"/g)].map((m) => m[1]);
    expect(keys.length).toBeGreaterThan(100);
    const missing = [...new Set(keys)].filter((k) => !has(k));
    expect(missing).toEqual([]);
  });

  it("every dynamic key family is covered", () => {
    const prefixes = [...source.matchAll(/\bt\(\s*`([a-zA-Z0-9_.]+)\$\{/g)].map((m) => m[1]);
    for (const prefix of new Set(prefixes)) {
      expect(DYNAMIC[prefix], `unknown dynamic key family ${prefix}`).toBeDefined();
      for (const value of DYNAMIC[prefix]) expect(has(prefix + value), prefix + value).toBe(true);
    }
  });
});
