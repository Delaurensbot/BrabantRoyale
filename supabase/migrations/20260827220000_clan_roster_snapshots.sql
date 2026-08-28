-- T14: durable server-side roster observations for join/leave history.
--
-- A row represents one player observed in one complete roster capture.  The
-- API does not provide an exact join/leave event; consumers must compare
-- captured_at values as observation intervals.

create table public.clan_roster_snapshots (
  id bigint generated always as identity primary key,
  clan_tag text not null check (
    clan_tag ~ '^[A-Z0-9]{1,32}$'
  ),
  player_tag text not null check (
    player_tag ~ '^[A-Z0-9]{1,32}$'
  ),
  player_name text not null default 'unknown' check (
    btrim(player_name) <> ''
    and char_length(player_name) <= 120
  ),
  role text not null default 'unknown' check (
    btrim(role) <> ''
    and char_length(role) <= 32
  ),
  trophies integer check (
    trophies is null or trophies >= 0
  ),
  seen_at timestamptz not null,
  captured_at timestamptz not null default now(),
  constraint clan_roster_snapshots_idempotency_key
    unique (clan_tag, player_tag, captured_at)
);

create index clan_roster_snapshots_clan_capture_idx
  on public.clan_roster_snapshots (clan_tag, captured_at desc, player_tag);

create index clan_roster_snapshots_player_history_idx
  on public.clan_roster_snapshots (clan_tag, player_tag, captured_at desc);

-- Roster rows are read and written by the server-side route only.  In
-- particular, do not grant anon/authenticated direct access to player rows.
alter table public.clan_roster_snapshots enable row level security;

revoke all on table public.clan_roster_snapshots from public;
grant select, insert, update, delete
  on table public.clan_roster_snapshots
  to service_role;

revoke all on sequence public.clan_roster_snapshots_id_seq from public;
grant usage, select
  on sequence public.clan_roster_snapshots_id_seq
  to service_role;

create policy "Roster snapshots are server managed"
  on public.clan_roster_snapshots
  for all
  to service_role
  using (true)
  with check (true);

comment on table public.clan_roster_snapshots is
  'Server-only normalized roster observations; join/leave times are intervals between snapshots, not exact game events.';
comment on column public.clan_roster_snapshots.captured_at is
  'UTC capture identity used to make retries idempotent for one roster snapshot.';
comment on column public.clan_roster_snapshots.seen_at is
  'UTC time at which this player was observed in the roster response.';
