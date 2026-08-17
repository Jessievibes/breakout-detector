-- Breakout Detector — schema
-- Idempotent: safe to re-run. Apply with `python -m src.jobs.migrate`.
--
-- Design notes that are easy to lose:
--   * `snapshot.day` is the UTC date a row *intends* to cover, stamped by the job, never
--     `current_date`. Actions cron fires hours late; a late run must still write the right day.
--   * review_daily is a plain view, not materialized. Nothing to refresh, no ordering footgun.
--     Convert only if EXPLAIN says so.
--   * Deltas are always divided by the actual day gap. A missed run makes a 2-day delta, and
--     reporting that as a daily rate would invent a spike.

begin;

do $$ begin
  create type store_kind as enum ('ios', 'play');
exception when duplicate_object then null; end $$;

-- ---------------------------------------------------------------------------
-- app
-- ---------------------------------------------------------------------------

create table if not exists app (
  id                  bigserial primary key,
  store               store_kind not null,
  store_app_id        text not null,
  name                text,
  -- `developer` is the Play D2 crawl key: developer?id=<NAME> works, dev?id= is 404,
  -- and the numeric developer_id has no working URL form. Verified 2026-08-17.
  developer           text,
  developer_id        text,
  category            text,
  released            date,
  price               numeric default 0,
  icon_url            text,
  first_seen          timestamptz not null default now(),
  discovered_via      text,     -- 'search' | 'developer' | 'chart' | 'newapps_feed' | 'similar' | 'seed'
  last_enriched       timestamptz,
  enrich_failures     int not null default 0,   -- consecutive; backs off then delists
  reviews_backfilled  boolean not null default false,
  delisted            boolean not null default false,
  twin_app_id         bigint references app(id),
  twin_confidence     numeric check (twin_confidence between 0 and 1),
  clone_suspect       boolean not null default false,
  relaunch_suspect    boolean not null default false,
  constraint app_store_uniq unique (store, store_app_id)
);

create index if not exists app_released_idx     on app (released desc nulls last);
create index if not exists app_store_cat_idx    on app (store, category);
create index if not exists app_backfill_idx     on app (reviews_backfilled) where reviews_backfilled = false;
-- the enrich queue: live apps, oldest-enriched first
create index if not exists app_enrich_queue_idx on app (store, last_enriched nulls first) where delisted = false;
-- the D2 crawl queue
create index if not exists app_developer_idx    on app (store, developer) where developer is not null;

-- ---------------------------------------------------------------------------
-- snapshot — one row per app per intended UTC day
-- ---------------------------------------------------------------------------

create table if not exists snapshot (
  app_id        bigint not null references app(id) on delete cascade,
  day           date   not null,
  install_exact bigint,    -- play realInstalls. null on ios
  install_min   bigint,    -- play minInstalls (band floor)
  rating_count  int,       -- ios userRatingCount (per-storefront) / play ratings
  avg_rating    numeric,
  review_count  int,       -- text reviews only (~5% of ratings)
  version       text,
  best_rank     int,       -- best position across tracked charts; null = unranked
  primary key (app_id, day)
);

create index if not exists snapshot_day_idx on snapshot (day desc);

-- ---------------------------------------------------------------------------
-- review_event — the day-one history source, immutable after backfill
-- ---------------------------------------------------------------------------

create table if not exists review_event (
  app_id    bigint not null references app(id) on delete cascade,
  review_id text   not null,
  posted_at timestamptz not null,   -- normalized to UTC at ingest
  rating    int check (rating between 1 and 5),
  version   text,
  country   text not null default 'us',
  primary key (app_id, review_id)
);

create index if not exists review_event_app_time_idx on review_event (app_id, posted_at desc);

create or replace view review_daily as
  select app_id,
         (posted_at at time zone 'utc')::date as day,
         count(*)::int as n
    from review_event
   group by 1, 2;

-- ---------------------------------------------------------------------------
-- velocity — deltas normalized per day, gap-aware
-- ---------------------------------------------------------------------------

create or replace view app_velocity as
  select
    app_id,
    day,
    install_exact,
    rating_count,
    (day - lag(day) over w) as day_gap,
    -- Divide by the real gap: a missed run must not read as a one-day spike.
    case when lag(install_exact) over w is not null and day > lag(day) over w
         then (install_exact - lag(install_exact) over w)::numeric / (day - lag(day) over w)
    end as installs_per_day,
    case when lag(rating_count) over w is not null and day > lag(day) over w
         then (rating_count - lag(rating_count) over w)::numeric / (day - lag(day) over w)
    end as ratings_per_day
  from snapshot
  window w as (partition by app_id order by day);

-- ---------------------------------------------------------------------------
-- Phase 1 acceptance view: new Play apps ranked by exact daily installs.
-- "Done when a SQL query returns new Play apps ranked by install rate, with real numbers."
-- ---------------------------------------------------------------------------

create or replace view new_play_apps_by_install_rate as
  select
    a.id,
    a.store_app_id,
    a.name,
    a.developer,
    a.category,
    a.released,
    (current_date - a.released)          as days_since_release,
    a.discovered_via,
    v.day                                as measured_on,
    v.install_exact,
    round(v.installs_per_day)            as installs_per_day,
    v.day_gap
  from app a
  join app_velocity v on v.app_id = a.id
  where a.store = 'play'
    and a.delisted = false
    and a.released is not null
    and a.released > current_date - interval '120 days'
    and v.installs_per_day is not null
    -- most recent measurement per app
    and v.day = (select max(s.day) from snapshot s where s.app_id = a.id)
  order by v.installs_per_day desc nulls last;

-- ---------------------------------------------------------------------------
-- retention target (spec §4): snapshots older than 60 days fold into weeks
-- ---------------------------------------------------------------------------

create table if not exists snapshot_weekly (
  app_id            bigint not null references app(id) on delete cascade,
  week_start        date   not null,   -- monday, utc
  install_exact_max bigint,
  rating_count_max  int,
  review_count_max  int,
  avg_rating_last   numeric,
  best_rank_min     int,
  primary key (app_id, week_start)
);

-- ---------------------------------------------------------------------------
-- breakout_score
-- ---------------------------------------------------------------------------

create table if not exists breakout_score (
  app_id     bigint  not null references app(id) on delete cascade,
  scored_on  date    not null,
  score      numeric not null,
  components jsonb   not null,   -- every raw value, z-value, weight, and cold_start flag
  primary key (app_id, scored_on)
);

create index if not exists breakout_score_rank_idx on breakout_score (scored_on desc, score desc);

-- ---------------------------------------------------------------------------
-- run_log — observability. Do not skip this.
-- ---------------------------------------------------------------------------

create table if not exists run_log (
  id         bigserial primary key,
  job        text not null,
  started_at timestamptz not null default now(),
  ended_at   timestamptz,
  ok         boolean,
  stats      jsonb   -- {fetched, inserted, empty_200, http_403, http_429, null_rate, ...}
);

create index if not exists run_log_job_idx on run_log (job, started_at desc);

commit;
