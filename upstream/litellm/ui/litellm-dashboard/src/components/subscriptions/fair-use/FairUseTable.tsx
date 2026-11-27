import type { ReactNode } from "react";
import { Table, TableBody, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export function FairUseTable({ name, columns, children }: { name: string; columns: string[]; children: ReactNode }) {
  return (
    <Table aria-label={name}>
      <TableHeader>
        <TableRow>
          {columns.map((column) => (
            <TableHead key={column} scope="col">
              {column}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>{children}</TableBody>
    </Table>
  );
}
