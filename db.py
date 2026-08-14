import asyncpg
from typing import Optional, List
import config

_pool: Optional[asyncpg.Pool] = None


async def init(dsn: str = None):
    global _pool
    _pool = await asyncpg.create_pool(dsn or config.DATABASE_URL, min_size=1, max_size=8)
    return _pool


async def close():
    if _pool:
        await _pool.close()


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db.init() belum dipanggil")
    return _pool


# ---------------- settings ----------------

async def get_setting(key: str, default: str = None) -> Optional[str]:
    r = await pool().fetchval("SELECT value FROM settings WHERE key=$1", key)
    return r if r is not None else default


async def set_setting(key: str, value: str, by: int = None):
    await pool().execute(
        """INSERT INTO settings(key,value,updated_by,updated_at)
           VALUES($1,$2,$3,now())
           ON CONFLICT (key) DO UPDATE
           SET value=EXCLUDED.value, updated_by=EXCLUDED.updated_by, updated_at=now()""",
        key, value, by,
    )


# ---------------- teknisi / admin ----------------

async def get_teknisi(uid: int):
    return await pool().fetchrow("SELECT * FROM teknisi WHERE teknisi_id=$1", uid)


async def onboard(uid: int, username: str, first_name: str) -> bool:
    """Tandai teknisi siap menerima DM. False kalau user_id tidak terdaftar."""
    r = await pool().execute(
        """UPDATE teknisi
           SET onboarded_at = COALESCE(onboarded_at, now()),
               tg_username=$2, tg_first_name=$3
           WHERE teknisi_id=$1""",
        uid, username, first_name,
    )
    return r.endswith("1")


async def is_admin(uid: int) -> bool:
    if uid in config.BOOTSTRAP_ADMINS:
        return True
    return bool(await pool().fetchval("SELECT 1 FROM admin_user WHERE teknisi_id=$1", uid))


async def tambah_teknisi(uid: int, nik: str, nama: str) -> str:
    """'baru' | 'diperbarui' | 'nik_dipakai'"""
    bentrok = await pool().fetchval(
        "SELECT teknisi_id FROM teknisi WHERE nik=$1 AND teknisi_id<>$2", nik, uid)
    if bentrok:
        return "nik_dipakai"
    ada = await pool().fetchval("SELECT 1 FROM teknisi WHERE teknisi_id=$1", uid)
    await pool().execute(
        """INSERT INTO teknisi(teknisi_id,nik,nama,aktif) VALUES($1,$2,$3,TRUE)
           ON CONFLICT (teknisi_id) DO UPDATE
           SET nik=EXCLUDED.nik, nama=EXCLUDED.nama, aktif=TRUE""",
        uid, nik, nama)
    return "diperbarui" if ada else "baru"


async def set_sektor(uid: int, sektor):
    await pool().execute("UPDATE teknisi SET sektor=$2 WHERE teknisi_id=$1", uid, sektor)


async def aktifkan_teknisi(uid: int):
    await pool().execute("UPDATE teknisi SET aktif=TRUE WHERE teknisi_id=$1", uid)


async def tambah_admin(uid: int, nama: str) -> bool:
    """False kalau sudah terdaftar sebagai admin."""
    ada = await pool().fetchval("SELECT 1 FROM admin_user WHERE teknisi_id=$1", uid)
    await pool().execute(
        """INSERT INTO admin_user(teknisi_id,nama) VALUES($1,$2)
           ON CONFLICT (teknisi_id) DO UPDATE SET nama=EXCLUDED.nama""", uid, nama)
    return not ada


async def hapus_admin(uid: int) -> bool:
    r = await pool().execute("DELETE FROM admin_user WHERE teknisi_id=$1", uid)
    return r.endswith("1")


async def daftar_admin():
    return await pool().fetch(
        "SELECT teknisi_id, nama, created_at FROM admin_user ORDER BY nama")


async def belum_onboarding():
    return await pool().fetch(
        "SELECT nik, nama, teknisi_id FROM teknisi WHERE aktif AND onboarded_at IS NULL ORDER BY nama"
    )


# ---------------- order ----------------

