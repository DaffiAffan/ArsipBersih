"""SQLite state database (schema v1)."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from .layout import STATE_DIR, long_path

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 30_000

SCHEMA = """
create table if not exists units (
    id text primary key,
    primary_file_id text,
    pair_kind text not null default 'single' check (pair_kind in ('single','live_photo','raw_jpg')),
    taken_at text,
    date_source text check (date_source is null or date_source in ('exif','quicktime','filename')),
    event_key text,
    event_name text,
    event_subpath text,
    location text not null default 'source' check (location in ('source','archive','review','trash')),
    primary_category text,
    planned_archive_dir text,
    planned_review_dir text,
    seen integer not null default 0
);

create table if not exists files (
    id text primary key,
    unit_id text references units(id),
    rel_path text not null unique,
    orig_rel_path text,
    size integer,
    mtime_ns integer,
    kind text,
    ext text,
    source_label text,
    sha256 text,
    hash_status text check (hash_status is null or hash_status in ('done','error')),
    meta_status text check (meta_status is null or meta_status in ('done','error')),
    visual_status text check (visual_status is null or visual_status in ('done','error')),
    error text,
    missing integer not null default 0,
    purged integer not null default 0
);
create index if not exists idx_files_unit on files(unit_id);
create index if not exists idx_files_size on files(size);

create table if not exists media_info (
    file_id text primary key references files(id),
    make text,
    model text,
    width integer,
    height integer,
    duration_s real,
    video_codec text,
    has_audio integer,
    has_gps integer,
    exif_datetime text,
    qt_datetime text,
    phash text,
    frame_phashes text,
    sharpness real,
    luma_mean real,
    luma_std real,
    clipping real,
    frame_lumas text,
    decode_ok integer
);

create table if not exists flags (
    id text primary key,
    unit_id text not null references units(id),
    category text not null,
    score real,
    reason text,
    unique (unit_id, category)
);

create table if not exists groups (
    id text primary key,
    kind text not null,
    suggested_keep_unit_id text
);

create table if not exists group_members (
    group_id text not null references groups(id),
    unit_id text not null references units(id),
    primary key (group_id, unit_id)
);

create table if not exists decisions (
    unit_id text primary key references units(id),
    decision text not null check (decision in ('keep','discard')),
    decided_at text,
    applied integer not null default 0
);

create table if not exists batches (
    id text primary key,
    kind text not null check (kind in ('rapikan','apply','undo','purge')),
    label text,
    created_at text
);

create table if not exists moves (
    id text primary key,
    batch_id text not null references batches(id),
    unit_id text,
    file_id text,
    src_rel text,
    dst_rel text,
    status text not null check (status in ('pending','done','failed','undone','purged')),
    created_at text,
    done_at text
);

create table if not exists event_overrides (
    id text primary key,
    kind text not null check (kind in ('not_event','rename','merge')),
    name_key text not null,
    value text
);

create table if not exists settings (
    key text primary key,
    value text not null
);

create table if not exists ai_embeddings (
    id text primary key,
    unit_id text not null references units(id),
    model text not null,
    dim integer not null,
    vector blob not null,
    created_at text,
    unique (unit_id, model)
);

create table if not exists ai_errors (
    id text primary key,
    unit_id text not null references units(id),
    model text not null,
    error text,
    created_at text,
    unique (unit_id, model)
);

create table if not exists ai_labels (
    id text primary key,
    unit_id text not null references units(id),
    head text not null,
    head_version text not null,
    grp text not null,
    sub text,
    prob real not null,
    model text,
    created_at text,
    unique (unit_id, head)
);

create table if not exists ai_feedback (
    id text primary key,
    unit_id text not null references units(id),
    grp text not null,
    source text not null check (source in ('koreksi','konfirmasi')),
    created_at text
);
create index if not exists idx_ai_feedback_unit on ai_feedback(unit_id);

create table if not exists date_overrides (
    id text primary key,
    unit_id text not null references units(id),
    taken_at text not null,
    precision text not null check (precision in ('month','day','second')),
    source text not null check (source in ('borrowed','event','manual')),
    ref text,
    decided_by text not null check (decided_by in ('auto','owner')),
    created_at text
);
create index if not exists idx_date_overrides_unit on date_overrides(unit_id);

create table if not exists event_date_decisions (
    id text primary key,
    event_key text not null,
    decision text not null check (decision in ('approved','ignored')),
    range_start text,
    range_end text,
    camera_start text,
    camera_end text,
    created_at text
);
create index if not exists idx_event_date_decisions_key on event_date_decisions(event_key);

create table if not exists xmp_writes (
    id text primary key,
    file_id text not null references files(id),
    xmp_rel_path text not null,
    taken_at text,
    sha256 text,
    created_at text
);

-- Dropped and recreated on every schema run so the definition can change across versions.
drop view if exists unit_dates;
create view unit_dates as
with ranked as (
    select o.unit_id, o.taken_at, o.source, o.precision,
           row_number() over (
               partition by o.unit_id
               order by (o.decided_by = 'owner') desc, o.created_at desc, o.rowid desc
           ) as rn
    from date_overrides o
    join units u on u.id = o.unit_id
    where o.decided_by = 'owner'
       or u.date_source is null
       or u.date_source not in ('exif','quicktime')
)
select u.id as unit_id,
       coalesce(r.taken_at, u.taken_at) as taken_at,
       case when r.unit_id is not null then r.source else u.date_source end as source,
       case
           when r.unit_id is not null then r.precision
           when u.taken_at is null then null
           when u.date_source = 'filename' and time(u.taken_at) = '00:00:00' then 'day'
           else 'second'
       end as precision
from units u
left join ranked r on r.unit_id = u.id and r.rn = 1;
"""


def new_id() -> str:
    return str(uuid.uuid4())


def open_db(root: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    state = Path(root) / STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(long_path(state / "arsip.db"), timeout=BUSY_TIMEOUT_MS / 1000,
        check_same_thread=check_same_thread,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("pragma foreign_keys=on")
    # Only touch the file (write lock) when the schema is missing or outdated, so opening
    # a connection never stalls behind a long writer such as a scan batch.
    if conn.execute("pragma user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.execute("pragma journal_mode=wal")
        conn.executescript(SCHEMA)
        conn.execute(f"pragma user_version={SCHEMA_VERSION}")
        conn.commit()
    return conn
