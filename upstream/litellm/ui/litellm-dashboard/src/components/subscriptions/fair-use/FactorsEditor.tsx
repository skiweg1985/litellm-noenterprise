import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { TableRow, TableCell } from "@/components/ui/table";
import { getFactors, saveFactors, type Factors } from "@/lib/http/subscriptionFairUse";
import { fairUseKey, polling, useDirectoryNames, useSaveSetting, type Session } from "./queries";
import { FairUseTable } from "./FairUseTable";
import { MemberPicker, groupLabel } from "./MemberPicker";

type Edit = { group: keyof Factors; id: string; value: string; adding: boolean };

export function FactorsEditor({ session, unknown }: { session: Session; unknown: Record<keyof Factors, Set<string>> }) {
  const query = useQuery({
    queryKey: [...fairUseKey(session), "factors"],
    queryFn: ({ signal }) => getFactors(session.accessToken, signal),
    ...polling,
  });
  const [edit, setEdit] = useState<Edit | null>(null);
  const names = useDirectoryNames(session, Object.keys(query.data?.users ?? {}));
  const save = useSaveSetting(
    session,
    ["factors"],
    (body: Factors) => saveFactors(session.accessToken, body),
    () => setEdit(null),
  );
  function memberName(group: keyof Factors, id: string) {
    return unknown[group].has(id)
      ? "Unknown " + groupLabel[group].toLowerCase() + " · " + id
      : names[group].get(id) ?? id;
  }
  function start(group: keyof Factors, id = "", value = 1) {
    save.reset();
    const entry = { group, id, adding: !id, value: String(value) };
    setEdit(entry);
  }
  function apply(remove: boolean) {
    if (!edit?.id || !query.data) return;
    const factors: Record<string, number> = { ...query.data[edit.group] };
    if (remove) delete factors[edit.id];
    else factors[edit.id] = Number(edit.value);
    save.mutate({ ...query.data, [edit.group]: factors });
  }
  const duplicate = !!(edit?.adding && Object.hasOwn(query.data?.[edit.group] ?? {}, edit.id));
  return (
    <section className="min-w-0 space-y-4" aria-labelledby="factors-heading">
      <h2 id="factors-heading" className="text-xl font-semibold">
        Factors
      </h2>
      {query.isPending && <p role="status">Loading factors…</p>}
      {query.isError ? (
        <p role="alert">Could not load factors.</p>
      ) : (
        query.data && (
          <>
            {(["teams", "users"] as const).map((group) => {
              const label = groupLabel[group];
              const rows = Object.entries(query.data[group] ?? {});
              return (
                <div key={group} className="space-y-2">
                  <div className="flex items-center justify-between gap-2">
                    <h3 className="font-medium">{label}s</h3>
                    <Button variant="outline" size="sm" onClick={() => start(group)}>
                      Add {label.toLowerCase()} factor
                    </Button>
                  </div>
                  {rows.length ? (
                    <FairUseTable name={label + " factors"} columns={[label, "Factor", "Actions"]}>
                      {rows.map(([id, factor]) => {
                        const name = memberName(group, id);
                        return (
                          <TableRow key={id}>
                            <TableCell>{name}</TableCell>
                            <TableCell>{factor === 0 ? "0 · Exempt" : factor}</TableCell>
                            <TableCell>
                              <Button
                                variant="outline"
                                size="sm"
                                aria-label={"Edit factor for " + name}
                                onClick={() => start(group, id, factor)}
                              >
                                Edit
                              </Button>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </FairUseTable>
                  ) : (
                    <p className="text-sm text-muted-foreground">No {label.toLowerCase()} factors.</p>
                  )}
                </div>
              );
            })}
            {edit && (
              <Dialog
                open
                onOpenChange={(open) => {
                  if (!open && !save.isPending) setEdit(null);
                }}
              >
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>
                      {edit.adding
                        ? "Add " + groupLabel[edit.group].toLowerCase() + " factor"
                        : "Edit factor for " + memberName(edit.group, edit.id)}
                    </DialogTitle>
                  </DialogHeader>
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      apply(false);
                    }}
                  >
                    <fieldset className="space-y-3" disabled={save.isPending}>
                      {edit.adding && (
                        <MemberPicker
                          group={edit.group}
                          value={edit.id}
                          onSelect={(id) => setEdit({ ...edit, id: id ?? "" })}
                        />
                      )}
                      <label className="block">
                        Factor
                        <Input
                          type="number"
                          min="0"
                          max="10"
                          step="any"
                          required
                          value={edit.value}
                          onChange={(event) => setEdit({ ...edit, value: event.target.value })}
                        />
                      </label>
                      {edit.value !== "" && Number(edit.value) === 0 && <p>Exempt</p>}
                      {duplicate && <p role="alert">This factor already exists.</p>}
                      {save.isError && <p role="alert">Could not save this factor. Try again.</p>}
                      <div className="flex flex-wrap gap-2">
                        <Button type="submit" disabled={!edit.id || duplicate}>
                          Save factor
                        </Button>
                        {!edit.adding && (
                          <Button type="button" variant="outline" onClick={() => apply(true)}>
                            Remove factor
                          </Button>
                        )}
                      </div>
                    </fieldset>
                  </form>
                </DialogContent>
              </Dialog>
            )}
          </>
        )
      )}
    </section>
  );
}