async def owner_of(no_inet: str) -> Optional[int]:
    return await pool().fetchval(
        "SELECT teknisi_id FROM v_order_owner WHERE no_inet=$1", no_inet
    )


async def get_order(no_inet: str):
    return await pool().fetchrow(
        """SELECT o.*, v.teknisi_id AS owner_id, t.nama AS owner_nama
           FROM orders o
           JOIN v_order_owner v ON v.no_inet=o.no_inet
           LEFT JOIN teknisi t ON t.teknisi_id=v.teknisi_id
           WHERE o.no_inet=$1""",
        no_inet,
    )


async def antrian(uid: int, limit: int) -> List[asyncpg.Record]:
    """Order aktif milik teknisi, diurutkan: kendala jatuh tempo dulu,
    lalu yang sudah jalan, lalu klaster dengan sisa terbanyak."""
    return await pool().fetch(
        """SELECT o.no_inet, o.group_uid, o.zona, o.status, o.speed_mb,
                  o.type_old, o.vendor_old, o.lat, o.lon, o.followup_date, o.flag,
                  o.odp, o.odc, o.sektor
           FROM orders o
           JOIN v_order_owner v ON v.no_inet=o.no_inet
           WHERE v.teknisi_id=$1
             AND o.status NOT IN ('CLOSED','BATAL')
             AND (o.status <> 'KENDALA'
                  OR o.followup_date IS NULL
                  OR o.followup_date <= CURRENT_DATE)
           ORDER BY
             CASE o.status
               WHEN 'CONFIG_OK'  THEN 0
               WHEN 'REQ_CONFIG' THEN 1
               WHEN 'GANTI_OK'   THEN 2
               WHEN 'TIKET_OPEN' THEN 3
               WHEN 'REQ_TIKET'  THEN 4
               WHEN 'CARING_OK'  THEN 5
               WHEN 'KENDALA'    THEN 6
               ELSE 7 END,
             o.odc, o.odp, o.no_inet
           LIMIT $2""",
        uid, limit,
    )


