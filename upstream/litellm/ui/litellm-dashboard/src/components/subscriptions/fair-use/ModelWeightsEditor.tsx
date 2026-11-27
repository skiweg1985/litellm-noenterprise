import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { TableCell, TableRow } from "@/components/ui/table";
import {
  getModelWeights,
  saveModelWeights,
  type ModelWeights,
  type ModelWeightsStatus,
} from "@/lib/http/subscriptionFairUse";
import { fairUseKey, polling, type Session, useSaveSetting } from "./queries";
import { buildOverrides, formatWeight, overrideDraft, type OverrideDraft } from "./presentation";
import { FairUseTable } from "./FairUseTable";

const pricingLabels = {
  list: "List price",
  factor: "Custom factor",
  prices: "Custom prices",
  like: "Matched price",
  unpriced: "No price",
};
const modes = { default: "Use list price", like: "Same as model…", factor: "Factor", prices: "Set prices" };
function OverrideEditor({
  session,
  data,
  model,
  close,
}: {
  session: Session;
  data: ModelWeightsStatus;
  model: string;
  close: () => void;
}) {
  const [draft, setDraft] = useState(() => overrideDraft(data.overrides[model]));
  const save = useSaveSetting(
    session,
    ["model-weights"],
    (overrides: ModelWeights) => saveModelWeights(session.accessToken, overrides),
    close,
  );
  const list = useId();
  const likeTarget = draft.like.trim();
  const targetRule = data.overrides[likeTarget];
  const inheritsPrices = targetRule && "like" in targetRule;
  const usedAsTarget = Object.values(data.overrides).some((rule) => "like" in rule && rule.like === model);
  const wouldChain = likeTarget === model || inheritsPrices || usedAsTarget;
  const invalidTarget = draft.mode === "like" && wouldChain;
  const related = [...new Set([data.reference_model, ...data.models.map((row) => row.model)])].filter(
    (name) => name !== model && !(data.overrides[name] && "like" in data.overrides[name]),
  );
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !save.isPending) close();
      }}
    >
      <DialogContent className="max-h-[85dvh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="break-all">Edit weight for {model}</DialogTitle>
        </DialogHeader>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(buildOverrides(data.overrides, model, draft));
          }}
        >
          <fieldset disabled={save.isPending} className="space-y-4">
            <RadioGroup
              aria-label="Weight setting"
              value={draft.mode}
              onValueChange={(mode) => setDraft({ ...draft, mode: mode as OverrideDraft["mode"] })}
            >
              {(Object.keys(modes) as OverrideDraft["mode"][]).map((mode) => (
                <label className="flex items-center gap-2" key={mode}>
                  <RadioGroupItem value={mode} />
                  {modes[mode]}
                </label>
              ))}
            </RadioGroup>
            {draft.mode === "like" && (
              <label className="block">
                Model
                <Input
                  required
                  value={draft.like}
                  list={list}
                  onChange={(event) => setDraft({ ...draft, like: event.target.value })}
                />
                <datalist id={list}>
                  {related.map((name) => (
                    <option key={name} value={name} />
                  ))}
                </datalist>
              </label>
            )}
            {invalidTarget && (
              <p role="alert">Choose a model with its own prices, or set a factor or prices directly.</p>
            )}
            {draft.mode === "factor" && (
              <label className="block">
                Weight factor
                <Input
                  required
                  type="number"
                  min={0}
                  max={100}
                  step="any"
                  value={draft.factor}
                  onChange={(event) => setDraft({ ...draft, factor: event.target.value })}
                />
              </label>
            )}
            {draft.mode === "prices" && (
              <fieldset className="grid gap-2">
                <legend>Prices per 1M tokens</legend>
                {(["input", "output", "cache"] as const).map((key) => (
                  <label key={key}>
                    {{ input: "Input price", output: "Output price", cache: "Cache read price (optional)" }[key]}
                    <Input
                      type="number"
                      min={0}
                      step="any"
                      required={key !== "cache"}
                      value={draft[key]}
                      onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
                    />
                  </label>
                ))}
              </fieldset>
            )}
            {save.isError && <p role="alert">Could not save this weight. Check the settings and try again.</p>}
            <Button type="submit" disabled={Boolean(invalidTarget) || (draft.mode === "like" && !likeTarget)}>
              Save weight
            </Button>
          </fieldset>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function ModelWeightsEditor({ session }: { session: Session }) {
  const query = useQuery({
    queryKey: [...fairUseKey(session), "model-weights"],
    queryFn: ({ signal }) => getModelWeights(session.accessToken, signal),
    ...polling,
  });
  const [model, setModel] = useState<string | null>(null);
  const data = query.data;
  return (
    <section className="min-w-0 space-y-3" aria-labelledby="weights-heading">
      <h2 id="weights-heading" className="text-xl font-semibold">
        Model weights
      </h2>
      {query.isPending && <p role="status">Loading model weights…</p>}
      {query.isError ? (
        <p role="alert">Could not load model weights.</p>
      ) : (
        data && (
          <>
            <p className="break-words text-sm">Reference model: {data.reference_model}</p>
            {data.models.length ? (
              <FairUseTable name="Model weights" columns={["Model", "Weight", "Pricing", "Actions"]}>
                {data.models.map((row) => {
                  const rule = data.overrides[row.model];
                  const pricing = rule && "like" in rule ? "Same as " + rule.like : pricingLabels[row.source];
                  return (
                    <TableRow key={row.model}>
                      <TableCell>{row.model}</TableCell>
                      <TableCell>{formatWeight(row.weight)}</TableCell>
                      <TableCell>
                        {row.unpriced ? (
                          <strong className="text-amber-700 dark:text-amber-300">
                            No price – counts like {data.reference_model}
                          </strong>
                        ) : (
                          pricing
                        )}
                      </TableCell>
                      <TableCell>
                        <Button
                          size="sm"
                          variant="outline"
                          aria-label={"Edit weight for " + row.model}
                          onClick={() => setModel(row.model)}
                        >
                          Edit
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </FairUseTable>
            ) : (
              <p>No models yet.</p>
            )}
            {model && <OverrideEditor session={session} model={model} data={data} close={() => setModel(null)} />}
          </>
        )
      )}
    </section>
  );
}
