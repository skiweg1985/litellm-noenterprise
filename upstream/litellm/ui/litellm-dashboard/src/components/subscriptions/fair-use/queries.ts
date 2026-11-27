import { useQuery, useQueries, useQueryClient, useMutation } from "@tanstack/react-query";
import { userListCall } from "@/components/networking";
import { useAllTeams } from "@/app/(dashboard)/hooks/teams/useTeams";
import { getFairUseStatus } from "@/lib/http/subscriptionFairUse";

export type Session = { accessToken: string; userId: string | null };
export const fairUseKey = ({ userId, accessToken }: Session) => ["subscription-fair-use", userId, accessToken];
export const polling = { refetchInterval: 60_000, refetchIntervalInBackground: false, retry: false } as const;

export function useFairUseStatus(session: Session) {
  return useQuery({
    queryKey: [...fairUseKey(session), "status"],
    queryFn: ({ signal }) => getFairUseStatus(session.accessToken, signal),
    ...polling,
  });
}

// These PUTs replace a setting. Refresh dependent status and personal usage after each save.
export function useSaveSetting<Input, Output>(
  session: Session,
  key: string[],
  save: (input: Input) => Promise<Output>,
  close: () => void,
) {
  const client = useQueryClient();
  const prefix = fairUseKey(session);
  return useMutation({
    mutationKey: [...prefix, ...key],
    mutationFn: save,
    onSuccess: async (saved) => {
      await client.cancelQueries({ queryKey: [...prefix, ...key] });
      client.setQueryData([...prefix, ...key], saved);
      void client.invalidateQueries({ queryKey: [...prefix, "status"] });
      void client.invalidateQueries({ queryKey: [...prefix, "me"] });
      close();
    },
  });
}

export function useDirectoryNames(session: Session, userIds: string[]) {
  const teams = useAllTeams();
  const ids = [...new Set(userIds)].sort();
  const batches: string[][] = [];
  for (let i = 0; i < ids.length; i += 100) batches.push(ids.slice(i, i + 100));
  const users = useQueries({
    queries: batches.map((batch) => ({
      queryKey: [...fairUseKey(session), "user-names", batch],
      queryFn: () => userListCall(session.accessToken, batch, 1, batch.length),
      staleTime: 60_000,
      retry: false,
    })),
  });
  return {
    teams: new Map(teams.data?.map((team) => [team.team_id, team.team_alias || team.team_id])),
    users: new Map(
      users.flatMap((result) =>
        (result.data?.users ?? []).map(
          (user) => [user.user_id, user.user_email || user.user_alias || user.user_id] as const,
        ),
      ),
    ),
  };
}
