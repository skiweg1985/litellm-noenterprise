import { createParser, createSerializer, useQueryStates } from "nuqs";

const boundedId = createParser({
  parse: (value) => (/^[a-zA-Z0-9_-]{1,128}$/.test(value) ? value : null),
  serialize: (value: string) => value,
});
const timestamp = createParser({
  parse: (value) =>
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$/.test(value) && Number.isFinite(Date.parse(value))
      ? new Date(value).toISOString()
      : null,
  serialize: (value: string) => value,
});

export const SUBSCRIPTION_LOG_QUERY = {
  subscription_id: boundedId,
  start_date: timestamp,
  end_date: timestamp,
};

/** Shared with usage-overview links so filters and exact UTC bounds agree. */
export const subscriptionLogsLink = createSerializer(SUBSCRIPTION_LOG_QUERY);
export const useSubscriptionLogRouting = () => useQueryStates(SUBSCRIPTION_LOG_QUERY, { history: "replace" });
