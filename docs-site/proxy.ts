import { NextResponse, type NextRequest } from 'next/server';
import { isMarkdownPreferred } from 'fumadocs-core/negotiation';
import { docsContentRoute } from '@/lib/shared';

/** Markdown for agents: `/quickstart.md`, or any page with `Accept: text/markdown`. */
export default function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (pathname.endsWith('.md')) {
    const page = pathname.slice(0, -3).replace(/^\/index$/, '');
    return NextResponse.rewrite(new URL(`${docsContentRoute}${page}/content.md`, request.nextUrl));
  }
  if (isMarkdownPreferred(request)) {
    const page = pathname === '/' ? '' : pathname;
    return NextResponse.rewrite(new URL(`${docsContentRoute}${page}/content.md`, request.nextUrl), {
      headers: { Vary: 'Accept' },
    });
  }
  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!_next/|api/|llms|openapi\\.json|robots\\.txt|sitemap\\.xml).*)'],
};
