import Link from 'next/link';
import { sql, formatNumber, velocityLabel, type BreakoutRow } from '@/lib/db';

export const dynamic = 'force-dynamic';

type Search = { store?: string; days?: string; via?: string; clones?: string };

export default async function Page({ searchParams }: { searchParams: Promise<Search> }) {
  const sp = await searchParams;
  const maxDays = Number(sp.days ?? 120) || 120;
  const store = sp.store === 'ios' || sp.store === 'play' ? sp.store : null;
  const via = sp.via || null;
  // Clone farms are hidden by default rather than deleted — scoring already applies a trust
  // penalty, and being able to show them is how the heuristic gets tuned.
  const showClones = sp.clones === 'show';

  let rows: BreakoutRow[] = [];
  let error: string | null = null;

  try {
    const db = sql();
    rows = (await db<BreakoutRow[]>`
      select app_id, score, store, name, developer, category, released, age_days,
             discovered_via, clone_suspect, cold_start, velocity_per_day, momentum, chart_rank
        from breakout_today
       where age_days <= ${maxDays}
         and (${store}::text is null or store = ${store}::store_kind)
         and (${via}::text is null or discovered_via = ${via})
         and (${showClones} or clone_suspect = false)
       limit 200
    `) as unknown as BreakoutRow[];
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }

  return (
    <main>
      <header className="head">
        <h1>Breakout Detector</h1>
        <p className="sub">
          {error ? 'query failed' : `${rows.length} apps · higher score = faster-growing for its age`}
        </p>
      </header>

      <nav className="filters">
        <Filter label="Store" param="store" current={sp.store} sp={sp}
                options={[['', 'All'], ['ios', 'iOS'], ['play', 'Play']]} />
        <Filter label="Max age" param="days" current={sp.days} sp={sp}
                options={[['30', '30d'], ['60', '60d'], ['120', '120d']]} />
        <Filter label="Channel" param="via" current={sp.via} sp={sp}
                options={[['', 'All'], ['search', 'Search'], ['chart', 'Chart'],
                          ['developer', 'Developer'], ['newapps_feed', 'New feed']]} />
        <Filter label="Clone farms" param="clones" current={sp.clones} sp={sp}
                options={[['', 'Hidden'], ['show', 'Shown']]} />
      </nav>

      {error && <p className="error">{error}</p>}

      {!error && (
        <table>
          <thead>
            <tr>
              <th className="num">#</th>
              <th className="num">Score</th>
              <th>App</th>
              <th>Store</th>
              <th className="num">Age</th>
              <th className="num">Velocity</th>
              <th className="num">Rank</th>
              <th>Found via</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={r.app_id}>
                <td className="num dim">{i + 1}</td>
                <td className="num strong">{Number(r.score).toFixed(2)}</td>
                <td>
                  <Link href={`/app/${r.app_id}`}>{r.name ?? '(unnamed)'}</Link>
                  <span className="dim"> · {r.developer ?? '—'}</span>
                  {r.cold_start && (
                    <span className="tag" title="Scored from backfilled review history, not yet from our own snapshots">
                      cold start
                    </span>
                  )}
                  {r.clone_suspect && <span className="tag warn">clone farm</span>}
                </td>
                <td>{r.store}</td>
                <td className="num">{r.age_days ?? '—'}d</td>
                <td className="num">
                  {formatNumber(r.velocity_per_day)}
                  <span className="dim unit"> {velocityLabel(r.store)}</span>
                </td>
                <td className="num">{r.chart_rank ? `#${r.chart_rank}` : '—'}</td>
                <td className="dim">{r.discovered_via ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {!error && rows.length === 0 && <p className="empty">No apps match these filters.</p>}

      <footer className="note">
        Velocity is exact daily installs on Play and daily rating counts on iOS. The two are
        never combined — scores are ranked within a store, so an app only competes with apps
        measured the same way. Velocity is blank until an app has two days of snapshots.
      </footer>
    </main>
  );
}

function Filter({ label, param, current, options, sp }: {
  label: string;
  param: string;
  current?: string;
  options: [string, string][];
  sp: Search;
}) {
  return (
    <div className="filter">
      <span className="filter-label">{label}</span>
      {options.map(([value, text]) => {
        const next = new URLSearchParams(sp as Record<string, string>);
        if (value) next.set(param, value);
        else next.delete(param);
        const active = (current ?? '') === value;
        return (
          <Link key={value || 'all'} href={`/?${next.toString()}`}
                className={active ? 'chip active' : 'chip'}>
            {text}
          </Link>
        );
      })}
    </div>
  );
}
