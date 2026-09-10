-- Casebook schema. Security model:
--   * The daily pipeline (GitHub Actions) writes with the SERVICE ROLE key,
--     which bypasses RLS. That key lives only in GitHub secrets.
--   * Students (anon) can read approved, still-open listings - nothing else.
--   * Admins sign in with email + password (Supabase Auth). Being signed in is
--     NOT enough: the email must also be in public.admins, checked by RLS on
--     every read and write. The admin page's secret link only hides the UI.

create table public.admins (
  email      text primary key check (email = lower(email)),
  added_at   timestamptz not null default now()
);

create table public.queue (
  id          bigint primary key,          -- Unstop listing id
  priority    int not null default 2,      -- sort only, never shown
  deadline    timestamptz,
  data        jsonb not null,              -- review card (title, org, context...)
  exported_at timestamptz not null default now()
);

create table public.archive_items (
  id          bigint primary key,
  first_seen  date not null,
  data        jsonb not null,              -- card + reason
  exported_at timestamptz not null default now()
);

create table public.decisions (
  listing_id  bigint primary key,
  decision    text not null check (decision in ('approve', 'reject', 'skip')),
  reason      text,
  decided_at  timestamptz not null default now(),
  decided_by  uuid default auth.uid(),
  source      text check (source in ('queue', 'archive')),
  updated     boolean not null default false,   -- re-approved after a change
  digested_at timestamptz,
  deadline    timestamptz,
  card        jsonb not null                    -- what students see
);

create table public.sync_meta (
  id          int primary key default 1 check (id = 1),
  data        jsonb not null,
  updated_at  timestamptz not null default now()
);

-- Admin check. SECURITY DEFINER so it can read public.admins, which no
-- client role can read directly.
create function public.is_admin() returns boolean
language sql stable security definer set search_path = ''
as $$
  select exists (
    select 1 from public.admins
    where email = lower(coalesce(auth.jwt() ->> 'email', ''))
  );
$$;
revoke all on function public.is_admin() from public;
grant execute on function public.is_admin() to anon, authenticated;

alter table public.admins        enable row level security;  -- no policies: invisible to clients
alter table public.queue         enable row level security;
alter table public.archive_items enable row level security;
alter table public.decisions     enable row level security;
alter table public.sync_meta     enable row level security;

create policy "admins read queue"   on public.queue         for select to authenticated using ((select public.is_admin()));
create policy "admins read archive" on public.archive_items for select to authenticated using ((select public.is_admin()));
create policy "admins read sync"    on public.sync_meta     for select to authenticated using ((select public.is_admin()));

create policy "public reads open approvals" on public.decisions for select to anon, authenticated
  using (decision = 'approve' and (deadline is null or deadline > now()));
create policy "admins read all decisions"  on public.decisions for select to authenticated using ((select public.is_admin()));
create policy "admins insert decisions"    on public.decisions for insert to authenticated with check ((select public.is_admin()));
create policy "admins update decisions"    on public.decisions for update to authenticated
  using ((select public.is_admin())) with check ((select public.is_admin()));
create policy "admins delete decisions"    on public.decisions for delete to authenticated using ((select public.is_admin()));

-- Students never see who decided, why something was rejected, or skip state.
revoke select on public.decisions from anon;
grant select (listing_id, decision, decided_at, updated, deadline, card) on public.decisions to anon;

insert into public.admins (email) values ('aganyabajaj2727@gmail.com');
