// Whether the project page opens the Sage chat connection, and the one notice it shows while reconnecting.

/** The id of the single "reconnecting" notice: errors update it in place instead of stacking new ones. */
export const CHAT_CONNECTION_TOAST = 'chat-connection';

/** Open the chat connection only in a conversation, with the sign-in known and present (never while signed out:
 * every refused handshake raised a pop-up, six in 30 s on the "Sign in to continue" screen, 2026-10-24), and while
 * the tab is visible or was hidden less than the grace period. */
export function chatShouldConnect(state: {
  conversationId: number | null;
  isReady: boolean;
  isSignedOut: boolean;
  isTabVisible: boolean;
  hiddenTimeoutExpired: boolean;
}): boolean {
  return state.conversationId !== null && state.isReady && !state.isSignedOut && (state.isTabVisible || !state.hiddenTimeoutExpired);
}
