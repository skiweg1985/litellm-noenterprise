"use client";

import { useState } from "react";
import { useMutation, useQuery, type UseQueryOptions } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ApiError } from "@/lib/http/client";
import {
  getBulkAssignments,
  saveBulkAssignments,
  type AssignmentPreview,
  type BulkChoice,
  type BulkResults,
  type BulkPreview,
} from "@/lib/http/subscriptionBulk";

const modeNames = {
  fixed: "Fixed subscription",
  priority: "Preferred subscription with reserves",
  distribution: "Distribute requests",
};
type ChoiceState = { selected: boolean; action: BulkChoice["action"] | "" };

function Membership({ assignment }: { assignment: AssignmentPreview }) {
  return (
    <>
      <span className="block text-sm text-muted-foreground">{modeNames[assignment.mode]}</span>
      <span className="break-words">{assignment.names.join(assignment.mode === "priority" ? " → " : ", ")}</span>
    </>
  );
}

export function SubscriptionBulkAssignment({
  accessToken,
  account,
  onClose,
  onSaved,
}: {
  accessToken: string;
  account: string;
  onClose: () => void;
  onSaved?: () => void;
}) {
  const [choices, setChoices] = useState<Record<string, ChoiceState>>({});
  const [outcomes, setOutcomes] = useState<Record<string, BulkResults["results"][number]>>({});
  const queryOptions: UseQueryOptions<BulkPreview, Error> = {
    queryKey: ["subscription-bulk", accessToken, account],
    queryFn: ({ signal }) => getBulkAssignments(accessToken, account, signal),
    retry: false,
    staleTime: 0,
    refetchOnWindowFocus: false,
  };
  const query = useQuery(queryOptions);
  const choiceFor = (row: BulkPreview["models"][number]): ChoiceState =>
    choices[row.model_id] ?? { selected: row.selected ?? false, action: row.current?.mode === "fixed" ? "" : "append" };
  const save = useMutation({
    mutationFn: (models: BulkChoice[]) => saveBulkAssignments(accessToken, account, models),
    onSuccess: (data) => {
      setOutcomes((previous) => ({
        ...previous,
        ...Object.fromEntries(data.results.map((result) => [result.model_id, result])),
      }));
      setChoices((previous) => {
        const next = { ...previous };
        for (const result of data.results)
          if (result.status === "saved")
            next[result.model_id] = { action: next[result.model_id]?.action ?? "append", selected: false };
        return next;
      });
      onSaved?.();
    },
  });
  const results = new Map(Object.values(outcomes).map((result) => [result.model_id, result]));
  const authError = [query.error, save.error].some(
    (error) => error instanceof ApiError && [401, 403].includes(error.status),
  );
  const rows = query.isError || authError ? [] : query.data?.models ?? [];
  const selected = rows.filter((row) => choiceFor(row).selected && row.eligible);
  const valid =
    !save.isError &&
    selected.length > 0 &&
    selected.every((row) => row.version && choiceFor(row).action && results.get(row.model_id)?.status !== "conflict");
  const pending = query.isFetching || save.isPending;
  const emptyReview = !query.isPending && !query.isError && rows.length === 0;
  const refreshReview = () => {
    save.reset();
    setOutcomes({});
    setChoices({});
    void query.refetch();
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) onClose();
      }}
    >
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl" showCloseButton={!save.isPending}>
        <DialogHeader>
          <DialogTitle>
            Assign {!query.isError && !authError ? query.data?.display_name ?? "subscription" : "subscription"} to
            models
          </DialogTitle>
          <DialogDescription>
            Review optional changes before saving. Signing in alone does not change any model. Your subscription stays
            connected if a change fails.
          </DialogDescription>
        </DialogHeader>
        {query.isPending && <p role="status">Loading compatible models…</p>}
        {query.isError && (
          <p role="alert">The model review or subscription catalog is unavailable. Refresh the review to retry.</p>
        )}
        {save.isError && <p role="alert">Changes could not be confirmed. Refresh the review before retrying.</p>}
        <Button variant="outline" disabled={pending} onClick={refreshReview}>
          Refresh review
        </Button>
        {emptyReview && <p>No ChatGPT models are available for this review.</p>}
        <div className="space-y-3">
          {rows.map((row) => (
            <AssignmentRow
              key={row.model_id}
              row={row}
              choice={choiceFor(row)}
              result={results.get(row.model_id)}
              pending={pending}
              plan={query.data}
              onChange={(choice) => setChoices((previous) => ({ ...previous, [row.model_id]: choice }))}
            />
          ))}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="outline" disabled={save.isPending} onClick={onClose}>
            Done
          </Button>
          <Button
            disabled={!valid || pending || query.isError}
            onClick={() =>
              save.mutate(
                selected.map((row) => ({
                  model_id: row.model_id,
                  version: row.version!,
                  action: choiceFor(row).action as BulkChoice["action"],
                })),
              )
            }
          >
            Save selected models
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function proposedAssignment(row: BulkPreview["models"][number], action: ChoiceState["action"], plan?: BulkPreview) {
  if (row.current?.mode !== "fixed" || !plan) return row.proposed;
  if (!action || action === "append") return row.proposed;
  const replacing = action === "replace";
  return {
    mode: replacing ? ("fixed" as const) : action,
    subscription_ids: replacing ? [plan.subscription_id] : [...row.current.subscription_ids, plan.subscription_id],
    names: replacing ? [plan.display_name] : [...row.current.names, plan.display_name],
  };
}

