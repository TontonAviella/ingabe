import { describe, expect, it } from 'vitest';
import { isConnectionFailure, serverFailureMessage } from './mapLoadError';

describe('isConnectionFailure', () => {
  it('is true when the request got no answer', () => {
    // MapLibre's AJAXError for a tile on a dropped connection, as seen on the phone
    expect(isConnectionFailure(0, 'AJAXError: Failed to fetch (0): https://example.test/api/layer/L1/21/1/2.png')).toBe(true);
    expect(isConnectionFailure(undefined, 'Failed to fetch')).toBe(true);
    expect(isConnectionFailure(undefined, 'NetworkError when attempting to fetch resource.')).toBe(true);
    expect(isConnectionFailure(undefined, 'Load failed')).toBe(true);
  });

  it('is false when the server answered, whatever the words', () => {
    expect(isConnectionFailure(500, 'Failed to fetch')).toBe(false);
    expect(isConnectionFailure(401, 'Unauthorized')).toBe(false);
    expect(isConnectionFailure(undefined, 'Bad response code: 423')).toBe(false);
    expect(isConnectionFailure(undefined, undefined)).toBe(false);
  });
});

describe('serverFailureMessage', () => {
  it('is the same line for every tile with the same status', () => {
    expect(serverFailureMessage(500)).toBe(serverFailureMessage(500));
    expect(serverFailureMessage(500)).not.toContain('http');
    expect(serverFailureMessage(500)).not.toBe(serverFailureMessage(503));
  });
});
