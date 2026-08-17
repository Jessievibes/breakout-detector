-- Record the install band an app showed at *discovery* time, before enrichment.
--
-- Why this earns a column: discovery finds far more apps than the daily enrich budget can
-- cover (first live run: 2,084 discovered, 400/day enriched). So the order of the enrich
-- queue decides whether this system finds young apps or established ones — and until an app
-- is enriched, its install band is the only youth signal available.
--
-- Measured on the first live batch (search-discovered, n=80):
--
--     install band    apps  % under 120d   median age
--     <100              27       93%           19d
--     100-1k            28       43%          152d
--     1k-10k            25       20%          284d
--
-- Under 100 installs is almost perfectly predictive of a brand-new app. Phase 0 tested this
-- idea with a 100,000 threshold and concluded it was "no better than random" — the threshold
-- was three orders of magnitude too coarse and washed the signal out entirely.

begin;

alter table app add column if not exists discovery_installs bigint;

comment on column app.discovery_installs is
  'Install band floor when first discovered (search results carry it free). Lower = younger; '
  'under 100 is ~93% likely to be an app under 120 days old. Null for channels that do not '
  'expose it (charts, developer pages).';

-- The enrich queue's ordering index: unenriched apps, smallest discovery band first.
create index if not exists app_enrich_priority_idx
  on app (store, discovery_installs nulls last, id)
  where delisted = false and last_enriched is null;

commit;
