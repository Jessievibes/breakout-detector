-- Breakout scoring (spec §7), as a SQL function invoked by the Actions workflow.
--
-- Not pg_cron and not an Edge Function: it must run *after* enrichment, and only Actions
-- knows whether the scrape succeeded. pg_cron would happily score yesterday's data.
--
-- Design commitments worth stating, because they are what stops this ranking noise:
--
--   * **Velocity is per-store.** Play gives exact daily installs, iOS gives daily rating
--     counts. They are never mixed into one number — z-scores are computed within a store,
--     so an app only ever competes with apps measured the same way.
--   * **Missing components renormalize, they do not default to zero.** An app with no chart
--     rank should be scored on what is known about it, not penalised for a missing input.
--   * **A volume floor.** Without it, 1 -> 4 installs/day is "300% growth" and outranks
--     everything real.
--   * **Every input is persisted** in breakout_score.components. You will need to answer
--     "why is this #1" and the raw values are the only way to do it.

begin;

create or replace function compute_breakout_scores(target_day date default current_date)
returns integer
language plpgsql
as $$
declare
  inserted integer;
begin

with eligible as (
  -- 120 days keeps the z-score population coherent: a two-year-old app's "momentum" is a
  -- different phenomenon and would distort the distribution these apps are measured against.
  select a.id, a.store, a.released, a.relaunch_suspect, a.developer,
         greatest(target_day - a.released, 1)::numeric as age_days
    from app a
   where a.delisted = false
     and a.released is not null
     and a.released > target_day - 120
),

-- Most recent cumulative total per app. Play: realInstalls. iOS: rating count.
totals as (
  select distinct on (s.app_id)
         s.app_id,
         coalesce(s.install_exact, s.rating_count)::numeric as cumulative,
         s.version,
         s.histogram,
         s.chart_count,
         s.updated_at
    from snapshot s
    join eligible e on e.id = s.app_id
   where s.day <= target_day
   order by s.app_id, s.day desc
),

-- Disjoint windows. v1 of the spec compared a 7-day average against a 30-day average that
-- *contained* it, which damps exactly the acceleration the metric is meant to detect.
vel as (
  select v.app_id,
         avg(coalesce(v.installs_per_day, v.ratings_per_day))
           filter (where v.day > target_day - 7)                       as v7,
         avg(coalesce(v.installs_per_day, v.ratings_per_day))
           filter (where v.day <= target_day - 7 and v.day > target_day - 30) as v_prior,
         avg(coalesce(v.installs_per_day, v.ratings_per_day))
           filter (where v.day > target_day - 3)                       as v3,
         count(*) filter (where v.day > target_day - 30)               as vel_days
    from app_velocity v
    join eligible e on e.id = v.app_id
   where v.day <= target_day
     and coalesce(v.installs_per_day, v.ratings_per_day) is not null
   group by v.app_id
),

-- Cold start: an app we met today has no velocity of our own, but its backfilled reviews
-- reach back before we ever saw it. Written reviews run at ~5% of star ratings (Spotify:
-- 36.1M ratings / 1.85M reviews), so scale by 20 to compare against a rating-count velocity.
cold as (
  select rd.app_id,
         avg(rd.n)::numeric * 20 as v_est,
         sum(rd.n)::numeric      as reviews_7d
    from review_daily rd
    join eligible e on e.id = rd.app_id
   where rd.day > target_day - 7 and rd.day <= target_day
   group by rd.app_id
),

ranks as (
  select s.app_id,
         min(s.best_rank) filter (where s.day = target_day)                        as rank_now,
         min(s.best_rank) filter (where s.day between target_day - 8 and target_day - 6) as rank_then
    from snapshot s
    join eligible e on e.id = s.app_id
   group by s.app_id
),

-- Clone farms: one developer shipping many apps in a narrow window. Cheap to detect and a
-- reliable marker of the shovelware that would otherwise dominate a "new + growing" ranking.
farms as (
  select developer, count(*) as sibling_count
    from app
   where developer is not null
     and released > target_day - 14
   group by developer
  having count(*) >= 5
),

