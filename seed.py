"""
Seeder sekali jalan: isi tabel teknisi, orders, dan assignment awal
dari MASTER_ONT_OBSOLETE_RJW.xlsx.

    python seed.py MASTER_ONT_OBSOLETE_RJW.xlsx

Aman dijalankan ulang: order yang sudah ada tidak ditimpa statusnya,
assignment tidak diubah kalau klaster sudah punya pemilik aktif.
"""
import asyncio
import sys
import pandas as pd
import db


async def main(path: str):
    await db.init()
    master = pd.read_excel(path, sheet_name="MASTER")
    rekap = pd.read_excel(path, sheet_name="REKAP_TEKNISI")

    master["no_inet"] = master["no_inet"].astype(str).str.strip()
    master["user_id"] = master["user_id"].astype(str).str.strip()
    master["nik"] = master["nik"].astype(str).str.strip()

    # ---- teknisi ----
    tek = (master[master.user_id != "-"][["user_id", "nik", "teknisi"]]
           .drop_duplicates(subset=["user_id"]))
    rows = [(int(r.user_id), r.nik, r.teknisi) for r in tek.itertuples()]
    await db.pool().executemany(
        """INSERT INTO teknisi(teknisi_id,nik,nama) VALUES($1,$2,$3)
           ON CONFLICT (teknisi_id) DO UPDATE SET nama=EXCLUDED.nama, nik=EXCLUDED.nik""",
        rows,
    )
    print(f"teknisi: {len(rows)}")

    # ---- orders ----
    o = master.where(pd.notna(master), None)
    orows = [
        (r.no_inet, r.group_uid, r.zona,
         int(r.speed_mb) if r.speed_mb is not None else None,
         r.sn_old, r.type_old, r.vendor_old,
         float(r.lat) if r.lat is not None else None,
         float(r.lon) if r.lon is not None else None,
         r.flag)
        for r in o.itertuples()
    ]
    await db.pool().executemany(
        """INSERT INTO orders(no_inet,group_uid,zona,speed_mb,sn_old,type_old,
                              vendor_old,lat,lon,flag)
           VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
           ON CONFLICT (no_inet) DO UPDATE SET
             group_uid=EXCLUDED.group_uid, zona=EXCLUDED.zona,
             speed_mb=EXCLUDED.speed_mb, sn_old=EXCLUDED.sn_old,
             type_old=EXCLUDED.type_old, vendor_old=EXCLUDED.vendor_old,
             lat=EXCLUDED.lat, lon=EXCLUDED.lon, flag=EXCLUDED.flag""",
        orows,
    )
    print(f"orders: {len(orows)}")

    # ---- centroid klaster (untuk query jarak saat klaim) ----
    cen = (master[master.zona != "CEK-GEO"]
           .groupby(["group_uid", "zona"], as_index=False)
           .agg(lat=("lat", "mean"), lon=("lon", "mean")))
    await db.pool().executemany(
        """INSERT INTO klaster(group_uid,zona,lat,lon) VALUES($1,$2,$3,$4)
           ON CONFLICT (group_uid) DO UPDATE SET
             zona=EXCLUDED.zona, lat=EXCLUDED.lat, lon=EXCLUDED.lon""",
        [(r.group_uid, r.zona, float(r.lat), float(r.lon)) for r in cen.itertuples()],
    )
    print(f"klaster: {len(cen)}")

    # ---- assignment awal ----
    mode = await db.get_setting("mode_klaim", "hibrida")
    if mode == "kolam":
        print("mode_klaim=kolam — semua klaster dilepas ke kolam, tidak ada penugasan awal")
        n = 0
    else:
        zmap = {r.zona: int(r.user_id) for r in rekap.itertuples()}
        n = 0
        for r in cen.itertuples():
            tid = zmap.get(r.zona)
            if not tid:
                continue
            sudah = await db.pool().fetchval(
                "SELECT 1 FROM assignment WHERE group_uid=$1 AND aktif", r.group_uid
            )
            if sudah:
                continue
            await db.set_assignment(r.group_uid, tid, by=None, catatan="seed awal")
            n += 1
    print(f"assignment baru: {n}")

    kg = await db.pool().fetchval("SELECT COUNT(*) FROM orders WHERE zona='CEK-GEO'")
    print(f"CEK-GEO (tidak diassign): {kg}")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "MASTER_ONT_OBSOLETE_RJW.xlsx"))
