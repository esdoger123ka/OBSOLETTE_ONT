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
                  o.type_old, o.vendor_old, o.lat, o.lon, o.followup_date, o.flag
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
             o.group_uid, o.no_inet
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
