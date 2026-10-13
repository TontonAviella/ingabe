import { describe, expect, it } from 'vitest';
import { splitSources } from './sageReplyText';

describe('splitSources', () => {
  it('takes the sources line off the answer', () => {
    const reply = '**Plot 175 has about 1,280 plants.**\n\n- 1.6 plants per m²\n\nSources: drone photo · Sentinel-2 · CHIRPS rain.';
    expect(splitSources(reply)).toEqual({
      body: '**Plot 175 has about 1,280 plants.**\n\n- 1.6 plants per m²',
      sources: ['drone photo', 'Sentinel-2', 'CHIRPS rain'],
    });
  });

  it('reads a bold or italic label and the old parenthesised form', () => {
    expect(splitSources('Done.\n**Sources:** (iSDAsoil 30m; WaPOR v3)').sources).toEqual(['iSDAsoil 30m', 'WaPOR v3']);
    expect(splitSources('Done.\n_Source: Sentinel-2_').sources).toEqual(['Sentinel-2']);
  });

  it('leaves an answer without sources as it is', () => {
    expect(splitSources('The layer is on the map.')).toEqual({ body: 'The layer is on the map.', sources: [] });
  });
});
