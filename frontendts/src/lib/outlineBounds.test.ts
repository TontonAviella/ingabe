import { describe, expect, it } from 'vitest';
import { boundsOf } from './outlineBounds';

describe('boundsOf', () => {
  it('gives the west-south and east-north corners of a line', () => {
    expect(
      boundsOf([
        [30.2, -1.5],
        [30.1, -1.7],
        [30.4, -1.6],
      ]),
    ).toEqual([
      [30.1, -1.7],
      [30.4, -1.5],
    ]);
  });

  it('has no box for no corners', () => {
    expect(boundsOf([])).toBeNull();
  });
});
