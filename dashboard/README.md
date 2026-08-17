# Dashboard (Phase 4)

Next.js 15 App Router, deployed on Vercel, reading Supabase through the service role from
server components only.

**Status: written but never compiled or deployed.** There is no Node toolchain on the
machine this was built on, so nothing here has been type-checked or run. Treat the first
`npm run build` as the real test.

## Deploy

1. `npm install` in this directory (needs Node 20+).
2. `npm run build` — fix anything the compiler objects to before deploying.
3. Import the repo at vercel.com, set **Root Directory** to `dashboard`.
4. Add the three environment variables from `.env.example`.

## Why the service role, and what keeps it safe

Every table has RLS enabled with zero policies, so the anon key can read nothing at all.
The dashboard therefore uses the service-role key, which bypasses RLS — and that is only
safe while the key stays server-side. `lib/db.ts` is imported exclusively from Server
Components. Never import it from a Client Component, and never rename the variable to
`NEXT_PUBLIC_*`: either would ship a full read/write database credential to every visitor.

## Auth

One password via HTTP Basic in `middleware.ts`. Browsers and password managers handle it
natively, and the dashboard fails closed if `DASHBOARD_PASSWORD` is unset. Swap for Supabase
Auth magic links if it ever needs more than one user.
