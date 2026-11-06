import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appUrl, contactEmail } from './shared';

function Logo() {
  return (
    <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
      <rect width="24" height="24" rx="5" fill="#131920" />
      <path d="M6 6h12M6 12h12M6 18h12M6 6v12M12 6v12M18 6v12" stroke="#4d94ff" strokeWidth="1.6" fill="none" />
    </svg>
  );
}

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: (
        <>
          <Logo />
          <span className="font-semibold">OpenGrid</span>
          <span className="text-fd-muted-foreground font-normal">Docs</span>
        </>
      ),
      url: '/',
    },
    links: [
      { text: 'Terminal', url: appUrl, external: true },
      { text: 'Get an API key', url: `mailto:${contactEmail}?subject=OpenGrid%20Data%20API%20access`, external: true },
    ],
  };
}
