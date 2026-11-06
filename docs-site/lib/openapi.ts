import { createOpenAPI } from 'fumadocs-openapi/server';
import spec from '../openapi.json';

// Generated from the API's code by `python -m api.docs_openapi` (repo root); never edit by hand. Imported, not
// read from disk, so the schema is bundled into the functions that render on demand (search, unknown paths).
export const openapi = createOpenAPI({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  input: { 'data-api': () => spec as any },
});