function AssignmentRow({
  row,
  choice,
  result,
  pending,
  plan,
  onChange,
}: {
  row: BulkPreview["models"][number];
  choice: ChoiceState;
  result?: BulkResults["results"][number];
  pending: boolean;
  plan?: BulkPreview;
  onChange: (choice: ChoiceState) => void;
}) {
  const saved = result?.status === "saved";
  const proposed = proposedAssignment(row, choice.action, plan);
  return (
    <section
      key={row.model_id}
      role="region"
      aria-label={`Model ${row.model_name}`}
      className="space-y-3 rounded-lg border p-3"
    >
      <label className="flex items-start gap-3 font-medium">
        <input
          type="checkbox"
          className="mt-1"
          aria-label={`Select ${row.model_name}`}
          checked={choice.selected}
          disabled={!row.eligible || pending || saved}
          onChange={(event) => onChange({ ...choice, selected: event.target.checked })}
        />
        <span className="min-w-0 break-words">
          {row.model_name}
          <span className="block text-sm font-normal text-muted-foreground">Upstream model: {row.upstream_model}</span>
        </span>
      </label>
      {row.reason && <p className="text-sm text-muted-foreground">{row.reason}</p>}
      {row.current && (
        <div>
          <h3 className="text-sm font-medium">Current</h3>
          <Membership assignment={row.current} />
        </div>
      )}
      {row.eligible && row.current?.mode === "fixed" && (
        <label className="block space-y-1 text-sm">
          <span>Change for {row.model_name}</span>
          <select
            className="w-full rounded-md border bg-background p-2"
            aria-label={`Change for ${row.model_name}`}
            value={choice.action}
            disabled={!choice.selected || pending || saved}
            onChange={(event) => onChange({ ...choice, action: event.target.value as ChoiceState["action"] })}
          >
            <option value="">Choose an explicit change</option>
            <option value="replace">Replace the fixed subscription</option>
            <option value="priority">Keep current first and add as reserve</option>
            <option value="distribution">Distribute across current and new</option>
          </select>
        </label>
      )}
      {proposed && choice.selected && (
        <div>
          <h3 className="text-sm font-medium">After saving</h3>
          <Membership assignment={proposed} />
        </div>
      )}
      {result && (
        <p role={saved ? "status" : "alert"} className={saved ? "text-sm" : "text-sm text-destructive"}>
          {result.message}
        </p>
      )}
    </section>
  );
}
