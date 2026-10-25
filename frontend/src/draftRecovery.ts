import type { DraftFields } from './types';

/** Keep the request and its idempotency key as one durable record before POST. */
export interface PendingDraftCreate {
  key: string;
  body: string;
  fields: DraftFields;
}

const storageKey = (userId: string) => `credit-pending-draft:${userId}`;

export function readPendingDraft(userId: string): PendingDraftCreate | null {
  const saved = sessionStorage.getItem(storageKey(userId));
  if (!saved) return null;
  const request = JSON.parse(saved) as { key: string; body: string };
  return { ...request, fields: JSON.parse(request.body) as DraftFields };
}

export function prepareDraftCreate(userId: string, fields: DraftFields): PendingDraftCreate {
  const pending = readPendingDraft(userId);
  if (pending) return pending;
  const request = { key: crypto.randomUUID(), body: JSON.stringify(fields) };
  sessionStorage.setItem(storageKey(userId), JSON.stringify(request));
  return { ...request, fields: JSON.parse(request.body) as DraftFields };
}

export function clearPendingDraft(userId: string, key: string): void {
  const pending = readPendingDraft(userId);
  if (pending?.key === key) sessionStorage.removeItem(storageKey(userId));
}
