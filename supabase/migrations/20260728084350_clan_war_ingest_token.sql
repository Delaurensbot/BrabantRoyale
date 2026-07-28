create schema if not exists private;

create or replace function private.has_valid_clan_history_ingest_token()
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
            ->> 'x-ingest-token',
          ''
        ),
        'UTF8'
      )
    ),
    'hex'
  ) = '4081cda4c915d344411da8a21ac324b77877f28fed623e01705a5f114bdf0c36';
$$;

revoke all on function private.has_valid_clan_history_ingest_token()
  from public;
grant usage on schema private to anon;
grant execute on function private.has_valid_clan_history_ingest_token()
  to anon;

grant insert, update on table public.clan_war_player_weeks to anon;
grant usage, select on sequence public.clan_war_player_weeks_id_seq to anon;

create policy "Scheduled history job can insert"
  on public.clan_war_player_weeks
  for insert
  to anon
  with check (
    (select private.has_valid_clan_history_ingest_token())
  );

create policy "Scheduled history job can update"
  on public.clan_war_player_weeks
  for update
  to anon
  using (
    (select private.has_valid_clan_history_ingest_token())
  )
  with check (
    (select private.has_valid_clan_history_ingest_token())
  );
