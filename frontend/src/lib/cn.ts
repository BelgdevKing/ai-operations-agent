/**
 * Join class names, dropping the falsy ones.
 *
 * Deliberately not clsx + tailwind-merge: nothing here depends on a later
 * class overriding an earlier one, and two dependencies to do a conditional
 * join is a poor trade. Adopting shadcn/ui later is the moment to revisit it,
 * since its components expect `cn` to resolve Tailwind conflicts.
 */
export type ClassValue = string | false | null | undefined;

export function cn(...values: ClassValue[]): string {
  return values.filter(Boolean).join(" ");
}
