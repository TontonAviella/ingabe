import { describe, expect, it } from 'vitest';
import { chatShouldConnect } from '@/lib/chatConnection';

const base = { conversationId: 7, isReady: true, isSignedOut: false, isTabVisible: true, hiddenTimeoutExpired: false };

describe('the Sage chat connection', () => {
  it('connects in a conversation when signed in', () => {
    expect(chatShouldConnect(base)).toBe(true);
  });

  it('never connects while signed out, so no pop-ups on the sign-in screen (2026-10-24)', () => {
    expect(chatShouldConnect({ ...base, isSignedOut: true })).toBe(false);
  });

  it('waits until the sign-in is known', () => {
    expect(chatShouldConnect({ ...base, isReady: false })).toBe(false);
  });

  it('stays closed outside a conversation and after a long hidden tab', () => {
    expect(chatShouldConnect({ ...base, conversationId: null })).toBe(false);
    expect(chatShouldConnect({ ...base, isTabVisible: false, hiddenTimeoutExpired: true })).toBe(false);
    expect(chatShouldConnect({ ...base, isTabVisible: false, hiddenTimeoutExpired: false })).toBe(true);
  });
});
