-- Run once in the SQL Editor of YOUR Supabase project. No secrets in this file.
begin;
create table if not exists public.mask_alerts (
  id uuid primary key,
  occurred_at timestamptz not null,
  session_id uuid not null,
  violation_labels text[] not null,
  counts jsonb not null default '{}'::jsonb,
  detections jsonb not null default '[]'::jsonb,
  model_sha256 text not null,
  inference_ms double precision not null,
  cloud_pipeline_ms double precision not null,
  image_path text,
  storage_status text not null default 'pending' check (storage_status in ('pending','saved','failed')),
  telegram_status text not null default 'pending' check (telegram_status in ('pending','sent','failed','unknown')),
  telegram_message_id bigint,
  telegram_ack_ms double precision,
  errors jsonb not null default '[]'::jsonb,
  completed_at timestamptz
);
create index if not exists mask_alerts_occurred_at_idx on public.mask_alerts (occurred_at desc);
alter table public.mask_alerts enable row level security;
revoke all on table public.mask_alerts from anon, authenticated;
grant select, insert, update, delete on table public.mask_alerts to service_role;

-- Private bucket. Backend secret key bypasses RLS; no public read/write policies.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('mask-alerts', 'mask-alerts', false, 2097152, array['image/jpeg'])
on conflict (id) do update set public = false,
  file_size_limit = excluded.file_size_limit, allowed_mime_types = excluded.allowed_mime_types;
commit;
