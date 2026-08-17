-- Signals that were already being fetched and thrown away.
--
-- No new requests: the Play detail response has carried the rating histogram and last-update
-- timestamp all along, Apple's lookup carries currentVersionReleaseDate, and the chart sweep
-- already sees every position before collapsing them to a single best rank.

begin;

alter table snapshot add column if not exists histogram   int[];
alter table snapshot add column if not exists chart_count int;
alter table snapshot add column if not exists updated_at  date;

comment on column snapshot.histogram is
  'Play rating distribution as [1-star, 2, 3, 4, 5]. Free in every detail response. Shape is '
  'a fake-review signal: organic apps show a J-curve with a real tail of low ratings.';
comment on column snapshot.chart_count is
  'How many distinct charts the app appeared in. Breadth, where best_rank is depth — #40 in '
  'five categories is a wider phenomenon than #3 in one niche.';
comment on column snapshot.updated_at is
  'When the developer last shipped an update (Play: updated; Apple: currentVersionReleaseDate). '
  'Distinguishes an app being actively invested in from an abandoned one with the same curve.';

-- ---------------------------------------------------------------------------
-- Rating-distribution suspicion
-- ---------------------------------------------------------------------------
--
-- Real apps collect complaints. A distribution that is almost entirely 5-star with virtually
-- no 2s or 3s is the signature of purchased ratings — genuine enthusiasm still produces a
-- tail. Requires volume before judging: 12 ratings that happen to all be 5-star is a normal
-- launch week, not fraud.

create or replace function histogram_suspicion(h int[])
returns numeric
language sql
immutable
as $$
  with t as (
    select coalesce(h[1],0) + coalesce(h[2],0) + coalesce(h[3],0)
         + coalesce(h[4],0) + coalesce(h[5],0) as total,
           coalesce(h[5],0) as five,
           coalesce(h[2],0) + coalesce(h[3],0) as middle
  )
  select case
           when h is null or (select total from t) < 50 then 0::numeric
           when (select five::numeric / total from t) > 0.90
            and (select middle::numeric / total from t) < 0.02 then 1.0
           when (select five::numeric / total from t) > 0.85
            and (select middle::numeric / total from t) < 0.05 then 0.5
           else 0::numeric
         end;
$$;

comment on function histogram_suspicion(int[]) is
  '0 = normal, 0.5 = odd, 1.0 = implausibly clean. Returns 0 below 50 ratings, where the '
  'distribution carries no information yet.';

commit;
