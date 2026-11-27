import { useId } from "react";
import type { Access } from "@/lib/http/subscriptionFairUse";
import TeamDropdown from "@/components/common_components/team_dropdown";
import UserDropdown from "@/components/common_components/UserDropdown";
export const groupLabel = { teams: "Team", users: "User" };

export function MemberPicker({
  group,
  value = null,
  onSelect,
}: {
  group: keyof Access;
  value?: string | null;
  onSelect: (id: string | null) => void;
}) {
  const id = useId();
  const Picker = group === "teams" ? TeamDropdown : UserDropdown;
  return (
    <div className="min-w-0 space-y-1">
      <label htmlFor={id}>Add {group === "teams" ? "team" : "user"}</label>
      <Picker id={id} showId={false} value={value} onChange={onSelect} />
    </div>
  );
}
