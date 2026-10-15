import { describe, expect, it } from 'vitest';
import { ago, inviteMessage } from '@/pages/OrganizationMembers';

describe('the people page', () => {
  it('says how long ago in plain words', () => {
    const now = new Date('2026-10-15T12:00:00Z');
    expect(ago('2026-10-15T08:00:00Z', now)).toBe('today');
    expect(ago('2026-10-14T08:00:00Z', now)).toBe('yesterday');
    expect(ago('2026-10-10T12:00:00Z', now)).toBe('5 days ago');
    expect(ago(null, now)).toBeNull();
    expect(ago('not a date', now)).toBeNull();
  });

  it('writes the message to send with the invitation link, or the site when there is none', () => {
    const withLink = inviteMessage('a@gmail.com', 'Ingabe', 'https://auth.example/invite?t=1', 'https://gis.nozalabs.rw');
    expect(withLink).toContain('Open your invitation: https://auth.example/invite?t=1');
    expect(withLink).toContain('"Continue with Google" with a@gmail.com');
    expect(inviteMessage('a@gmail.com', 'Ingabe', null, 'https://gis.nozalabs.rw')).toContain('Open https://gis.nozalabs.rw');
  });
});
