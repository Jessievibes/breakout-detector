import { NextRequest, NextResponse } from 'next/server';

/**
 * Single-password gate via HTTP Basic auth.
 *
 * Chosen over a login page because it is a handful of lines, browsers and password managers
 * handle it natively, and this dashboard has exactly one user. Supabase Auth magic links are
 * the alternative if that ever stops being true.
 *
 * Basic auth sends the password on every request, so this is only safe over HTTPS — which
 * Vercel enforces. The comparison is length-safe rather than an early-exit `===`, since the
 * endpoint is public and timing is observable.
 */
export function middleware(request: NextRequest) {
  const expected = process.env.DASHBOARD_PASSWORD;

  // Fail closed. An unset password must not mean an open dashboard.
  if (!expected) {
    return new NextResponse('DASHBOARD_PASSWORD is not configured', { status: 500 });
  }

  const header = request.headers.get('authorization');
  if (header?.startsWith('Basic ')) {
    const decoded = Buffer.from(header.slice(6), 'base64').toString('utf8');
    const supplied = decoded.slice(decoded.indexOf(':') + 1);
    if (timingSafeEqual(supplied, expected)) {
      return NextResponse.next();
    }
  }

  return new NextResponse('Authentication required', {
    status: 401,
    headers: { 'WWW-Authenticate': 'Basic realm="Breakout Detector", charset="UTF-8"' },
  });
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export const config = {
  // Guard everything except Next's own static assets — otherwise the 401 challenge fires
  // for every chunk and the browser prompts repeatedly.
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
