import { docsLlms } from '@/lib/source';

export const revalidate = false;

const HEADER = `# OpenGrid Data API docs

> Real-time GPU compute market data (prices, indices, fixings, capacity, costs, reliability, events, news) over
> one REST API at https://data.tryopengrid.com/v1. Every page is also markdown: add .md to its URL. Everything in
> one file: /llms-full.txt. OpenAPI schema: /openapi.json.
`;

export async function GET() {
  const index = (await docsLlms.index()).replace(/^# .*\n/, '');
  return new Response(`${HEADER}${index}`, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
}
