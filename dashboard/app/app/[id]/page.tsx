import Link from 'next/link';
import { notFound } from 'next/navigation';
import { db, formatNumber, velocityLabel, type SnapshotRow } from '@/lib/db';

export const dynamic = 'force-dynamic';

export default async function AppDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const appId = Number(id);
  if (Number.isNaN(appId)) notFound();

  const supabase = db();
  const [appRes, snapRes, reviewRes, scoreRes] = await Promise.all([
    supabase.from('app').select('*').eq('id', appId).single(),
    supabase.from('snapshot').select('day, install_exact, rating_count, best_rank')
      .eq('app_id', appId).order('day', { ascending: true }),
    supabase.from('review_daily').select('day, n').eq('app_id', appId).order('day', { ascending: true }),
    supabase.from('breakout_score').select('scored_on, score, components')
      .eq('app_id', appId).order('scored_on', { ascending: false }).limit(1),
  ]);

  if (appRes.error || !appRes.data) notFound();
  const app = appRes.data;
  const snaps = (snapRes.data ?? []) as SnapshotRow[];
  const reviews = (reviewRes.data ?? []) as { day: string; n: number }[];
  const latest = scoreRes.data?.[0];
  const components = (latest?.components ?? {}) as Record<string, unknown>;

  const cumulative = snaps.map((s) =>
    app.store === 'play' ? s.install_exact : s.rating_count
  );

  return (
    <main>
      <p><Link href="/">← back to ranking</Link></p>

      <header className="head">
        <h1>{app.name ?? '(unnamed)'}</h1>
        <p className="sub">
          {app.developer ?? '—'} · {app.store} · {app.category ?? 'uncategorised'} ·
          released {app.released ?? 'unknown'}
          {latest && <> · score <strong>{Number(latest.score).toFixed(2)}</strong></>}
        </p>
      </header>

      <section>
        <h2>Cumulative {app.store === 'play' ? 'installs' : 'ratings'}</h2>
        {cumulative.filter((v) => v !== null).length > 1 ? (
          <Sparkline values={cumulative} />
        ) : (
          <p className="empty">
            Needs two days of snapshots. Velocity cannot exist before the second
            measurement — this is expected on a newly discovered app, not a fault.
          </p>
        )}
      </section>

      <section>
        <h2>Review arrival</h2>
        {reviews.length > 1 ? (
          <>
            <Sparkline values={reviews.map((r) => r.n)} />
            <p className="note">
              Reconstructed from backfilled review timestamps, so it reaches back before this
              app was first seen. Written reviews run at roughly 5% of star ratings, and the
              feed caps at 500 per app.
            </p>
          </>
        ) : (
          <p className="empty">No review history backfilled yet.</p>
        )}
      </section>

      <section>
        <h2>Snapshots</h2>
        <table>
          <thead>
            <tr>
              <th>Day</th>
              <th className="num">{app.store === 'play' ? 'Installs' : 'Ratings'}</th>
              <th className="num">Chart rank</th>
            </tr>
          </thead>
          <tbody>
            {[...snaps].reverse().slice(0, 30).map((s) => (
              <tr key={s.day}>
                <td>{s.day}</td>
                <td className="num">
                  {formatNumber(app.store === 'play' ? s.install_exact : s.rating_count)}
                </td>
                <td className="num">{s.best_rank ? `#${s.best_rank}` : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section>
        <h2>Why this score</h2>
        <p className="note">
          Every input is persisted, so a ranking can always be explained after the fact.
          Velocity here is {velocityLabel(app.store)}.
        </p>
        <table>
          <tbody>
            {Object.entries(components).sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => (
              <tr key={k}>
                <td className="dim">{k}</td>
                <td className="num">{v === null ? '—' : String(v)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}

/**
 * Inline SVG rather than a charting library: one dependency fewer, no client JS, and the
 * shape is all that matters at this size. Nulls are dropped rather than treated as zero —
 * a missing measurement is not a crash to zero, and drawing it as one would invent a story.
 */
function Sparkline({ values }: { values: (number | null)[] }) {
  const points = values.filter((v): v is number => v !== null && v !== undefined);
  if (points.length < 2) return null;

  const width = 640;
  const height = 120;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;

  const d = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * width;
      const y = height - ((v - min) / span) * (height - 8) - 4;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  return (
    <figure className="spark">
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height}
           preserveAspectRatio="none" role="img"
           aria-label={`Trend from ${min.toLocaleString()} to ${max.toLocaleString()}`}>
        <path d={d} fill="none" stroke="currentColor" strokeWidth="2" />
      </svg>
      <figcaption className="dim">
        {min.toLocaleString()} → {max.toLocaleString()} over {points.length} days
      </figcaption>
    </figure>
  );
}
