import { createGetUrl } from 'fumadocs-core/source';

export const appName = 'OpenGrid Docs';
export const siteUrl = 'https://docs.tryopengrid.com';
export const dataApiUrl = 'https://data.tryopengrid.com';
export const appUrl = 'https://terminal.tryopengrid.com';
// How a company gets an API key today: the operator team issues every key.
export const contactEmail = 'hello@tryopengrid.com';

export const docsRoute = '/';
export const docsContentRoute = '/llms.mdx';

const getContentUrl = createGetUrl(docsContentRoute);

/** The page as markdown (for agents and "Copy Markdown"): /llms.mdx/<slugs>/content.md */
export function getPageMarkdownUrl(page: { slugs: string[]; locale?: string }) {
  const segments = [...page.slugs, 'content.md'];
  return { segments, url: getContentUrl(segments, page.locale) };
}
