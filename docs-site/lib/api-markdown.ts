import { dataApiUrl } from './shared';

type Json = Record<string, any>;

interface OpenAPIData {
  getSchema: () => { bundled: unknown };
  getOpenAPIPageProps: () => unknown;
}

function deref(doc: Json, v: any): any {
  if (v && typeof v === 'object' && typeof v.$ref === 'string') {
    return v.$ref
      .replace(/^#\//, '')
      .split('/')
      .reduce((o: any, k: string) => o?.[k], doc);
  }
  return v;
}

function typeOf(s: Json): string {
  const t = Array.isArray(s.type) ? s.type.join(' | ') : (s.type ?? 'any');
  return t === 'array' && s.items?.type ? `array of ${typeOf(s.items)}` : t;
}

/** Field list of a response schema, two levels deep: enough to read a body without the reference page. */
function fields(doc: Json, schema: Json, prefix = '', depth = 0): string[] {
  const s = deref(doc, schema) ?? {};
  const target = s.type === 'array' || (Array.isArray(s.type) && s.type.includes('array')) ? deref(doc, s.items) ?? {} : s;
  const props: Json = target.properties ?? {};
  const out: string[] = [];
  for (const [name, raw] of Object.entries(props)) {
    const p = deref(doc, raw) ?? {};
    out.push(`${'  '.repeat(depth)}- \`${prefix}${name}\` (${typeOf(p)})${p.description ? `: ${p.description}` : ''}`);
    if (depth < 1) out.push(...fields(doc, p, '', depth + 1));
  }
  return out;
}

/** An API reference page as markdown, for llms.txt, "Copy Markdown" and `Accept: text/markdown`. */
export function operationMarkdown(data: OpenAPIData): string {
  const doc = data.getSchema().bundled as Json;
  const props = data.getOpenAPIPageProps() as { operations?: { path: string; method: string }[] };
  const parts: string[] = [];
  for (const { path, method } of props.operations ?? []) {
    const op: Json = doc.paths?.[path]?.[method] ?? {};
    parts.push(`## ${op.summary ?? `${method.toUpperCase()} ${path}`}`);
    parts.push(`\`${method.toUpperCase()} ${dataApiUrl}${path}\``);
    if (op.description) parts.push(op.description);

    const params = (op.parameters ?? []).map((p: Json) => deref(doc, p));
    if (params.length) {
      parts.push('### Parameters');
      parts.push(
        params
          .map(
            (p: Json) =>
              `- \`${p.name}\` (${p.in}${p.required ? ', required' : ''}, ${typeOf(p.schema ?? {})})` +
              `${p.description ? `: ${p.description}` : ''}`,
          )
          .join('\n'),
      );
    }

    const body = op.requestBody?.content?.['application/json']?.schema;
    if (body) {
      parts.push('### Request body (JSON)');
      parts.push(fields(doc, body).join('\n') || '- (object)');
    }

    const ok = op.responses?.['200']?.content;
    const json = ok?.['application/json']?.schema;
    if (json) {
      const dataSchema = deref(doc, json).properties?.data;
      parts.push('### Response `data`');
      parts.push(
        fields(doc, dataSchema ?? {}).join('\n') ||
          (dataSchema?.description ?? 'See the Responses page for the envelope.'),
      );
      parts.push('Every success is `{"data": ..., "meta": {as_of, api_version, request_id, next_cursor?}}`.');
    } else if (ok) {
      parts.push(`### Response\n\n${Object.keys(ok).join(', ')}`);
    }

    const example = path.replace(/\{([a-z_]+)\}/g, (_m, name) => {
      const p = params.find((x: Json) => x.name === name);
      return p?.example ?? `{${name}}`;
    });
    parts.push(
      '### Example\n\n```bash\n' +
        `curl ${method === 'get' ? '' : `-X ${method.toUpperCase()} `}"${dataApiUrl}${example}" \\\n` +
        '  -H "Authorization: Bearer $OPENGRID_API_KEY"\n```',
    );
  }
  return parts.join('\n\n');
}
