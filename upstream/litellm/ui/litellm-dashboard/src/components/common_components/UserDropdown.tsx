import React, { useMemo, useState } from "react";
import { PaginatedSearchSelect } from "@/components/shared/PaginatedSearchSelect";
import type { SearchSelectOption } from "@/components/shared/SearchSelect";
import { useInfiniteUsers, useUserLookup } from "@/app/(dashboard)/hooks/users/useUsers";
import type { UserInfo } from "@/components/networking";

interface UserDropdownProps {
  value?: string | null;
  onChange: (userId: string | null) => void;
  disabled?: boolean;
  pageSize?: number;
  id?: string;
  showId?: boolean;
}

export const userOptionLabel = (
  user: Pick<UserInfo, "user_id" | "user_alias" | "user_email">,
  showId = true,
): string => {
  if (!showId) return user.user_email || user.user_alias || user.user_id;
  if (user.user_alias) return `${user.user_alias} (${user.user_id})`;
  if (user.user_email) return `${user.user_email} (${user.user_id})`;
  return user.user_id;
};

const UserDropdown: React.FC<UserDropdownProps> = ({ value, onChange, disabled, pageSize = 50, id, showId = true }) => {
  const [search, setSearch] = useState("");

  const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isLoading } = useInfiniteUsers(
    pageSize,
    search || undefined,
  );

  const loadedOptions = useMemo<SearchSelectOption[]>(() => {
    const byId = new Map<string, SearchSelectOption>();
    for (const user of (data?.pages ?? []).flatMap((page) => page.users)) {
      if (byId.has(user.user_id)) continue;
      byId.set(user.user_id, {
        value: user.user_id,
        label: userOptionLabel(user, showId),
      });
    }
    return Array.from(byId.values());
  }, [data, showId]);

  const selectedIsLoaded = loadedOptions.some((option) => option.value === value);
  const { data: selectedUser } = useUserLookup(value && !selectedIsLoaded ? value : null);

  const options = useMemo<SearchSelectOption[]>(() => {
    if (!value || selectedIsLoaded || !selectedUser) return loadedOptions;
    return [
      {
        value: selectedUser.user_id,
        label: userOptionLabel(selectedUser, showId),
      },
      ...loadedOptions,
    ];
  }, [value, selectedIsLoaded, selectedUser, loadedOptions, showId]);

  return (
    <div data-testid="user-dropdown">
      <PaginatedSearchSelect
        options={options}
        value={value}
        onValueChange={onChange}
        onSearchChange={setSearch}
        onLoadMore={fetchNextPage}
        hasNextPage={hasNextPage}
        isLoading={isLoading}
        isFetchingNextPage={isFetchingNextPage}
        placeholder="Search users by email…"
        emptyText="No users found"
        loadingText="Loading users…"
        disabled={disabled}
        inputId={id}
      />
    </div>
  );
};

export default UserDropdown;
