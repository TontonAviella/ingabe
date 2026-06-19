import { describe, expect, it } from 'vitest';

import { isBrowserDuckDbWasmAvailable, loadDuckDbWasmModule } from './duckdbWasm';

describe('DuckDB-WASM browser analytics loader', () => {
  it('resolves the real DuckDB-WASM package instead of a Vite stub', async () => {
    const duckdb = await loadDuckDbWasmModule();

    expect(duckdb.PACKAGE_NAME).toBe('@duckdb/duckdb-wasm');
    expect(typeof duckdb.AsyncDuckDB).toBe('function');
    await expect(isBrowserDuckDbWasmAvailable()).resolves.toBe(true);
  });
});
