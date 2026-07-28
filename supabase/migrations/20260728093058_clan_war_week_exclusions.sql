create table public.clan_war_week_exclusions (
  id bigint generated always as identity primary key,
  clan_tag text not null check (clan_tag <> ''),
  race_created_at timestamptz not null,
  player_tag text not null check (player_tag <> ''),
  reason text not null default '' check (char_length(reason) <= 240),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint clan_war_week_exclusions_snapshot_player_key
    unique (clan_tag, race_created_at, player_tag),
  constraint clan_war_week_exclusions_history_fkey
    foreign key (clan_tag, race_created_at, player_tag)
    references public.clan_war_player_weeks (
      clan_tag,
      race_created_at,
      player_tag
    )
    on delete cascade
);

alter table public.clan_war_week_exclusions enable row level security;

grant select on table public.clan_war_week_exclusions
  to anon, authenticated;
grant select, insert, update, delete
  on table public.clan_war_week_exclusions
  to service_role;
grant insert, update, delete on table public.clan_war_week_exclusions
  to anon;
grant usage, select on sequence public.clan_war_week_exclusions_id_seq
  to anon, service_role;

create or replace function private.has_valid_analytics_admin_key()
returns boolean
language sql
stable
security invoker
set search_path = ''
as $$
  select encode(
    pg_catalog.sha256(
      pg_catalog.convert_to(
        coalesce(
          pg_catalog.current_setting('request.headers', true)::json
            ->> 'x-analytics-admin-key',
          ''
        ),
        'UTF8'
      )
    ),
    'hex'
  ) = '2d3ef0fb32963a071ec32055dcfa6502132d5e4670fb979dbbe9c45456fa54a1';
$$;

revoke all on function private.has_valid_analytics_admin_key()
  from public;
grant usage on schema private to anon;
grant execute on function private.has_valid_analytics_admin_key()
  to anon;

create or replace function private.set_clan_war_week_exclusion_updated_at()
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

revoke all
  on function private.set_clan_war_week_exclusion_updated_at()
  from public;

create trigger set_clan_war_week_exclusion_updated_at
before update on public.clan_war_week_exclusions
for each row
execute function private.set_clan_war_week_exclusion_updated_at();

create policy "Clan war week exclusions are publicly readable"
  on public.clan_war_week_exclusions
  for select
  to anon, authenticated
  using (true);

create policy "Analytics admins can insert week exclusions"
  on public.clan_war_week_exclusions
  for insert
  to anon
  with check (
    (select private.has_valid_analytics_admin_key())
  );

create policy "Analytics admins can update week exclusions"
  on public.clan_war_week_exclusions
  for update
  to anon
  using (
    (select private.has_valid_analytics_admin_key())
  )
  with check (
    (select private.has_valid_analytics_admin_key())
  );

create policy "Analytics admins can delete week exclusions"
  on public.clan_war_week_exclusions
  for delete
  to anon
  using (
    (select private.has_valid_analytics_admin_key())
  );

