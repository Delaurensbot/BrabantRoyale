-- T05: server-side intra-day storage for duel-first monitoring.
--
-- The project currently uses Supabase's public schema and the standard
-- anon/authenticated/service_role roles.  Raw monitoring rows are deliberately
-- server-only.  Public clients receive only the aggregate view below, because
-- table-level RLS cannot hide sensitive player columns from a broad SELECT.

create table public.river_race_live_snapshots (
  id bigint generated always as identity primary key,
  clan_tag text not null check (btrim(clan_tag) <> ''),
  season_id bigint not null check (season_id >= 0),
  section_index integer check (
    section_index is null
    or section_index >= 0
  ),
  period_index integer not null check (period_index >= 0),
  period_type text not null default 'unknown' check (
    btrim(period_type) <> ''
    and char_length(period_type) <= 32
  ),
  race_created_at timestamptz not null,
  player_tag text not null check (btrim(player_tag) <> ''),
  player_name text not null check (btrim(player_name) <> ''),
  player_role text not null default '' check (char_length(player_role) <= 32),
  decks_used smallint check (
    decks_used is null
    or decks_used between 0 and 16
  ),
  decks_used_today smallint check (
    decks_used_today is null
    or decks_used_today >= 0
  ),
  fame integer check (fame is null or fame >= 0),
  repair_points integer check (repair_points is null or repair_points >= 0),
  boat_attacks smallint check (
    boat_attacks is null
    or boat_attacks >= 0
  ),
  boat_attacks_today smallint check (
    boat_attacks_today is null
    or boat_attacks_today >= 0
  ),
  boat_defenses smallint check (
    boat_defenses is null
    or boat_defenses >= 0
  ),
  boat_defenses_today smallint check (
    boat_defenses_today is null
    or boat_defenses_today >= 0
  ),
  captured_at timestamptz not null default now(),
  source text not null default 'unknown' check (
    btrim(source) <> ''
    and char_length(source) <= 64
  ),
  payload_version integer not null default 1 check (payload_version >= 1),
  -- The exact capture time is retained; this generated UTC bucket is the
  -- idempotency grain for one player observation every ten minutes.
  capture_bucket timestamptz generated always as (
    date_bin(
      interval '10 minutes',
      captured_at,
      timestamptz '1970-01-01 00:00:00+00'
    )
  ) stored,
  constraint river_race_live_snapshots_idempotency_key
    unique (
      clan_tag,
      race_created_at,
      period_index,
      player_tag,
      capture_bucket
    )
);

create index river_race_live_snapshots_clan_race_capture_idx
  on public.river_race_live_snapshots (
    clan_tag,
    race_created_at desc,
    period_index,
    player_tag,
    capture_bucket desc
  );

create index river_race_live_snapshots_player_history_idx
  on public.river_race_live_snapshots (
    player_tag,
    race_created_at desc,
    captured_at desc
  );

create table public.war_player_day_events (
  id bigint generated always as identity primary key,
  clan_tag text not null check (btrim(clan_tag) <> ''),
  race_created_at timestamptz not null,
  period_index integer not null check (period_index >= 0),
  player_tag text not null check (btrim(player_tag) <> ''),
  event_type text not null check (
    btrim(event_type) <> ''
    and char_length(event_type) <= 64
  ),
  observed_decks_used_today smallint check (
    observed_decks_used_today is null
    or observed_decks_used_today >= 0
  ),
  confidence text not null default 'unknown' check (
    confidence in ('unknown', 'low', 'medium', 'high')
  ),
  observed_at timestamptz not null default now(),
  details jsonb not null default '{}'::jsonb check (
    jsonb_typeof(details) = 'object'
  ),
  created_at timestamptz not null default now(),
  constraint war_player_day_events_idempotency_key
    unique (
      clan_tag,
      race_created_at,
      period_index,
      player_tag,
      event_type
    )
);

create index war_player_day_events_clan_race_observed_idx
  on public.war_player_day_events (
    clan_tag,
    race_created_at desc,
    period_index,
    player_tag,
    observed_at desc
  );

create index war_player_day_events_player_observed_idx
  on public.war_player_day_events (player_tag, observed_at desc);