raw as (
  select e.id as app_id,
         e.store,
         e.age_days,
         t.cumulative,
         -- epsilon differs by store because the units do: installs/day vs ratings/day
         case when e.store = 'play' then 5.0 else 0.5 end as eps,
         coalesce(v.v7, c.v_est)                 as v7,
         v.v_prior,
         v.v3,
         coalesce(v.vel_days, 0)                 as vel_days,
         (v.v7 is null and c.v_est is not null)  as cold_start,
         c.reviews_7d,
         t.cumulative / e.age_days               as lifetime_rate,
         histogram_suspicion(t.histogram)        as fake_rating_score,
         t.chart_count,
         case when t.updated_at is not null
              then (target_day - t.updated_at)::numeric end as days_since_update,
         r.rank_now,
         r.rank_then,
         e.relaunch_suspect,
         (f.developer is not null)               as clone_suspect,
         f.sibling_count
    from eligible e
    left join totals t on t.app_id = e.id
    left join vel    v on v.app_id = e.id
    left join cold   c on c.app_id = e.id
    left join ranks  r on r.app_id = e.id
    left join farms  f on f.developer = e.developer
),

components as (
  select r.*,
         -- >2 means accelerating. Null when there is not enough history to say, which is
         -- different from "not accelerating" and must not be scored as zero.
         case when r.v_prior is not null and r.vel_days >= 10
              then r.v7 / greatest(r.v_prior, r.eps) end                as momentum,
         -- Works on day one: cumulative total over app age needs no history of our own.
         case when r.v7 is not null and r.lifetime_rate is not null
              then r.v7 / greatest(r.lifetime_rate, r.eps) end          as vs_lifetime,
         case when r.rank_now is not null and r.rank_then is not null
                   then greatest(-5, least(5, (r.rank_then - r.rank_now) / 10.0))
              when r.rank_now is not null and r.rank_then is null
                   then 3.0   -- unranked -> ranked is a real move, not a missing value
              end                                                        as rank_velocity,
         -- Kills the "1 -> 4 per day is 300% growth" problem.
         -- Cast: log() returns double precision, and round(double, int) does not exist in
         -- Postgres. Keeping every component numeric avoids that trap at the end.
         case when r.v7 is not null
              then log((greatest(r.v7, 0) + 1)::numeric) end             as volume,
         -- The day-one signal. Cumulative total over app age needs no history of our own, so
         -- it is the only component available the moment an app is first enriched. Logged
         -- because install rates span five orders of magnitude and the raw value would make
         -- the z-score a one-app show.
         case when r.lifetime_rate is not null and r.lifetime_rate > 0
              then log((r.lifetime_rate + 1)::numeric) end               as log_lifetime,
         -- Breadth, where rank is depth. Charting at #40 across five categories is a wider
         -- phenomenon than #3 in a single niche, and the chart sweep already sees both.
         case when r.chart_count is not null
              then log((r.chart_count + 1)::numeric) end                 as breadth,
         -- Taper rather than a cliff: v1 zeroed at 90 days, so an app scored 4.2 one day and
         -- 0.0 the next. Half weight at the 120-day eligibility edge.
         greatest(0.1, 1 - r.age_days / 240.0)                           as recency,
         greatest(0.1,
                  1.0 - (case when r.clone_suspect then 0.4 else 0 end)
                      - (case when r.relaunch_suspect then 0.3 else 0 end)
                      -- NO histogram penalty. The obvious version of this — "almost all
                      -- 5-star with no mid-range must be purchased" — was tested against
                      -- real distributions and flags beloved mature apps: Genius Scan and a
                      -- 1.4M-rating pregnancy tracker both tripped it, while carrying tens
                      -- of thousands of genuine 1-stars. On Play, ~90% five-star is what
                      -- *quality* looks like, and the mid-band share shrinks with volume.
                      -- The shape alone cannot separate "loved" from "bought". A workable
                      -- version needs velocity — a rating burst inconsistent with install
                      -- rate or review arrival — which needs history we do not have yet.
                      -- fake_rating_score is still computed and persisted, unused, so the
                      -- idea can be revisited against real time series.
                      -- Abandonment. Two apps with the same curve are not equally
                      -- interesting when one has not shipped in two months.
                      - (case when r.days_since_update > 60 then 0.15 else 0 end))
                                                                          as trust
    from raw r
),

