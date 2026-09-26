-- XMP-stack rebuild: relational schema, in its own xmp_-prefixed tables so
-- it never collides with the existing `accounts` (jsonb) table this same
-- Supabase project already serves to the live ProfileWatch deployment.
--
-- Each subscriber's auth user is linked to exactly one account via
-- xmp_account_owners; RLS means a signed-in user can only ever read their
-- own account's rows — "only their profile shows" is enforced by Postgres
-- itself, not by frontend logic.

create table if not exists xmp_accounts (
  id text primary key,
  business_name text not null,
  category text,
  account_type text,
  source_url text
);

create table if not exists xmp_reviews (
  account_id text not null references xmp_accounts(id) on delete cascade,
  id text not null,
  rating int not null,
  text text not null,
  date date not null,
  responded boolean not null default false,
  response_date date,
  response_text text,
  primary key (account_id, id)
);

create table if not exists xmp_listings (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references xmp_accounts(id) on delete cascade,
  directory text not null,
  name text not null,
  address text not null,
  phone text not null
);

create table if not exists xmp_weekly_health_history (
  account_id text not null references xmp_accounts(id) on delete cascade,
  week_ending date not null,
  score int not null,
  formula_version int not null,
  primary key (account_id, week_ending)
);

-- Which subscriber (auth.users) owns which account.
create table if not exists xmp_account_owners (
  account_id text not null references xmp_accounts(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  primary key (account_id, user_id)
);

alter table xmp_accounts enable row level security;
alter table xmp_reviews enable row level security;
alter table xmp_listings enable row level security;
alter table xmp_weekly_health_history enable row level security;
alter table xmp_account_owners enable row level security;

-- A signed-in subscriber sees only the account(s) they own.
create policy "own account" on xmp_accounts for select
  using (exists (select 1 from xmp_account_owners ao where ao.account_id = xmp_accounts.id and ao.user_id = auth.uid()));

create policy "own reviews" on xmp_reviews for select
  using (exists (select 1 from xmp_account_owners ao where ao.account_id = xmp_reviews.account_id and ao.user_id = auth.uid()));

create policy "own listings" on xmp_listings for select
  using (exists (select 1 from xmp_account_owners ao where ao.account_id = xmp_listings.account_id and ao.user_id = auth.uid()));

create policy "own history" on xmp_weekly_health_history for select
  using (exists (select 1 from xmp_account_owners ao where ao.account_id = xmp_weekly_health_history.account_id and ao.user_id = auth.uid()));

create policy "own ownership row" on xmp_account_owners for select
  using (user_id = auth.uid());

-- No insert/update/delete policies for any of these: subscribers are
-- read-only. Writes (seeding, and any future admin action) go through the
-- service_role key, which bypasses RLS entirely.
