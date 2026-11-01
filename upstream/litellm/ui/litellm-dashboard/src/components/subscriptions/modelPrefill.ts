import { createParser, createSerializer, parseAsStringLiteral } from "nuqs";
import { uiHref } from "@/utils/uiHref";

const boundedString = (pattern: RegExp) =>
  createParser({ parse: (value) => (pattern.test(value) ? value : null), serialize: String });
export const subscriptionPrefillParsers = {
  subscription: boundedString(/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i),
  subscription_model: boundedString(/^[a-zA-Z0-9._-]{1,200}$/),
};
const serialize = createSerializer({ tab: parseAsStringLiteral(["add"] as const), ...subscriptionPrefillParsers });
export function subscriptionModelHref(subscription: string, model: string) {
  return serialize(uiHref("models-and-endpoints/"), { tab: "add", subscription, subscription_model: model });
}
