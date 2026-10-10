-- Run once in Supabase SQL Editor. Idempotent, no existing data is deleted.
create table if not exists cxz_users (
 id bigint primary key, name text not null default '', username text not null default '',
 created_at timestamptz not null default now(), last_seen timestamptz not null default now(),
 activated_at timestamptz, blocked boolean not null default false,
 state jsonb not null default '{}', key_failures int not null default 0,
 key_lock_until timestamptz
);
create table if not exists cxz_keys (
 id bigserial primary key, digest text unique not null, label text not null,
 created_at timestamptz not null default now(), expires_at timestamptz,
 revoked boolean not null default false, used_by bigint references cxz_users(id), used_at timestamptz
);
create table if not exists cxz_jobs (
 id bigserial primary key, user_id bigint not null references cxz_users(id), request_id bigint unique not null,
 niche text not null, region text not null, target int not null check(target in (5,10)),
 status text not null default 'queued', created_at timestamptz not null default now(),
 updated_at timestamptz not null default now(), lease_until timestamptz,
 checked int not null default 0, rejected int not null default 0, uncertain int not null default 0,
 candidates jsonb, cursor int not null default 0, detail text not null default '',
 notified boolean not null default false, cancelled boolean not null default false
);
create unique index if not exists cxz_one_active_job on cxz_jobs(user_id) where status in ('queued','running');
create table if not exists cxz_results (
 id bigserial primary key, user_id bigint not null references cxz_users(id),
 job_id bigint not null references cxz_jobs(id), org_id text not null, name text not null,
 address text not null, phone text not null default '', url text not null,
 checked_at timestamptz not null default now(), favorite boolean not null default false,
 note text not null default '', stage text not null default 'Не связывался',
 evidence jsonb not null default '{}', unique(user_id,org_id)
);
create index if not exists cxz_results_user_day on cxz_results(user_id,checked_at);
create table if not exists cxz_settings (name text primary key,value text not null);
create table if not exists cxz_inbox (
 id bigint primary key, payload jsonb, status text not null default 'queued',
 attempts int not null default 0, lease_until timestamptz, created_at timestamptz not null default now()
);
-- The bot uses a server-side PostgreSQL connection, never a browser anon key.
alter table cxz_users enable row level security;
alter table cxz_keys enable row level security;
alter table cxz_jobs enable row level security;
alter table cxz_results enable row level security;
alter table cxz_settings enable row level security;
alter table cxz_inbox enable row level security;
revoke all on cxz_users,cxz_keys,cxz_jobs,cxz_results,cxz_settings,cxz_inbox from anon,authenticated;
revoke all on sequence cxz_keys_id_seq,cxz_jobs_id_seq,cxz_results_id_seq from anon,authenticated;