-- Winsorize before z-scoring: a single app with 400x its cohort's velocity would otherwise
-- own the mean and compress everything else toward zero.
bounds as (
  select store,
         percentile_cont(0.05) within group (order by momentum)      as mom_lo,
         percentile_cont(0.95) within group (order by momentum)      as mom_hi,
         percentile_cont(0.05) within group (order by vs_lifetime)   as vsl_lo,
         percentile_cont(0.95) within group (order by vs_lifetime)   as vsl_hi,
         percentile_cont(0.05) within group (order by volume)        as vol_lo,
         percentile_cont(0.95) within group (order by volume)        as vol_hi,
         percentile_cont(0.05) within group (order by breadth)       as brd_lo,
         percentile_cont(0.95) within group (order by breadth)       as brd_hi,
         percentile_cont(0.05) within group (order by log_lifetime)  as lif_lo,
         percentile_cont(0.95) within group (order by log_lifetime)  as lif_hi
    from components
   group by store
),

clipped as (
  -- percentile_cont returns double precision, so the clipped values come back double unless
  -- cast. Everything downstream stays numeric to keep round() usable.
  select c.*,
         least(greatest(c.momentum,    b.mom_lo), b.mom_hi)::numeric    as w_momentum,
         least(greatest(c.vs_lifetime, b.vsl_lo), b.vsl_hi)::numeric    as w_vs_lifetime,
         least(greatest(c.volume,      b.vol_lo), b.vol_hi)::numeric    as w_volume,
         least(greatest(c.log_lifetime, b.lif_lo), b.lif_hi)::numeric   as w_lifetime,
         least(greatest(c.breadth,      b.brd_lo), b.brd_hi)::numeric   as w_breadth
    from components c
    join bounds b on b.store = c.store
),

-- Population is (store, day): an app only competes with apps measured the same way.
moments as (
  select store,
         avg(w_momentum)     as mom_avg,  nullif(stddev_samp(w_momentum), 0)     as mom_sd,
         avg(w_vs_lifetime)  as vsl_avg,  nullif(stddev_samp(w_vs_lifetime), 0)  as vsl_sd,
         avg(rank_velocity)  as rnk_avg,  nullif(stddev_samp(rank_velocity), 0)  as rnk_sd,
         avg(w_volume)       as vol_avg,  nullif(stddev_samp(w_volume), 0)       as vol_sd,
         avg(w_lifetime)     as lif_avg,  nullif(stddev_samp(w_lifetime), 0)     as lif_sd,
         avg(w_breadth)      as brd_avg,  nullif(stddev_samp(w_breadth), 0)      as brd_sd
    from clipped
   group by store
),

scored as (
  select c.*,
         (c.w_momentum    - m.mom_avg) / m.mom_sd as z_momentum,
         (c.w_vs_lifetime - m.vsl_avg) / m.vsl_sd as z_vs_lifetime,
         (c.rank_velocity - m.rnk_avg) / m.rnk_sd as z_rank_velocity,
         (c.w_volume      - m.vol_avg) / m.vol_sd as z_volume,
         (c.w_lifetime    - m.lif_avg) / m.lif_sd as z_lifetime,
         (c.w_breadth     - m.brd_avg) / m.brd_sd as z_breadth
    from clipped c
    join moments m on m.store = c.store
),

