-- T13: configurable clan policy and an admin-only leader decision audit log.
--
-- Policy values are safe to read as a public, non-personal configuration
-- model.  Leader decisions are sensitive human context and are kept behind
-- the existing private.has_valid_analytics_admin_key() RLS boundary.

create table public.clan_policy_settings (
  clan_tag text primary key check (
    clan_tag ~ '^[A-Z0-9]{1,32}$'
  ),
  duel_first_enabled boolean not null default false,
  duel_first_alert_after_utc text not null default '12:00:00Z' check (
    duel_first_alert_after_utc ~ '^([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]Z$'
  ),
  promotion_window_weeks smallint not null default 6 check (
    promotion_window_weeks in (2, 4, 6)
  ),
  promotion_min_average integer not null default 2500 check (
    promotion_min_average between 0 and 10000
  ),
  promotion_min_reliability numeric(5,2) not null default 95.00 check (
    promotion_min_reliability between 0 and 100
  ),
  promotion_min_observed_races smallint not null default 2 check (
    promotion_min_observed_races between 1 and 52
  ),
  demotion_window_weeks smallint not null default 10 check (
    demotion_window_weeks between 1 and 52
  ),
  demotion_max_missed_attacks smallint not null default 2 check (
    demotion_max_missed_attacks between 0 and 832
  ),
  trial_races_required smallint not null default 2 check (
    trial_races_required between 1 and 52
  ),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

comment on table public.clan_policy_settings is
  'Validated, non-personal policy configuration for one Brabant Royale clan.';
comment on column public.clan_policy_settings.duel_first_alert_after_utc is
  'UTC time of day in HH:MM:SSZ form; the policy switch controls whether it is used.';

alter table public.clan_policy_settings enable row level security;

revoke all on table public.clan_policy_settings from public;
grant select on table public.clan_policy_settings to anon, authenticated;
grant insert, update on table public.clan_policy_settings to anon;
grant select, insert, update on table public.clan_policy_settings to service_role;

create or replace function private.set_clan_policy_settings_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.updated_at = pg_catalog.now();
  return new;
end;
$$;

revoke all on function private.set_clan_policy_settings_updated_at()
  from public;

create trigger set_clan_policy_settings_updated_at
before update on public.clan_policy_settings
for each row
execute function private.set_clan_policy_settings_updated_at();

create policy "Clan policy settings are publicly readable"
  on public.clan_policy_settings
  for select
  to anon, authenticated
  using (true);

create policy "Analytics admins can insert clan policy settings"
  on public.clan_policy_settings
  for insert
  to anon
  with check (
    (select private.has_valid_analytics_admin_key())
  );

create policy "Analytics admins can update clan policy settings"
  on public.clan_policy_settings
  for update
  to anon
  using (
    (select private.has_valid_analytics_admin_key())
  )
  with check (
    (select private.has_valid_analytics_admin_key())
  );

-- Seed every currently configured clan so the defaults are explicit in the
-- database as well as in the server read layer.  Future configured clans use
-- the same column defaults when their first policy is written.
insert into public.clan_policy_settings (clan_tag)
values ('9YP8UY'), ('GPCLVLPP'), ('RLQQQC99')
on conflict (clan_tag) do nothing;

create table public.leader_decisions (
  id bigint generated always as identity primary key,
  clan_tag text not null check (
    clan_tag ~ '^[A-Z0-9]{1,32}$'
  ),
  player_tag text check (
    player_tag is null or player_tag ~ '^[A-Z0-9]{1,32}$'
  ),
  actor text not null check (
    char_length(btrim(actor)) between 1 and 120
  ),
  decision_type text not null check (
    decision_type in (
      'promotion',
      'demotion',
      'exemption',
      'strategic_experiment',
      'main_clan',
      'BR2',
      'BR3',
      'reject',
      'manual_correction'
    )
  ),
  reason text not null check (
    char_length(btrim(reason)) between 1 and 240
  ),
  related_race_key text check (
    related_race_key is null or char_length(related_race_key) <= 256
  ),
  idempotency_key text not null check (
    idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
  ),
  created_at timestamptz not null default now(),
  constraint leader_decisions_clan_idempotency_key
    unique (clan_tag, idempotency_key)
);

create index leader_decisions_clan_created_idx
  on public.leader_decisions (clan_tag, created_at desc, id desc);

comment on table public.leader_decisions is
  'Admin-only append-only audit log for human clan decisions; never expose through public read models.';
comment on column public.leader_decisions.idempotency_key is
  'Client retry key scoped to clan_tag; duplicate retries do not create another audit row.';

alter table public.leader_decisions enable row level security;

revoke all on table public.leader_decisions from public;
grant select, insert on table public.leader_decisions to anon;
grant select, insert, update, delete on table public.leader_decisions to service_role;
revoke all on sequence public.leader_decisions_id_seq from public;
grant usage, select on sequence public.leader_decisions_id_seq to anon;
grant usage, select on sequence public.leader_decisions_id_seq to service_role;

create policy "Analytics admins can read leader decisions"
  on public.leader_decisions
  for select
  to anon
  using (
    (select private.has_valid_analytics_admin_key())
  );

create policy "Analytics admins can insert leader decisions"
  on public.leader_decisions
  for insert
  to anon
  with check (
    (select private.has_valid_analytics_admin_key())
  );
