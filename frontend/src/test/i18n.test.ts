// "No hard-coded English strings outside locale files" (Phase 5): every key the code asks
// for exists in en.json; dynamic keys are checked against the values they can take.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

import en from "../i18n/locales/en.json";

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
  "privilege.": ["user", "sme", "admin", "auditor", "pii_processor"],
  "home.role.": ["user", "sme", "admin", "auditor"],
  "directories.roleKinds.": ["group", "attribute", "ou"],
  "status.": ["config", "unverified", "verified", "failed", "disabled", "tombstoned"],
  "dataSources.origins.": ["config", "admin"],
  "audit.actions.": ["sign_in", "sign_in_refused", "forbidden", "grant_added", "grant_revoked", "session_ended", "privileges_changed"],
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
