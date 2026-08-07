-- ============================================================
-- Sistem monitoring penggantian ONT obsolete - RJW
-- Postgres 14+
-- ============================================================

-- ---------- referensi ----------

CREATE TABLE IF NOT EXISTS teknisi (
    teknisi_id      BIGINT PRIMARY KEY,          -- telegram user_id
    nik             TEXT UNIQUE NOT NULL,
    nama            TEXT NOT NULL,
    aktif           BOOLEAN NOT NULL DEFAULT TRUE,
    onboarded_at    TIMESTAMPTZ,                 -- diisi saat /start; NULL = bot belum bisa kirim DM
    tg_username     TEXT,
    tg_first_name   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS admin_user (
    teknisi_id      BIGINT PRIMARY KEY,          -- telegram user_id, tidak harus ada di tabel teknisi
    nama            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      BIGINT
);

CREATE TABLE IF NOT EXISTS kendala_ref (
    kode            TEXT PRIMARY KEY,
    label           TEXT NOT NULL,
    perlu_tanggal   BOOLEAN NOT NULL DEFAULT FALSE,
    perlu_catatan   BOOLEAN NOT NULL DEFAULT FALSE,
    urutan          INT NOT NULL DEFAULT 0
);

-- ---------- data order ----------

CREATE TABLE IF NOT EXISTS orders (
    no_inet             TEXT PRIMARY KEY,
    group_uid           TEXT NOT NULL,
    zona                TEXT NOT NULL,
    speed_mb            INT,
    sn_old              TEXT,
    type_old            TEXT,
    vendor_old          TEXT,
    lat                 DOUBLE PRECISION,
    lon                 DOUBLE PRECISION,
    flag                TEXT NOT NULL DEFAULT 'OK',   -- OK | CEK_GEO | DATA_KOSONG | SUDAH_DUALBAND | MULTI_ONT
    status              TEXT NOT NULL DEFAULT 'NEW',
    -- pintu darurat: menang atas hasil join ke assignment
    teknisi_override    BIGINT REFERENCES teknisi(teknisi_id),
    -- hasil pekerjaan
    sn_new              TEXT UNIQUE,
    type_new            TEXT,
    vendor_new          TEXT,
    foto_label_sn       TEXT,          -- telegram file_id
    foto_terpasang      TEXT,          -- telegram file_id
    -- caring
    nama_plg            TEXT,
    cp_plg              TEXT,
    -- kendala aktif
    kode_kendala        TEXT REFERENCES kendala_ref(kode),
    catatan_kendala     TEXT,
    followup_date       DATE,
    percobaan           INT NOT NULL DEFAULT 0,
    -- jejak waktu per tahap
    assigned_at         TIMESTAMPTZ,
    dikirim_at          TIMESTAMPTZ,   -- terakhir kali di-push ke teknisi
    caring_at           TIMESTAMPTZ,
    req_tiket_at        TIMESTAMPTZ,
    tiket_at            TIMESTAMPTZ,
    ganti_at            TIMESTAMPTZ,
    req_config_at       TIMESTAMPTZ,
    config_at           TIMESTAMPTZ,
    closed_at           TIMESTAMPTZ,
    -- akuntansi produktivitas
    closed_by           BIGINT REFERENCES teknisi(teknisi_id),
    req_tiket_by        BIGINT REFERENCES teknisi(teknisi_id),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_status_chk;
ALTER TABLE orders ADD CONSTRAINT orders_status_chk CHECK (status IN (
    'NEW','ASSIGNED','CARING_OK','KENDALA','REQ_TIKET','TIKET_OPEN',
    'GANTI_OK','REQ_CONFIG','CONFIG_OK','CLOSED','BATAL'
));

CREATE INDEX IF NOT EXISTS ix_orders_zona    ON orders(zona);
CREATE INDEX IF NOT EXISTS ix_orders_group   ON orders(group_uid);
CREATE INDEX IF NOT EXISTS ix_orders_status  ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_follow  ON orders(followup_date) WHERE status = 'KENDALA';

-- Pemetaan klaster -> teknisi. Bukan kolom di orders, supaya perpindahan
-- kepemilikan = 1 baris baru dan riwayatnya utuh.
CREATE TABLE IF NOT EXISTS assignment (
    id              BIGSERIAL PRIMARY KEY,
    group_uid       TEXT NOT NULL,
    teknisi_id      BIGINT NOT NULL REFERENCES teknisi(teknisi_id),
    aktif           BOOLEAN NOT NULL DEFAULT TRUE,
    assigned_by     BIGINT,
    assigned_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at        TIMESTAMPTZ,
    catatan         TEXT
);

-- hanya satu pemilik aktif per klaster
CREATE UNIQUE INDEX IF NOT EXISTS ux_assignment_aktif
    ON assignment(group_uid) WHERE aktif;
CREATE INDEX IF NOT EXISTS ix_assignment_teknisi ON assignment(teknisi_id) WHERE aktif;

-- Append-only. Jangan pernah UPDATE atau DELETE di sini.
CREATE TABLE IF NOT EXISTS progress (
    id              BIGSERIAL PRIMARY KEY,
    no_inet         TEXT NOT NULL REFERENCES orders(no_inet),
    status_from     TEXT,
    status_to       TEXT NOT NULL,
    kode_kendala    TEXT,
    catatan         TEXT,
    sn_new          TEXT,
    foto_file_id    TEXT,
    actor           BIGINT,
    actor_role      TEXT NOT NULL DEFAULT 'teknisi',   -- teknisi | admin | sistem
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_progress_inet ON progress(no_inet, ts);
CREATE INDEX IF NOT EXISTS ix_progress_ts   ON progress(ts);

CREATE TABLE IF NOT EXISTS tickets (
    no_inet         TEXT PRIMARY KEY REFERENCES orders(no_inet),
    no_tiket        TEXT NOT NULL,
    jenis           TEXT NOT NULL,          -- INSERA | DSC
    requested_by    BIGINT REFERENCES teknisi(teknisi_id),
    requested_at    TIMESTAMPTZ,
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at       TIMESTAMPTZ,
    bukti_file_id   TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_no ON tickets(no_tiket);

-- ---------- view bantu ----------

-- pemilik efektif: override menang atas assignment klaster
CREATE OR REPLACE VIEW v_order_owner AS
SELECT o.no_inet,
       o.group_uid,
       o.zona,
       o.status,
       o.flag,
       COALESCE(o.teknisi_override, a.teknisi_id) AS teknisi_id,
       (o.teknisi_override IS NOT NULL)           AS is_override
FROM orders o
LEFT JOIN assignment a ON a.group_uid = o.group_uid AND a.aktif;

CREATE OR REPLACE VIEW v_beban AS
SELECT t.teknisi_id,
       t.nama,
       t.aktif,
       (t.onboarded_at IS NOT NULL)                                   AS siap_dm,
       COUNT(*) FILTER (WHERE v.status NOT IN ('CLOSED','BATAL'))     AS sisa,
       COUNT(*) FILTER (WHERE v.status = 'KENDALA')                   AS kendala,
       COUNT(*) FILTER (WHERE v.status = 'REQ_TIKET')                 AS tunggu_tiket,
       COUNT(*) FILTER (WHERE v.status = 'CLOSED')                    AS closed,
       COUNT(v.no_inet)                                               AS total
FROM teknisi t
LEFT JOIN v_order_owner v ON v.teknisi_id = t.teknisi_id
GROUP BY t.teknisi_id, t.nama, t.aktif, t.onboarded_at;

-- klaster tanpa progres > N hari: kandidat rebalancing
CREATE OR REPLACE VIEW v_stagnan AS
SELECT o.group_uid,
       o.zona,
       a.teknisi_id,
       t.nama,
       COUNT(*)                                          AS sisa,
       MAX(GREATEST(o.updated_at, o.assigned_at))        AS aktivitas_terakhir,
       EXTRACT(DAY FROM now() - MAX(GREATEST(o.updated_at, o.assigned_at)))::INT AS diam_hari
FROM orders o
LEFT JOIN assignment a ON a.group_uid = o.group_uid AND a.aktif
LEFT JOIN teknisi t    ON t.teknisi_id = a.teknisi_id
WHERE o.status NOT IN ('CLOSED','BATAL')
GROUP BY o.group_uid, o.zona, a.teknisi_id, t.nama;

-- ---------- seed referensi ----------

INSERT INTO kendala_ref (kode,label,perlu_tanggal,perlu_catatan,urutan) VALUES
 ('K01','Pelanggan tidak bisa dihubungi',   FALSE, FALSE, 1),
 ('K02','Pelanggan menolak diganti',        FALSE, TRUE,  2),
 ('K03','Rumah kosong / pelanggan luar kota',FALSE,FALSE, 3),
 ('K04','Alamat tidak ditemukan',           FALSE, TRUE,  4),
 ('K05','Pelanggan minta jadwal ulang',     TRUE,  FALSE, 5),
 ('K06','Stok ONT kosong',                  FALSE, FALSE, 6),
 ('K07','Pelanggan sudah cabut / isolir',   FALSE, FALSE, 7),
 ('K08','Data salah (sudah dual band)',     FALSE, TRUE,  8),
 ('K09','Perlu perbaikan jaringan dulu',    FALSE, TRUE,  9),
 ('K99','Lainnya',                          FALSE, TRUE, 99)
ON CONFLICT (kode) DO NOTHING;

INSERT INTO settings (key,value) VALUES
 ('kuota_harian','3'),
 ('sla_tiket_jam','8'),
 ('sla_config_jam','4'),
 ('stagnan_hari','7'),
 ('link_grup_tsel','https://t.me/'),
 ('distribusi_jam','07:30')
ON CONFLICT (key) DO NOTHING;
