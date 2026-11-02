/** The user-management search box: a case-insensitive substring match on name or email. */

export interface SearchableUser {
  full_name?: string | null;
  email?: string | null;
}

/**
 * Users whose full name or email contains `term`, ignoring case. A blank term
 * (empty or whitespace only) returns every user, as a new array.
 */
export function filterUsers<T extends SearchableUser>(users: readonly T[], term: string): T[] {
  if (!term.trim()) return [...users];
  const needle = term.toLowerCase();
  return users.filter(
    (user) =>
      (user.full_name && user.full_name.toLowerCase().includes(needle)) ||
      (user.email && user.email.toLowerCase().includes(needle))
  );
}