create table public.notification_log (
  id bigint generated always as identity primary key,
  event_key text not null check (
    btrim(event_key) <> ''
    and char_length(event_key) <= 256
  ),
  channel text not null check (
    btrim(channel) <> ''
    and char_length(channel) <= 64
  ),
  status text not null default 'pending' check (
    btrim(status) <> ''
    and char_length(status) <= 32
  ),
  response_code integer check (
    response_code is null
    or response_code between 100 and 599
  ),
  sent_at timestamptz,
  details jsonb not null default '{}'::jsonb check (
    jsonb_typeof(details) = 'object'
  ),
  constraint notification_log_idempotency_key
    unique (event_key, channel)
);

create index notification_log_channel_sent_idx
  on public.notification_log (channel, sent_at desc);

create index notification_log_status_sent_idx
  on public.notification_log (status, sent_at desc);

-- RLS is enabled on every raw table.  No anon/authenticated table privileges
-- or policies are created; the only direct table principal is service_role.
-- This keeps player-level monitoring, event details, and delivery details out
-- of the public API without introducing a new auth system.
alter table public.river_race_live_snapshots enable row level security;
alter table public.war_player_day_events enable row level security;
alter table public.notification_log enable row level security;

revoke all on table public.river_race_live_snapshots from public;
revoke all on table public.war_player_day_events from public;
revoke all on table public.notification_log from public;

grant select, insert, update, delete
  on table public.river_race_live_snapshots
  to service_role;
grant select, insert, update, delete
  on table public.war_player_day_events
  to service_role;
grant select, insert, update, delete
  on table public.notification_log
  to service_role;

revoke all on sequence public.river_race_live_snapshots_id_seq from public;
revoke all on sequence public.war_player_day_events_id_seq from public;
revoke all on sequence public.notification_log_id_seq from public;

grant usage, select
  on sequence public.river_race_live_snapshots_id_seq
  to service_role;
grant usage, select
  on sequence public.war_player_day_events_id_seq
  to service_role;
grant usage, select
  on sequence public.notification_log_id_seq
  to service_role;

-- Explicit service-role policies document the intended server-side write path.
-- Supabase's service_role normally bypasses RLS, but the policies keep this
-- boundary correct if that role is used through an RLS-aware server path.
create policy "Live snapshots are server managed"
  on public.river_race_live_snapshots
  for all
  to service_role
  using (true)
  with check (true);

create policy "War player day events are server managed"
  on public.war_player_day_events
  for all
  to service_role
  using (true)
  with check (true);

create policy "Notification log is server managed"
  on public.notification_log
  for all
  to service_role
  using (true)
  with check (true);

-- Public clients may consume only an aggregate, non-player summary.  The
-- view intentionally omits player tags/names/roles, event details, confidence,
-- and notification delivery data.  NULL metric totals mean that no value was
-- observed; they are not coerced to zero.
create view public.river_race_live_snapshot_summary
with (security_barrier = true)
as
select
  clan_tag,
  season_id,
  section_index,
  period_index,
  period_type,
  race_created_at,
  capture_bucket,
  max(captured_at) as captured_at,
  count(*)::integer as player_count,
  count(decks_used_today)::integer as players_with_known_decks_today,
  sum(fame) as total_fame,
  sum(repair_points) as total_repair_points,
  sum(decks_used_today) as total_decks_used_today,
  sum(boat_attacks_today) as total_boat_attacks_today,
  sum(boat_defenses_today) as total_boat_defenses_today
from public.river_race_live_snapshots
group by
  clan_tag,
  season_id,
  section_index,
  period_index,
  period_type,
  race_created_at,
  capture_bucket;

revoke all on public.river_race_live_snapshot_summary from public;
grant select on public.river_race_live_snapshot_summary to anon, authenticated;

comment on table public.river_race_live_snapshots is
  'Server-only per-player intra-day race observations; use the public aggregate view for summaries.';
comment on column public.river_race_live_snapshots.capture_bucket is
  'Generated UTC ten-minute bucket derived from captured_at; part of the live snapshot idempotency key.';
comment on table public.war_player_day_events is
  'Server-only observed player-day events; details and confidence are not public data.';
comment on table public.notification_log is
  'Server-only notification delivery audit; event details and responses are not public data.';
comment on view public.river_race_live_snapshot_summary is
  'Public aggregate race summary without player identity, roles, event details, or notification data.';
