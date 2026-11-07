'use client';
import { createOpenAPIPage } from 'fumadocs-openapi/ui';

// The playground sends requests straight from the visitor's browser to data.tryopengrid.com with the key they
// paste: no proxy, no shared key. Anonymous visitors can read the reference but every request needs their own key.
export const OpenAPIPage = createOpenAPIPage({
  storageKeyPrefix: 'opengrid-docs-',
});
