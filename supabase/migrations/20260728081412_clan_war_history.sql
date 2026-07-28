create table public.clan_war_player_weeks (
  id bigint generated always as identity primary key,
  clan_tag text not null check (clan_tag <> ''),
  clan_name text not null,
  season_id bigint not null check (season_id >= 0),
  race_created_at timestamptz not null,
  player_tag text not null check (player_tag <> ''),
  player_name text not null,
  player_role text not null default '',
  fame integer not null default 0 check (fame >= 0),
  repair_points integer not null default 0 check (repair_points >= 0),
  contribution integer not null default 0 check (
    contribution >= 0
    and contribution = fame + repair_points
  ),
  decks_used smallint not null default 0 check (decks_used between 0 and 16),
  captured_at timestamptz not null default now(),
  constraint clan_war_player_weeks_snapshot_player_key
    unique (clan_tag, race_created_at, player_tag)
);

create index clan_war_player_weeks_player_history_idx
  on public.clan_war_player_weeks (player_tag, race_created_at desc);

alter table public.clan_war_player_weeks enable row level security;

grant select on table public.clan_war_player_weeks to anon, authenticated;
grant select, insert, update on table public.clan_war_player_weeks to service_role;
grant usage, select on sequence public.clan_war_player_weeks_id_seq to service_role;

revoke insert, update, delete on table public.clan_war_player_weeks
  from anon, authenticated;

create policy "Clan war history is publicly readable"
  on public.clan_war_player_weeks
  for select
  to anon, authenticated
  using (true);
