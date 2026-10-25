/** The filter state of an audit screen, shared by the platform viewer and the organization panel. */
export interface AuditFilterState {
  startDate: string;
  endDate: string;
  eventType: string;
  outcome: string;
  /** Organization view only: the actor, by uuid (the server refuses a non-member). */
  userUuid: string;
  /** Display label of the picked user; never sent to the server. */
  userLabel: string;
  /** Organization view only: everything that happened to one file or collection. */
  resourceUuid: string;
}

export const emptyAuditFilters = (): AuditFilterState => ({
  startDate: '',
  endDate: '',
  eventType: '',
  outcome: '',
  userUuid: '',
  userLabel: '',
  resourceUuid: '',
});

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** True for an empty value or a well-formed uuid; the server answers 422 to anything else. */
export const isAuditResourceUuid = (value: string): boolean =>
  value.trim() === '' || UUID_PATTERN.test(value.trim());
