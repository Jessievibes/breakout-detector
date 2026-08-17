-- Row Level Security — deny by default.
--
-- Every table gets RLS enabled and *no* policies for anon/authenticated. In Postgres,
-- RLS with zero policies denies all access, which is exactly what we want: the anon key
-- can reach nothing even if it leaks. Jobs and the dashboard's server components connect
-- as the service role / table owner, which bypasses RLS.
--
-- The failure mode this prevents: shipping a NEXT_PUBLIC_ key that can read the whole
-- breakout table. Never expose the service key to the browser.

begin;

alter table app             enable row level security;
alter table snapshot        enable row level security;
alter table review_event    enable row level security;
alter table snapshot_weekly enable row level security;
alter table breakout_score  enable row level security;
alter table run_log         enable row level security;

-- Also force RLS for the table owner, so a mistaken owner-context query from a web
-- process cannot quietly bypass the policy set.
alter table app             force row level security;
alter table snapshot        force row level security;
alter table review_event    force row level security;
alter table snapshot_weekly force row level security;
alter table breakout_score  force row level security;
alter table run_log         force row level security;

-- Belt and braces: revoke the blanket grants Supabase hands the API roles.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    execute 'revoke all on all tables in schema public from anon';
    execute 'revoke all on all sequences in schema public from anon';
  end if;
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    execute 'revoke all on all tables in schema public from authenticated';
    execute 'revoke all on all sequences in schema public from authenticated';
  end if;
end $$;

commit;
