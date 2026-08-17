import postgres from 'postgres';

/**
 * Server-only Postgres client.
 *
 * Uses DATABASE_URL — the same credential the jobs use — rather than a Supabase service-role
 * key. Three reasons: it is one secret instead of three, it keeps a full-access API key out
 * of Vercel entirely, and it lets these queries be tested against the real database during
 * development instead of only discovering problems at deploy time.
 *
 * **Use the transaction pooler (port 6543) here**, not the session pooler the jobs use.
 * Serverless functions open and discard connections constantly; transaction mode is built
 * for exactly that, while session mode holds a real backend per connection and will exhaust
 * the database under any traffic at all.
 *
 * Never import this from a Client Component. Every table has RLS enabled with zero policies,
 * so this connection is the only way in — which is the point, and also why it must stay on
 * the server.
 */
declare global {
  // eslint-disable-next-line no-var
  var _sql: ReturnType<typeof postgres> | undefined;
}

export function sql() {
  if (!globalThis._sql) {
    const url = process.env.DATABASE_URL;
    if (!url) {
      throw new Error(
        'DATABASE_URL is not set (Vercel → Settings → Environment Variables). ' +
          'Use the Supabase transaction pooler on port 6543.'
      );
    }
    globalThis._sql = postgres(url, {
      max: 1, // one connection per serverless instance; the pooler does the real pooling
      idle_timeout: 20,
      connect_timeout: 15,
      prepare: false, // transaction-mode pooling cannot keep prepared statements alive
    });
  }
  return globalThis._sql;
}

export type BreakoutRow = {
  app_id: number;
  score: string;
  store: 'ios' | 'play';
  name: string | null;
  developer: string | null;
  category: string | null;
  released: string | null;
  age_days: number | null;
  discovered_via: string | null;
  clone_suspect: boolean;
  cold_start: boolean | null;
  velocity_per_day: string | null;
  momentum: string | null;
  chart_rank: number | null;
};

/** Velocity units differ by store and must never be presented as one number. */
export function velocityLabel(store: string) {
  return store === 'play' ? 'installs/day' : 'ratings/day';
}

/**
 * postgres.js returns `date` and `timestamptz` columns as JavaScript Date objects, not
 * strings. Rendering one directly throws "Objects are not valid as a React child", which is
 * a runtime error rather than a type error — the driver types these as `any`, so the
 * compiler cannot catch it. Everything date-shaped goes through here.
 */
export function formatDate(value: unknown): string {
  if (!value) return '—';
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  return String(value).slice(0, 10);
}

export function formatNumber(n: number | string | null | undefined, digits = 0) {
  if (n === null || n === undefined || n === '') return '—';
  const v = typeof n === 'string' ? Number(n) : n;
  if (Number.isNaN(v)) return '—';
  return v.toLocaleString('en-US', { maximumFractionDigits: digits });
}
