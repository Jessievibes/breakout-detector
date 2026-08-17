# Dashboard (Phase 4)

Next.js 15 App Router, deployed on Vercel, reading Supabase through the service role from
server components only.

**Status: builds clean and verified against the live database.** Ranked table, filters,
detail pages, and the password gate all confirmed working locally.

## Deploy

1. Import the repo at vercel.com, set **Root Directory** to `dashboard`.
2. Add two environment variables:
   - `DATABASE_URL` — the Supabase **transaction pooler** URI, port **6543** (not the
     session pooler on 5432 the jobs use; see below)
   - `DASHBOARD_PASSWORD` — anything you like
3. Deploy.

Locally: `npm install && npm run build && npm start`, with the same two variables in
`.env.local`.

## Why DATABASE_URL rather than a Supabase service key

Originally written against the Supabase service-role key. Switched to the same
`DATABASE_URL` the jobs use because it is one secret instead of three, it keeps a
full-access API key out of Vercel entirely, and it made these queries testable against the
real database during development rather than only at deploy time — which caught two runtime
bugs the type checker could not (see below).

`lib/db.ts` is server-only. Never import it from a Client Component and never prefix the
variable with `NEXT_PUBLIC_`: either would ship a database credential to every visitor.
Every table has RLS enabled with zero policies, so this connection is the only way in.

**Use the transaction pooler (6543), not the session pooler (5432).** Serverless functions
open and discard connections constantly; transaction mode is built for that, while session
mode holds a real backend per connection and will exhaust the database.

## A trap worth knowing

postgres.js returns `date` and `timestamptz` columns as JavaScript `Date` objects, not
strings. Rendering one directly throws "Objects are not valid as a React child" at runtime —
and the driver types them loosely enough that the compiler does not catch it. Everything
date-shaped goes through `formatDate()`.

## Auth

One password via HTTP Basic in `middleware.ts`. Browsers and password managers handle it
natively, and the dashboard fails closed if `DASHBOARD_PASSWORD` is unset. Swap for Supabase
Auth magic links if it ever needs more than one user.
