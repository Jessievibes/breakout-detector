import Link from 'next/link';
import { notFound } from 'next/navigation';
import { sql, formatDate, formatNumber, velocityLabel } from '@/lib/db';

export const dynamic = 'force-dynamic';

type AppRow = {
  id: number;
  store: string;
  name: string | null;
  developer: string | null;
  category: string | null;
  released: unknown;
  clone_suspect: boolean;
  relaunch_suspect: boolean;
};
type SnapRow = {
  day: unknown;
  install_exact: string | null;
  rating_count: number | null;
  best_rank: number | null;
  chart_count: number | null;
};

export default async function AppDetail({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const appId = Number(id);
  if (!Number.isInteger(appId)) notFound();

  const db = sql();
  const [apps, snaps, reviews, scores] = await Promise.all([
    db<AppRow[]>`select id, store, name, developer, category, released,
                        clone_suspect, relaunch_suspect
                   from app where id = ${appId}`,
    db<SnapRow[]>`select day, install_exact, rating_count, best_rank, chart_count
                    from snapshot where app_id = ${appId} order by day`,
    db<{ day: string; n: number }[]>`select day, n from review_daily
                                      where app_id = ${appId} order by day`,
    db<{ score: string; components: Record<string, unknown> }[]>`
        select score, components from breakout_score
         where app_id = ${appId} order by scored_on desc limit 1`,
  ]);

  const app = apps[0];
  if (!app) notFound();

  const latest = scores[0];
  const components = latest?.components ?? {};
  const isPlay = app.store === 'play';
  const cumulative = snaps.map((s) =>
    isPlay ? (s.install_exact === null ? null : Number(s.install_exact)) : s.rating_count
  );

  return (
    <main>
      <p><Link href="/">← back to ranking</Link></p>

      <header className="head">
        <h1>{app.name ?? '(unnamed)'}</h1>
        <p className="sub">
          {app.developer ?? '—'} · {app.store} · {app.category ?? 'uncategorised'} · released{' '}
          {formatDate(app.released)}
          {latest && <> · score <strong>{Number(latest.score).toFixed(2)}</strong></>}
          {app.clone_suspect && <span className="tag warn">clone farm</span>}
          {app.relaunch_suspect && (
            <span className="tag warn" title="Review history predates the claimed release date">
              relaunch
            </span>
          )}
        </p>
      </header>

      <section>
        <h2>Cumulative {isPlay ? 'installs' : 'ratings'}</h2>
        {cumulative.filter((v) => v !== null).length > 1 ? (
          <Sparkline values={cumulative} />
        ) : (
          <p className="empty">
            Needs two days of snapshots. Velocity cannot exist before the second measurement —
            expected on a newly discovered app, not a fault.
          </p>
        )}
      </section>

      <section>
        <h2>Review arrival</h2>
        {reviews.length > 1 ? (
          <>
            <Sparkline values={reviews.map((r) => Number(r.n))} />
            <p className="note">
              Reconstructed from backfilled review timestamps, so it reaches back before this
              app was first seen. Written reviews run at roughly 5% of star ratings.
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
              <th className="num">{isPlay ? 'Installs' : 'Ratings'}</th>
              <th className="num">Best rank</th>
              <th className="num">Charts</th>
            </tr>
          </thead>
          <tbody>
            {[...snaps].reverse().slice(0, 30).map((s) => (
              <tr key={formatDate(s.day)}>
                <td>{formatDate(s.day)}</td>
                <td className="num">
                  {formatNumber(isPlay ? s.install_exact : s.rating_count)}
                </td>
                <td className="num">{s.best_rank ? `#${s.best_rank}` : '—'}</td>
                <td className="num">{s.chart_count ?? '—'}</td>
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
            {Object.entries(components)
              .sort(([a], [b]) => a.localeCompare(b))
              .map(([k, v]) => (
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
  const points = values.filter((v): v is number => v !== null && !Number.isNaN(v));
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
        {min.toLocaleString()} → {max.toLocaleString()} over {points.length} points
      </figcaption>
    </figure>
  );
}