weighted as (
  select s.*,
         -- Renormalize over the components that exist. A missing input must not read as a
         -- zero z-score, which is "exactly average" rather than "unknown".
         (coalesce(0.40 * s.z_momentum, 0)
        + coalesce(0.25 * s.z_vs_lifetime, 0)
        + coalesce(0.20 * s.z_rank_velocity, 0)
        + coalesce(0.15 * s.z_volume, 0)
        + coalesce(0.20 * s.z_lifetime, 0)
        + coalesce(0.10 * s.z_breadth, 0)) as weighted_sum,
         (case when s.z_momentum      is not null then 0.40 else 0 end
        + case when s.z_vs_lifetime   is not null then 0.25 else 0 end
        + case when s.z_rank_velocity is not null then 0.20 else 0 end
        + case when s.z_volume        is not null then 0.15 else 0 end
        + case when s.z_lifetime      is not null then 0.20 else 0 end
        + case when s.z_breadth       is not null then 0.10 else 0 end) as weight_total
    from scored s
)

insert into breakout_score (app_id, scored_on, score, components)
select w.app_id,
       target_day,
       round(coalesce(w.weighted_sum / nullif(w.weight_total, 0), 0) * w.recency * w.trust, 6),
       jsonb_strip_nulls(jsonb_build_object(
         'store',         w.store,
         'age_days',      round(w.age_days, 1),
         'cumulative',    w.cumulative,
         'v7',            round(w.v7, 4),
         'v_prior',       round(w.v_prior, 4),
         'v3',            round(w.v3, 4),
         'vel_days',      w.vel_days,
         'cold_start',    w.cold_start,
         'reviews_7d',    w.reviews_7d,
         'lifetime_rate', round(w.lifetime_rate, 4),
         'momentum',      round(w.momentum, 4),
         'vs_lifetime',   round(w.vs_lifetime, 4),
         'rank_now',      w.rank_now,
         'rank_then',     w.rank_then,
         'rank_velocity', round(w.rank_velocity, 4),
         'volume',        round(w.volume, 4),
         'z_momentum',      round(w.z_momentum, 4),
         'z_vs_lifetime',   round(w.z_vs_lifetime, 4),
         'z_rank_velocity', round(w.z_rank_velocity, 4),
         'z_volume',        round(w.z_volume, 4),
         'log_lifetime',    round(w.log_lifetime, 4),
         'z_lifetime',      round(w.z_lifetime, 4),
         'chart_count',     w.chart_count,
         'breadth',         round(w.breadth, 4),
         'z_breadth',       round(w.z_breadth, 4),
         'days_since_update', w.days_since_update,
         'fake_rating_score', w.fake_rating_score,
         'weight_total',  w.weight_total,
         'recency',       round(w.recency, 4),
         'trust',         round(w.trust, 4),
         'clone_suspect', w.clone_suspect,
         'sibling_count', w.sibling_count,
         'relaunch',      w.relaunch_suspect
       ))
  from weighted w
 where w.weight_total > 0   -- nothing known about this app today; skip rather than score 0
on conflict (app_id, scored_on) do update
   set score = excluded.score, components = excluded.components;

get diagnostics inserted = row_count;

-- Propagate the clone-farm verdict back onto the app so the dashboard can filter on it
-- without unpacking jsonb.
update app a
   set clone_suspect = true
  from (select developer from app
         where developer is not null and released > target_day - 14
         group by developer having count(*) >= 5) f
 where a.developer = f.developer and a.clone_suspect = false;

return inserted;
end;
$$;

comment on function compute_breakout_scores(date) is
  'Scores apps released within 120 days. Per-store z-scores over winsorized components; '
  'missing components renormalize rather than defaulting to zero. Every input is persisted '
  'in breakout_score.components so a ranking can be explained after the fact.';

-- Convenience view: today''s ranking, joined to the identity fields a dashboard needs.
create or replace view breakout_today as
  select bs.score,
         a.store, a.name, a.developer, a.category, a.released,
         (current_date - a.released) as age_days,
         a.discovered_via,
         a.clone_suspect,
         (bs.components->>'cold_start')::boolean as cold_start,
         (bs.components->>'v7')::numeric         as velocity_per_day,
         (bs.components->>'momentum')::numeric   as momentum,
         (bs.components->>'rank_now')::int       as chart_rank,
         bs.components,
         a.id as app_id
    from breakout_score bs
    join app a on a.id = bs.app_id
   where bs.scored_on = (select max(scored_on) from breakout_score)
   order by bs.score desc;

commit;
