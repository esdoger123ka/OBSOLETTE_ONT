"""
Pindahkan order ke klaster baru berbasis ODC/sektor.

    python remap_sektor.py MASTER_ONT_SEKTOR_RJW.xlsx

Status pengerjaan, tiket, SN, foto, dan riwayat TIDAK disentuh — hanya
pemetaan klaster, ODC, dan sektor yang diperbarui.

Assignment lama dihapus karena klaster lama sudah tidak ada. Order
dikembalikan ke kolam supaya teknisi mengambil sendiri sesuai sektornya.
Order yang sudah jalan (melewati tahap caring) tetap dipegang pemilik
lamanya lewat kolom teknisi_override, supaya pekerjaan yang sedang
berlangsung tidak hilang.
"""
import asyncio
import sys
import pandas as pd
import db


async def main(path: str):
    await db.init()
    m = pd.read_excel(path, sheet_name="MASTER")
    k = pd.read_excel(path, sheet_name="KLASTER")
    t = pd.read_excel(path, sheet_name="TEKNISI")

    m["no_inet"] = m["no_inet"].astype(str).str.strip()
    m = m.astype(object).where(pd.notna(m), None)

    # 1. Kunci pekerjaan yang sedang berjalan ke pemilik lamanya
    n = await db.pool().execute(
        """UPDATE orders o SET teknisi_override = v.teknisi_id
           FROM v_order_owner v
           WHERE v.no_inet = o.no_inet
             AND o.teknisi_override IS NULL
             AND v.teknisi_id IS NOT NULL
             AND o.status NOT IN ('NEW','ASSIGNED','CLOSED','BATAL')""")
    print(f"order sedang jalan dikunci ke pemiliknya: {n}")

    # 2. Sektor teknisi
    rows = [(int(r.user_id), int(r.sektor) if pd.notna(r.sektor) else None)
            for r in t.itertuples()]
    await db.pool().executemany(
        "UPDATE teknisi SET sektor=$2 WHERE teknisi_id=$1", rows)
    print(f"teknisi diberi sektor: {sum(1 for _, s in rows if s)} sektor, "
          f"{sum(1 for _, s in rows if not s)} bebas")

    # 3. Klaster baru
    await db.pool().execute("DELETE FROM assignment WHERE aktif = FALSE")
    await db.pool().execute("UPDATE assignment SET aktif=FALSE, ended_at=now() WHERE aktif")
    await db.pool().execute("DELETE FROM klaster")
    krows = [(r.group_uid, f"S{int(r.sektor)}", float(r.lat), float(r.lon),
              r.odc, int(r.sektor)) for r in k.itertuples()]
    krows.append(("LUAR-RJW", "LUAR", -6.9203, 107.5729, None, None))
    await db.pool().executemany(
        """INSERT INTO klaster(group_uid,zona,lat,lon,odc,sektor)
           VALUES($1,$2,$3,$4,$5,$6)
           ON CONFLICT (group_uid) DO UPDATE SET
             zona=EXCLUDED.zona, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
             odc=EXCLUDED.odc, sektor=EXCLUDED.sektor""", krows)
    print(f"klaster: {len(krows)}")

    # 4. Order dipetakan ulang
    orows = [(r.no_inet, r.group_uid, r.odc, r.odp,
              int(r.sektor) if r.sektor is not None else None,
              f"S{int(r.sektor)}" if r.sektor is not None else "LUAR",
              r.flag)
             for r in m.itertuples()]
    await db.pool().executemany(
        """UPDATE orders SET group_uid=$2, odc=$3, odp=$4, sektor=$5,
                             zona=$6, flag=$7, updated_at=now()
           WHERE no_inet=$1""", orows)
    print(f"order dipetakan ulang: {len(orows)}")

    # 5. Order yang belum tersentuh dikembalikan ke antrian
    n = await db.pool().execute(
        """UPDATE orders SET status='NEW', assigned_at=NULL
           WHERE status='ASSIGNED' AND teknisi_override IS NULL""")
    print(f"dikembalikan ke kolam: {n}")

    for r in await db.pool().fetch(
            """SELECT sektor, COUNT(*) AS n FROM orders
               WHERE status NOT IN ('CLOSED','BATAL')
               GROUP BY sektor ORDER BY sektor NULLS LAST"""):
        print(f"  sektor {r['sektor'] or 'LUAR RJW'}: {r['n']} order")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "MASTER_ONT_SEKTOR_RJW.xlsx"))
