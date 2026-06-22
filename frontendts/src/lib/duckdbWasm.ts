import type { AsyncDuckDB } from '@duckdb/duckdb-wasm';

type DuckDbWasmModule = typeof import('@duckdb/duckdb-wasm');

let dbPromise: Promise<AsyncDuckDB> | null = null;

export async function loadDuckDbWasmModule(): Promise<DuckDbWasmModule> {
  return import('@duckdb/duckdb-wasm');
}

export async function isBrowserDuckDbWasmAvailable(): Promise<boolean> {
  try {
    await loadDuckDbWasmModule();
    return true;
  } catch {
    return false;
  }
}

export async function createBrowserDuckDb(): Promise<AsyncDuckDB> {
  if (typeof Worker === 'undefined') {
    throw new Error('DuckDB-WASM requires a browser Worker runtime');
  }

  const duckdb = await loadDuckDbWasmModule();
  const bundles = duckdb.getJsDelivrBundles();
  const bundle = await duckdb.selectBundle(bundles);
  if (!bundle.mainModule || !bundle.mainWorker) {
    throw new Error('DuckDB-WASM did not provide a browser bundle');
  }

  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts(${JSON.stringify(bundle.mainWorker)});`], {
      type: 'text/javascript',
    }),
  );
  const worker = new Worker(workerUrl);
  const logger = new duckdb.ConsoleLogger();
  const db = new duckdb.AsyncDuckDB(logger, worker);

  try {
    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  } finally {
    URL.revokeObjectURL(workerUrl);
  }

  return db;
}

export function getBrowserDuckDb(): Promise<AsyncDuckDB> {
  dbPromise ??= createBrowserDuckDb();
  return dbPromise;
}

export async function resetBrowserDuckDbForTests(): Promise<void> {
  const existing = dbPromise;
  dbPromise = null;
  if (!existing) return;

  try {
    const db = await existing;
    await db.terminate();
  } catch {
    // Test cleanup should not mask the original test failure.
  }
}
