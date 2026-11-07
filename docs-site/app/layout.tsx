import { RootProvider } from 'fumadocs-ui/provider/next';
import type { Metadata } from 'next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import type { ReactNode } from 'react';
import { siteUrl } from '@/lib/shared';
import './global.css';

const inter = Inter({ subsets: ['latin'] });
const mono = JetBrains_Mono({ subsets: ['latin'], variable: '--font-mono' });

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: { template: '%s | OpenGrid Docs', default: 'OpenGrid Docs' },
  description: 'Documentation for the OpenGrid Data API: real-time GPU compute prices, indices, capacity and more.',
  icons: {
    icon: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='5' fill='%23131920'/%3E%3Cpath d='M6 6h12M6 12h12M6 18h12M6 6v12M12 6v12M18 6v12' stroke='%234d94ff' stroke-width='1.6' fill='none'/%3E%3C/svg%3E",
  },
  alternates: { types: { 'text/plain': '/llms.txt' } },
};

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${inter.className} ${mono.variable}`} suppressHydrationWarning>
      <body className="flex flex-col min-h-screen">
        <RootProvider>{children}</RootProvider>
      </body>
    </html>
  );
}
