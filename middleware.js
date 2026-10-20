// HTTP Basic Auth gate for the hosted dashboard.
//
// Why this exists: on Vercel's Hobby plan the production domain is publicly
// reachable — Vercel Authentication only protects preview URLs, and managed
// Password Protection needs Enterprise or a paid Pro add-on. This is the free
// equivalent, which Vercel's own docs recommend.
//
// It matters because this dashboard carries client names with engagement labels
// and internal competitive assessments, and its own footer says it is not for
// external distribution. An obscure URL is not access control: links get
// forwarded, screenshotted and kept by people after they leave.
//
// Setup, once:
//   1. Vercel project → Settings → Environment Variables
//        DASHBOARD_USER = wya
//        DASHBOARD_PASS = <a long random string>
//      Set for Production (and Preview if you use preview URLs).
//   2. Redeploy.
//
// If DASHBOARD_PASS is unset the site is left OPEN and a warning is logged, so a
// misconfiguration is visible in the Vercel logs rather than silently exposing
// everything. Change the password to revoke access from everyone at once.

export const config = {
  // Gate everything except Vercel's own internals.
  matcher: ['/((?!_vercel|_next/static|favicon\\.ico).*)'],
};

export default function middleware(request) {
  const user = process.env.DASHBOARD_USER || 'wya';
  const pass = process.env.DASHBOARD_PASS;

  // Fail CLOSED. A missing password is a misconfiguration, and the failure mode for
  // a misconfigured gate on confidential material must be "nobody gets in", not
  // "everybody does" — the first deploy happens before the env var is set.
  if (!pass) {
    console.error(
      'DASHBOARD_PASS is not set — refusing all requests. ' +
      'Set it in Vercel project settings and redeploy.'
    );
    return new Response(
      'This dashboard is not configured yet. Set DASHBOARD_PASS in the Vercel project ' +
      'settings and redeploy.',
      { status: 503, headers: { 'Cache-Control': 'no-store',
                                'X-Robots-Tag': 'noindex, nofollow' } }
    );
  }

  const header = request.headers.get('authorization') || '';
  if (header.startsWith('Basic ')) {
    let decoded = '';
    try {
      decoded = atob(header.slice(6));
    } catch {
      decoded = '';
    }
    // Split on the FIRST colon only: passwords may contain colons.
    const i = decoded.indexOf(':');
    const gotUser = i < 0 ? '' : decoded.slice(0, i);
    const gotPass = i < 0 ? '' : decoded.slice(i + 1);
    if (safeEqual(gotUser, user) && safeEqual(gotPass, pass)) {
      return; // authorised — serve the static file
    }
  }

  return new Response('Authentication required.', {
    status: 401,
    headers: {
      'WWW-Authenticate': 'Basic realm="WYA market monitor", charset="UTF-8"',
      'Cache-Control': 'no-store',
      'X-Robots-Tag': 'noindex, nofollow',
    },
  });
}

// Length-independent comparison, so response time does not leak the password.
function safeEqual(a, b) {
  const x = String(a), y = String(b);
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    diff |= (x.charCodeAt(i) || 0) ^ (y.charCodeAt(i) || 0);
  }
  return diff === 0;
}
