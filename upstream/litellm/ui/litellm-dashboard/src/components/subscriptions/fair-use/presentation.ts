import type { AdminAccount, ModelWeights } from "@/lib/http/subscriptionFairUse";

// The server names each window by its duration ("5-hour", "weekly", "3-day").
export const windowTitle = (window: { name: string }) =>
  window.name.charAt(0).toUpperCase() + window.name.slice(1) + " window";
export function policyNotice(account: Pick<AdminAccount, "enabled" | "quota_state">) {
  if (!account.enabled) return "Fair use is off";
  return account.quota_state === "current" ? null : "Fair use paused – quota data unavailable";
}

const resetFormat: Intl.DateTimeFormatOptions = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
export function localReset(seconds: number | null) {
  return seconds == null ? null : new Date(seconds * 1000).toLocaleString(undefined, resetFormat);
}

export const percent = (value: number) => value.toLocaleString(undefined, { maximumFractionDigits: 2 }) + "%";
export const formatWeight = (value: number) => value.toLocaleString(undefined, { maximumSignificantDigits: 4 }) + "×";
// Move the decimal point without introducing binary rounding in editable price fields.
function shiftDecimal(value: number, places: number) {
  const [digits, exponent = "0"] = String(value).split("e");
  return Number(digits + "e" + (Number(exponent) + places));
}
export const perMillion = (value: number) => shiftDecimal(value, 6);
export const perToken = (value: number) => shiftDecimal(value, -6);

export type OverrideDraft = {
  mode: "default" | "like" | "factor" | "prices";
  like: string;
  factor: string;
  input: string;
  output: string;
  cache: string;
};
export function overrideDraft(rule: ModelWeights[string] | undefined): OverrideDraft {
  const draft: OverrideDraft = { mode: "default", like: "", factor: "1", input: "", output: "", cache: "" };
  if (!rule) return draft;
  if ("like" in rule) return { ...draft, mode: "like", like: rule.like };
  if ("factor" in rule) return { ...draft, mode: "factor", factor: String(rule.factor) };
  return {
    ...draft,
    mode: "prices",
    input: String(perMillion(rule.input)),
    output: String(perMillion(rule.output)),
    cache: rule.cache_read == null ? "" : String(perMillion(rule.cache_read)),
  };
}

export function buildOverrides(current: ModelWeights, model: string, draft: OverrideDraft): ModelWeights {
  const next = { ...current };
  switch (draft.mode) {
    case "default":
      delete next[model];
      break;
    case "like":
      next[model] = { like: draft.like.trim() };
      break;
    case "factor":
      next[model] = { factor: Number(draft.factor) };
      break;
    case "prices":
      next[model] = {
        input: perToken(Number(draft.input)),
        output: perToken(Number(draft.output)),
        ...(draft.cache.trim() && { cache_read: perToken(Number(draft.cache)) }),
      };
      break;
  }
  return next;
}
