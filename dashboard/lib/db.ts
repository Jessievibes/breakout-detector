import { createClient } from '@supabase/supabase-js';

/**
 * Server-only Supabase client.
 *
 * Uses the service role, which bypasses RLS — every table has RLS enabled with zero
 * policies, so the anon key can reach nothing even if it leaks. That is the design, and it
 * only holds while this key stays on the server: never import this from a Client Component,
 * and never rename the variable to NEXT_PUBLIC_*, which would inline it into the browser
 * bundle and hand every visitor full read/write access to the database.
 *
 * PostgREST over HTTP rather than a direct Postgres connection, deliberately: serverless
 * functions open and discard connections constantly, and a connection pool is the wrong
 * shape for that. The jobs use psycopg directly because they are long-lived batch processes.
 */
export function db() {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_SERVICE_KEY;
  if (!url || !key) {
    throw new Error(
      'SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (Vercel → Project → Settings → Environment Variables)'
    );
  }
  return createClient(url, key, { auth: { persistSession: false } });
}

export type BreakoutRow = {
  app_id: number;
  score: number;
  store: 'ios' | 'play';
  name: string | null;
  developer: string | null;
  category: string | null;
  released: string | null;
  age_days: number | null;
  discovered_via: string | null;
  clone_suspect: boolean;
  cold_start: boolean | null;
  velocity_per_day: number | null;
  momentum: number | null;
  chart_rank: number | null;
  components: Record<string, unknown>;
};

export type SnapshotRow = {
  day: string;
  install_exact: number | null;
  rating_count: number | null;
  best_rank: number | null;
};

/** Velocity units differ by store and must never be presented as one number. */
export function velocityLabel(store: string) {
  return store === 'play' ? 'installs/day' : 'ratings/day';
}

export function formatNumber(n: number | null | undefined, digits = 0) {
  if (n === null || n === undefined) return '—';
  return Number(n).toLocaleString('en-US', { maximumFractionDigits: digits });
}
