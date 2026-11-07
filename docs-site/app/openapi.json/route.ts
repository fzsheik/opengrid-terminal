import spec from '@/openapi.json';

export const revalidate = false;

export function GET() {
  return Response.json(spec);
}
