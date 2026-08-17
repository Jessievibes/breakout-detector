-- Phase 5: cross-store twin matching, and snapshot retention.

begin;

create extension if not exists pg_trgm;

-- ---------------------------------------------------------------------------
-- Twin matching
-- ---------------------------------------------------------------------------
--
-- Expectations first, because they matter for how this is used: only about 17% of brand-new
-- apps exist on both stores (measured 2026-08-17, n=12). Most indie launches are
-- single-store. So the matched population is small AND biased toward better-resourced
-- developers — any statistic computed over twins is a statement about that subset, not about
-- new apps generally.
--
-- It is still worth having. A matched pair is the only way to observe the same app through
-- two different instruments: Play's exact install counts beside iOS's rating velocity. That
-- correlation is the one genuinely novel measurement this system can make (spec §11.5), and
-- nobody publishes the ratio.

create or replace function normalize_app_name(name text)
returns text
language sql
immutable
as $$
  -- Case and punctuation only. The FULL name is kept deliberately.
  --
  -- The first version of this stripped everything after the first separator to remove
  -- marketing tails, but the character class included whitespace, so it kept only the first
  -- word: every Adobe app normalized to "adobe" and "Adobe Premiere" matched "Adobe Express"
  -- at confidence 1.00. Even a correct tail-strip is wrong here — "AccuWeather: Forecast"
  -- and "AccuWeather: Radar" are genuinely different apps.
  --
  -- Precision beats recall for twins. A missed pair costs one row of a study; a wrong pair
  -- corrupts the install-to-rating ratio that is the entire reason for matching.
  select nullif(regexp_replace(lower(coalesce(name, '')), '[^a-z0-9]', '', 'g'), '');
$$;

create index if not exists app_name_trgm_idx on app using gin (name gin_trgm_ops);

-- Parameter is double precision, not real: psycopg binds a Python float as double, and
-- Postgres will not implicitly narrow it during function resolution. Dropping the older
-- signature keeps this from becoming an ambiguous overload on re-migration.
drop function if exists match_twins(real);

create or replace function match_twins(min_similarity double precision default 0.55)
returns integer
language plpgsql
as $$
declare
  matched integer;
begin
  -- Re-derive from scratch each run, as a plain statement rather than a data-modifying CTE:
  -- a tightened rule must actually remove the pairs it no longer believes, instead of
  -- leaving yesterday's mistakes in place for a later analysis to trip over.
  update app set twin_app_id = null, twin_confidence = null where twin_app_id is not null;

  with candidates as (
    select i.id                                   as ios_id,
           p.id                                   as play_id,
           similarity(i.name, p.name)             as name_sim,
           (normalize_app_name(i.name) = normalize_app_name(p.name))          as exact_name,
           -- Publisher strings differ across stores more than the apps do: Apple lists
           -- "Airbnb, Inc." where Play lists "Airbnb". Prefix matching either way catches
           -- the legal-suffix case without the false positives a fuzzy threshold invites.
           (normalize_app_name(i.developer) is not null
            and normalize_app_name(p.developer) is not null
            and (normalize_app_name(i.developer) = normalize_app_name(p.developer)
                 or normalize_app_name(i.developer) like normalize_app_name(p.developer) || '%'
                 or normalize_app_name(p.developer) like normalize_app_name(i.developer) || '%')
           ) as same_dev
      from app i
      join app p
        on p.store = 'play'
       and i.store = 'ios'
       -- Cheap equality does most of the work; the trigram index catches near-misses
       -- without a cross join over the whole catalogue.
       and (normalize_app_name(i.name) = normalize_app_name(p.name)
            or i.name % p.name)
     where i.name is not null and p.name is not null
       and i.delisted = false and p.delisted = false
  ),
  graded as (
    select ios_id, play_id, name_sim, exact_name, same_dev,
           case
             -- Same name and same publisher: as certain as this gets.
             when exact_name and same_dev             then 1.00
             -- Same name, different publisher. Often a genuine twin whose store listings
             -- name the publisher differently — but also exactly what a clone looks like.
             when exact_name                          then 0.60
             -- A near-name alone is not evidence: "Remini - AI Photo Enhancer" and
             -- "UpFoto - AI Photo Enhancer" are different apps that share a category.
             -- Requiring the publisher to match is what makes fuzzy matching safe.
             when same_dev and name_sim >= 0.70       then 0.75
           end::numeric as confidence
      from candidates
  ),
  scored as (
    select ios_id, play_id, confidence,
           row_number() over (partition by ios_id  order by confidence desc, name_sim desc) as rn_ios,
           row_number() over (partition by play_id order by confidence desc, name_sim desc) as rn_play
      from graded
     where confidence is not null
       and name_sim >= min_similarity
  ),
  -- One twin each way. Without this, a generic name ("Calculator") pairs a dozen apps and
  -- every one of them is wrong.
  best as (
    select * from scored where rn_ios = 1 and rn_play = 1
  ),
  applied as (
    update app a
       set twin_app_id = b.play_id, twin_confidence = b.confidence
      from best b
     where a.id = b.ios_id
       and (a.twin_app_id is distinct from b.play_id
            or a.twin_confidence is distinct from b.confidence)
    returning a.id
  )
  select count(*) into matched from applied;

  -- Mirror onto the Play side so the link is navigable from either store.
  update app p
     set twin_app_id = i.id, twin_confidence = i.twin_confidence
    from app i
   where i.store = 'ios' and i.twin_app_id = p.id
     and p.twin_app_id is distinct from i.id;

  return matched;