async def transisi(no_inet: str, status_to: str, actor: int, *,
                   role: str = "teknisi", catatan: str = None,
                   kode_kendala: str = None, sn_new: str = None,
                   foto: str = None, extra_sql: str = "", extra_args=()):
    """Ubah status + catat ke progress dalam satu transaksi."""
    async with pool().acquire() as con:
        async with con.transaction():
            old = await con.fetchval(
                "SELECT status FROM orders WHERE no_inet=$1 FOR UPDATE", no_inet
            )
            if old is None:
                raise ValueError("order tidak ditemukan")
            sql = f"UPDATE orders SET status=$2, updated_at=now(){extra_sql} WHERE no_inet=$1"
            await con.execute(sql, no_inet, status_to, *extra_args)
            await con.execute(
                """INSERT INTO progress(no_inet,status_from,status_to,kode_kendala,
                                        catatan,sn_new,foto_file_id,actor,actor_role)
                   VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                no_inet, old, status_to, kode_kendala, catatan, sn_new, foto, actor, role,
            )
            await con.execute(
                """UPDATE klaster SET terakhir_aktif=now()
                   WHERE group_uid=(SELECT group_uid FROM orders WHERE no_inet=$1)""",
                no_inet)
            return old


# ---------------- assignment ----------------

async def set_assignment(group_uid: str, teknisi_id: int, by: int, catatan: str = None):
    async with pool().acquire() as con:
        async with con.transaction():
            await con.execute(
                "UPDATE assignment SET aktif=FALSE, ended_at=now() WHERE group_uid=$1 AND aktif",
                group_uid,
            )
            await con.execute(
                """INSERT INTO assignment(group_uid,teknisi_id,assigned_by,catatan)
                   VALUES($1,$2,$3,$4)""",
                group_uid, teknisi_id, by, catatan,
            )
            await con.execute(
                """UPDATE orders SET status='ASSIGNED', assigned_at=now(), updated_at=now()
                   WHERE group_uid=$1 AND status='NEW'""",
                group_uid,
            )


# ---------------- klaim mandiri (pull terkunci) ----------------

async def sisa_sektor():
    return await pool().fetch("SELECT * FROM v_sisa_sektor ORDER BY sektor")


async def boleh_keluar_sektor(sektor: int) -> tuple:
    """(boleh, sisa) — teknisi sektor boleh ambil di luar sektornya kalau
    sisa di sektornya sendiri sudah di bawah ambang."""
    ambang = int(await get_setting("ambang_keluar_sektor", "50"))
    sisa = await pool().fetchval(
        """SELECT COUNT(*) FROM orders
           WHERE sektor=$1 AND status NOT IN ('CLOSED','BATAL')""", sektor) or 0
    return sisa <= ambang, sisa


async def luar_rjw_dibuka() -> bool:
    ambang = int(await get_setting("ambang_buka_luar_rjw", "100"))
    sisa = await pool().fetchval(
        """SELECT COUNT(*) FROM orders
           WHERE sektor IS NOT NULL AND status NOT IN ('CLOSED','BATAL')""") or 0
    return sisa <= ambang


async def kolam_terdekat(lat: float, lon: float, radius_km: float,
                         limit: int = 8, sektor: int = None,
                         kunci_sektor: bool = False):
    """Klaster tanpa pemilik.

    kunci_sektor=True  -> hanya klaster di sektor teknisi tersebut
    kunci_sektor=False -> semua sektor, tapi yang paling tertinggal naik
                          ke atas lewat bobot ketertinggalan
    """
    diskon = float(await get_setting("diskon_umur_km_per_hari", "0.15"))
    maxhari = int(await get_setting("max_diskon_hari", "20"))
    buka_luar = await luar_rjw_dibuka()

    # sektor dengan sisa terbanyak dapat potongan jarak, supaya teknisi
    # bebas terarah ke sana lebih dulu
    bobot = {}
    rows = await pool().fetch(
        """SELECT sektor, COUNT(*) AS n FROM orders
           WHERE sektor IS NOT NULL AND status NOT IN ('CLOSED','BATAL')
           GROUP BY sektor""")
    if rows:
        maxn = max(r["n"] for r in rows) or 1
        for r in rows:
            bobot[r["sektor"]] = 2.0 * r["n"] / maxn   # 0..2 km potongan

    hasil = await pool().fetch(
        """WITH j AS (
             SELECT v.*,
                    6371 * 2 * asin(sqrt(
                      power(sin(radians(v.lat - $1) / 2), 2) +
                      cos(radians($1)) * cos(radians(v.lat)) *
                      power(sin(radians(v.lon - $2) / 2), 2)
                    )) AS km
             FROM v_kolam v
             WHERE ($7::int IS NULL OR NOT $8 OR v.sektor = $7)
               AND (v.sektor IS NOT NULL OR $9)
           )
           SELECT group_uid, zona, lat, lon, prioritas, sektor, odc,
                  sisa, diam_hari, km,
                  km - ($5 * LEAST(diam_hari, $6)) AS skor
           FROM j
           WHERE km <= $3 OR prioritas
           ORDER BY prioritas DESC, skor ASC
           LIMIT $4""",
        lat, lon, radius_km, limit * 3, diskon, maxhari,
        sektor, kunci_sektor, buka_luar)

    if kunci_sektor:
        return hasil[:limit]
    urut = sorted(hasil, key=lambda r: (not r["prioritas"],
                                        r["skor"] - bobot.get(r["sektor"], 0)))
    return urut[:limit]


async def klaim_aktif(teknisi_id: int) -> tuple:
    """(klaster yang masih bisa dikerjakan, total klaster hasil klaim)."""
    r = await pool().fetchrow(
        "SELECT n_klaim, n_pegang FROM v_klaim_aktif WHERE teknisi_id=$1", teknisi_id)
    return (r["n_klaim"], r["n_pegang"]) if r else (0, 0)


async def klaim(group_uid: str, teknisi_id: int, *, lat=None, lon=None,
                jarak=None, live=None):
    """Ambil klaster dari kolam. Gagal (False) kalau keburu diambil orang lain."""
    hari = int(await get_setting("klaim_expire_hari", "5"))
    async with pool().acquire() as con:
        async with con.transaction():
            ada = await con.fetchval(
                "SELECT 1 FROM assignment WHERE group_uid=$1 AND aktif FOR UPDATE",
                group_uid)
            if ada:
                return False
            await con.execute(
                """INSERT INTO assignment(group_uid,teknisi_id,assigned_by,
                                          claim_mode,expires_at)
                   VALUES($1,$2,$2,'self', now() + make_interval(days => $3))""",
                group_uid, teknisi_id, hari)
            await con.execute(
                """UPDATE orders SET status='ASSIGNED', assigned_at=now(), updated_at=now()
                   WHERE group_uid=$1 AND status='NEW'""", group_uid)
            await con.execute(
                "UPDATE klaster SET terakhir_aktif=now() WHERE group_uid=$1", group_uid)
            await con.execute(
                """INSERT INTO klaim_log(group_uid,teknisi_id,aksi,lat,lon,jarak_km,live)
                   VALUES($1,$2,'klaim',$3,$4,$5,$6)""",
                group_uid, teknisi_id, lat, lon, jarak, live)
            return True


async def lepas(group_uid: str, teknisi_id: int, aksi: str = "lepas"):
    async with pool().acquire() as con:
        async with con.transaction():
            await con.execute(
                "UPDATE assignment SET aktif=FALSE, ended_at=now() WHERE group_uid=$1 AND aktif",
                group_uid)
            await con.execute(
                """INSERT INTO klaim_log(group_uid,teknisi_id,aksi)
                   VALUES($1,$2,$3)""", group_uid, teknisi_id, aksi)


async def klaim_saya(teknisi_id: int):
    return await pool().fetch(
        """SELECT a.group_uid, a.expires_at, a.claim_mode,
                  COUNT(o.no_inet) AS sisa
           FROM assignment a
           JOIN orders o ON o.group_uid=a.group_uid AND o.status NOT IN ('CLOSED','BATAL')
           WHERE a.aktif AND a.teknisi_id=$1
           GROUP BY a.group_uid, a.expires_at, a.claim_mode
           ORDER BY a.expires_at NULLS LAST""",
        teknisi_id)


async def klaim_kedaluwarsa():
    """Klaim mandiri yang klasternya tidak ada aktivitas apa pun selama
    N hari. Berbasis klaster.terakhir_aktif, bukan tanggal klaim — supaya
    klaster yang sedang dikerjakan tidak ikut ditarik."""
    hari = int(await get_setting("klaim_expire_hari", "5"))
    return await pool().fetch(
        """SELECT a.group_uid, a.teknisi_id, COUNT(o.no_inet) AS sisa,
                  EXTRACT(DAY FROM now()-k.terakhir_aktif)::INT AS diam_hari
           FROM assignment a
           JOIN klaster k ON k.group_uid = a.group_uid
           JOIN orders o  ON o.group_uid = a.group_uid
                         AND o.status NOT IN ('CLOSED','BATAL')
           WHERE a.aktif AND a.claim_mode='self'
             AND k.terakhir_aktif < now() - make_interval(days => $1)
           GROUP BY a.group_uid, a.teknisi_id, k.terakhir_aktif""",
        hari)


async def set_prioritas(group_uid: str, nyala: bool) -> bool:
    r = await pool().execute(
        "UPDATE klaster SET prioritas=$2 WHERE group_uid=$1", group_uid, nyala)
    return r.endswith("1")


async def ringkas_kolam():
    return await pool().fetchrow(
        """SELECT COUNT(*) AS klaster, COALESCE(SUM(sisa),0) AS order_sisa,
                  COUNT(*) FILTER (WHERE prioritas) AS didorong,
                  COALESCE(MAX(diam_hari),0) AS terlama
           FROM v_kolam""")


# ---------------- monitoring ----------------

async def produksi_hari(tanggal_offset: int = 0):
    """Jumlah transisi status hari ini (atau H-n), berbasis tabel progress."""
    return await pool().fetch(
        """SELECT status_to, COUNT(*) AS n
           FROM progress
           WHERE (ts AT TIME ZONE 'Asia/Jakarta')::date
                 = (now() AT TIME ZONE 'Asia/Jakarta')::date - $1::int
           GROUP BY status_to ORDER BY n DESC""",
        tanggal_offset)


async def top_teknisi_hari(batas: int = 10):
    return await pool().fetch(
        """SELECT t.nama, COUNT(*) AS n
           FROM progress p JOIN teknisi t ON t.teknisi_id = p.actor
           WHERE p.status_to = 'CLOSED'
             AND (p.ts AT TIME ZONE 'Asia/Jakarta')::date
                 = (now() AT TIME ZONE 'Asia/Jakarta')::date
           GROUP BY t.nama ORDER BY n DESC LIMIT $1""", batas)


async def progres_kumulatif():
    return await pool().fetchrow(
        """SELECT
             COUNT(*)                                          AS total,
             COUNT(*) FILTER (WHERE status='CLOSED')           AS closed,
             COUNT(*) FILTER (WHERE status='KENDALA')          AS kendala,
             COUNT(*) FILTER (WHERE status='NEW')              AS belum_assign,
             COUNT(*) FILTER (WHERE status NOT IN ('CLOSED','BATAL','NEW')) AS jalan
           FROM orders""")


async def laju_harian(hari: int = 7):
    """Rata-rata order ditutup per hari selama N hari terakhir."""
    return await pool().fetchval(
        """SELECT COUNT(*)::float / GREATEST($1,1)
           FROM progress
           WHERE status_to='CLOSED'
             AND ts >= now() - make_interval(days => $1)""", hari)


async def tren_harian(hari: int = 14):
    return await pool().fetch(
        """SELECT (ts AT TIME ZONE 'Asia/Jakarta')::date AS tgl, COUNT(*) AS n
           FROM progress
           WHERE status_to='CLOSED' AND ts >= now() - make_interval(days => $1)
           GROUP BY 1 ORDER BY 1 DESC""", hari)


async def detail_export():
    return await pool().fetch(
        """SELECT o.no_inet, o.group_uid, o.zona, o.flag, o.status,
                  k.nama AS teknisi, o.speed_mb, o.vendor_old, o.type_old,
                  o.sn_old, o.sn_new, t.no_tiket, t.jenis AS jenis_tiket,
                  o.kode_kendala, o.catatan_kendala, o.followup_date, o.percobaan,
                  to_char(o.caring_at    AT TIME ZONE 'Asia/Jakarta','YYYY-MM-DD HH24:MI') AS caring,
                  to_char(o.req_tiket_at AT TIME ZONE 'Asia/Jakarta','YYYY-MM-DD HH24:MI') AS req_tiket,
                  to_char(o.tiket_at     AT TIME ZONE 'Asia/Jakarta','YYYY-MM-DD HH24:MI') AS tiket_terbit,
                  to_char(o.ganti_at     AT TIME ZONE 'Asia/Jakarta','YYYY-MM-DD HH24:MI') AS ganti_ont,
                  to_char(o.config_at    AT TIME ZONE 'Asia/Jakarta','YYYY-MM-DD HH24:MI') AS config_ok,
                  to_char(o.closed_at    AT TIME ZONE 'Asia/Jakarta','YYYY-MM-DD HH24:MI') AS closed,
                  o.lat, o.lon
           FROM orders o
           LEFT JOIN v_order_owner v ON v.no_inet = o.no_inet
           LEFT JOIN teknisi k       ON k.teknisi_id = v.teknisi_id
           LEFT JOIN tickets t       ON t.no_inet = o.no_inet
           ORDER BY o.zona, o.group_uid, o.no_inet""")


async def sisa_sekitar(group_uid: str, teknisi_id: int, kecuali: str) -> int:
    """Order lain milik teknisi ini di klaster yang sama dan belum selesai."""
    return await pool().fetchval(
        """SELECT COUNT(*) FROM v_order_owner
           WHERE group_uid=$1 AND teknisi_id=$2 AND no_inet<>$3
             AND status NOT IN ('CLOSED','BATAL')""",
        group_uid, teknisi_id, kecuali) or 0


async def isi_klaster(group_uid: str):
    return await pool().fetch(
        """SELECT o.no_inet, o.odp, o.status, o.speed_mb, o.kode_kendala,
                  o.followup_date, t.nama AS teknisi
           FROM orders o
           LEFT JOIN v_order_owner v ON v.no_inet = o.no_inet
           LEFT JOIN teknisi t       ON t.teknisi_id = v.teknisi_id
           WHERE o.group_uid = $1
           ORDER BY o.odp, o.no_inet""", group_uid)


async def info_klaster(group_uid: str):
    return await pool().fetchrow(
        """SELECT k.group_uid, k.odc, k.sektor, k.prioritas, k.lat, k.lon,
                  k.terakhir_aktif, t.nama AS pemilik, a.claim_mode,
                  EXTRACT(DAY FROM now()-k.terakhir_aktif)::INT AS diam_hari
           FROM klaster k
           LEFT JOIN assignment a ON a.group_uid = k.group_uid AND a.aktif
           LEFT JOIN teknisi t    ON t.teknisi_id = a.teknisi_id
           WHERE k.group_uid = $1""", group_uid)


async def klaster_ada(group_uid: str) -> bool:
    return bool(await pool().fetchval(
        "SELECT 1 FROM klaster WHERE group_uid=$1", group_uid))


async def hapus_assignment_hantu() -> int:
    """Penugasan yang menunjuk klaster tidak ada. Sisa bug lama."""
    r = await pool().execute(
        """DELETE FROM assignment a
           WHERE NOT EXISTS (SELECT 1 FROM klaster k WHERE k.group_uid = a.group_uid)""")
    return int(r.split()[-1])


async def cari_klaster(q: str):
    return await pool().fetch(
        """SELECT group_uid FROM klaster
           WHERE group_uid ILIKE '%'||$1||'%' OR odc ILIKE '%'||$1||'%'
           ORDER BY group_uid LIMIT 12""", q)


async def klaster_zona(zona: str):
    return await pool().fetch(
        "SELECT DISTINCT group_uid FROM orders WHERE zona=$1 ORDER BY group_uid", zona
    )


async def cari_teknisi(q: str):
    return await pool().fetch(
        """SELECT teknisi_id, nik, nama FROM teknisi
           WHERE aktif AND (nama ILIKE '%'||$1||'%' OR nik = $1 OR teknisi_id::TEXT = $1)
           ORDER BY nama LIMIT 10""",
        q,
    )


async def beban():
    return await pool().fetch("SELECT * FROM v_beban WHERE aktif ORDER BY sisa DESC")


async def stagnan(hari: int):
    return await pool().fetch(
        "SELECT * FROM v_stagnan WHERE diam_hari >= $1 ORDER BY diam_hari DESC, sisa DESC LIMIT 30",
        hari,
    )


# ---------------- laporan ----------------

async def funnel():
    return await pool().fetch(
        """SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status"""
    )


async def aging(status: str, kolom: str):
    return await pool().fetch(
        f"""SELECT o.no_inet, o.zona, t.nama,
                   EXTRACT(EPOCH FROM now()-o.{kolom})/3600 AS jam
            FROM orders o
            JOIN v_order_owner v ON v.no_inet=o.no_inet
            LEFT JOIN teknisi t ON t.teknisi_id=v.teknisi_id
            WHERE o.status=$1 AND o.{kolom} IS NOT NULL
            ORDER BY o.{kolom} ASC LIMIT 50""",
        status,
    )


async def pareto_kendala():
    return await pool().fetch(
        """SELECT o.kode_kendala, k.label, COUNT(*) AS n
           FROM orders o JOIN kendala_ref k ON k.kode=o.kode_kendala
           WHERE o.status='KENDALA'
           GROUP BY o.kode_kendala, k.label ORDER BY n DESC"""
    )


async def sn_dipakai(sn: str, kecuali: str) -> Optional[str]:
    return await pool().fetchval(
        "SELECT no_inet FROM orders WHERE sn_new=$1 AND no_inet<>$2", sn, kecuali
    )
