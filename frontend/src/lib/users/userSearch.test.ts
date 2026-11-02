import { describe, it, expect } from 'vitest';
import { filterUsers } from './userSearch';

const users = [
  { uuid: 'a', full_name: 'Alice Example', email: 'alice@x.com' },
  { uuid: 'b', full_name: 'Bob Jones', email: 'BOB@Corp.example' },
  { uuid: 'c', full_name: null, email: 'nameless@x.com' },
  { uuid: 'd', full_name: 'No Mail', email: null },
];

const ids = (list: { uuid: string }[]) => list.map((u) => u.uuid);

describe('filterUsers', () => {
  it('returns every user, as a copy, for a blank term', () => {
    for (const term of ['', '   ']) {
      const result = filterUsers(users, term);
      expect(ids(result)).toEqual(['a', 'b', 'c', 'd']);
      expect(result).not.toBe(users);
    }
  });

  it('matches the name or the email, ignoring case', () => {
    expect(ids(filterUsers(users, 'ALICE'))).toEqual(['a']);
    expect(ids(filterUsers(users, 'corp.EXAMPLE'))).toEqual(['b']);
    expect(ids(filterUsers(users, 'x.com'))).toEqual(['a', 'c']);
    expect(ids(filterUsers(users, 'mail'))).toEqual(['d']);
  });

  it('tolerates missing names and emails', () => {
    expect(ids(filterUsers(users, 'nameless'))).toEqual(['c']);
    expect(ids(filterUsers(users, 'zzz'))).toEqual([]);
  });

  it('does not trim a non-blank term before matching', () => {
    expect(ids(filterUsers(users, ' jones'))).toEqual(['b']);
    expect(ids(filterUsers(users, ' alice'))).toEqual([]);
  });
});