end;
$$;

-- Twin pairs with both stores' latest measurements side by side. This is the view that can
-- answer "how many Play installs does one iOS rating correspond to" once velocity exists.
create or replace view twin_pairs as
  select i.id            as ios_app_id,
         p.id            as play_app_id,
         i.name          as ios_name,
         p.name          as play_name,
         i.twin_confidence,
         i.released      as ios_released,
         p.released      as play_released,
         si.rating_count as ios_ratings,
         sp.install_exact as play_installs,
         vi.ratings_per_day  as ios_ratings_per_day,
         vp.installs_per_day as play_installs_per_day
    from app i
    join app p on p.id = i.twin_app_id and p.store = 'play'
    left join lateral (select rating_count from snapshot
                        where app_id = i.id and rating_count is not null
                        order by day desc limit 1) si on true
    left join lateral (select install_exact from snapshot
                        where app_id = p.id and install_exact is not null
                        order by day desc limit 1) sp on true
    left join lateral (select ratings_per_day from app_velocity
                        where app_id = i.id and ratings_per_day is not null
                        order by day desc limit 1) vi on true
    left join lateral (select installs_per_day from app_velocity
                        where app_id = p.id and installs_per_day is not null
                        order by day desc limit 1) vp on true
   where i.store = 'ios';

-- ---------------------------------------------------------------------------
-- Retention (spec §4)
-- ---------------------------------------------------------------------------
--
-- snapshot grows by one row per app per day forever; review_event does not grow after
-- backfill and is kept indefinitely. Folding old snapshots into weeks keeps the daily table
-- small without losing the shape of the history: max() of a cumulative counter over a week
-- is the week's end value, which is what a rollup of a monotonic series should preserve.

create or replace function roll_up_snapshots(cutoff_days integer default 60)
returns integer
language plpgsql
as $$
declare
  rolled integer;
begin
  insert into snapshot_weekly (app_id, week_start, install_exact_max, rating_count_max,
                               review_count_max, avg_rating_last, best_rank_min)
  select app_id,
         date_trunc('week', day)::date,
         max(install_exact),
         max(rating_count),
         max(review_count),
         (array_agg(avg_rating order by day desc) filter (where avg_rating is not null))[1],
         min(best_rank)
    from snapshot
   where day < current_date - cutoff_days
   group by app_id, date_trunc('week', day)::date
  on conflict (app_id, week_start) do update
     set install_exact_max = greatest(snapshot_weekly.install_exact_max, excluded.install_exact_max),
         rating_count_max  = greatest(snapshot_weekly.rating_count_max,  excluded.rating_count_max),
         review_count_max  = greatest(snapshot_weekly.review_count_max,  excluded.review_count_max),
         avg_rating_last   = coalesce(excluded.avg_rating_last, snapshot_weekly.avg_rating_last),
         best_rank_min     = least(snapshot_weekly.best_rank_min, excluded.best_rank_min);

  get diagnostics rolled = row_count;

  delete from snapshot where day < current_date - cutoff_days;
  return rolled;
end;
$$;

commit;
