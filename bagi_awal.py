"""
Pembagian awal: tiap teknisi dapat beberapa klaster supaya hari pertama
langsung ada pekerjaan. Sisanya ditinggal di kolam untuk /ambil.

    python bagi_awal.py            # 3 klaster per teknisi
    python bagi_awal.py 4          # 4 klaster per teknisi

Aturan:
  - Teknisi sektor hanya dapat klaster di sektornya sendiri.
  - Teknisi bebas dapat klaster dari sektor yang paling banyak sisanya,
    dibagi proporsional supaya beban antar sektor mengecil.
  - Klaster yang diberikan dipilih berdekatan satu sama lain, bukan acak.
  - Klaster luar RJW tidak pernah dibagikan di sini.

Aman dijalankan ulang: klaster yang sudah ada pemiliknya dilewati.
Pembagian ini bermode 'assign', jadi tidak menghitung batas /ambil.
"""
import asyncio
import sys
from math import radians, sin, cos, asin, sqrt
import db


def jarak(a, b):
    dlat = radians(b["lat"] - a["lat"])
    dlon = radians(b["lon"] - a["lon"])
    return 6371 * 2 * asin(sqrt(
        sin(dlat / 2) ** 2 +
        cos(radians(a["lat"])) * cos(radians(b["lat"])) * sin(dlon / 2) ** 2))


async def main(per_teknisi: int):
    await db.init()

    tek = await db.pool().fetch(
        """SELECT teknisi_id, nama, sektor FROM teknisi
           WHERE aktif ORDER BY sektor NULLS LAST, nama""")
    bebas_kl = {}
    for s in (1, 2, 3):
        rows = await db.pool().fetch(
            """SELECT k.group_uid, k.lat, k.lon, COUNT(o.no_inet) AS n
               FROM klaster k
               JOIN orders o ON o.group_uid = k.group_uid
                            AND o.status NOT IN ('CLOSED','BATAL')
               LEFT JOIN assignment a ON a.group_uid = k.group_uid AND a.aktif
               WHERE k.sektor = $1 AND a.id IS NULL
               GROUP BY k.group_uid, k.lat, k.lon""", s)
        bebas_kl[s] = [dict(r) for r in rows]
        print(f"sektor {s}: {len(rows)} klaster bebas, {sum(r['n'] for r in rows)} order")

    # Porsi teknisi bebas per sektor, proporsional terhadap sisa order
    sisa = {s: sum(r["n"] for r in bebas_kl[s]) for s in (1, 2, 3)}
    n_bebas = sum(1 for t in tek if t["sektor"] is None)
    butuh = n_bebas * per_teknisi
    total = sum(sisa.values()) or 1
    porsi = {s: round(butuh * sisa[s] / total) for s in (1, 2, 3)}
    print(f"teknisi bebas {n_bebas} × {per_teknisi} klaster = {butuh}, "
          f"porsi per sektor: {porsi}")

    antre_bebas = []
    for s in (1, 2, 3):
        antre_bebas += [s] * porsi[s]
    antre_bebas.sort(key=lambda s: -sisa[s])   # sektor tertinggal lebih dulu

    dibagi = 0
    for t in tek:
        target = []
        for _ in range(per_teknisi):
            if t["sektor"]:
                s = t["sektor"]
            elif antre_bebas:
                s = antre_bebas.pop(0)
            else:
                s = max(sisa, key=sisa.get)
            target.append(s)

        pusat = None
        for s in target:
            kandidat = bebas_kl[s]
            if not kandidat:
                continue
            # klaster pertama: yang ordernya paling banyak.
            # berikutnya: yang terdekat dari klaster pertama.
            if pusat is None or pusat["group_uid"] not in {k["group_uid"] for k in kandidat}:
                pilih = max(kandidat, key=lambda k: k["n"])
                pusat = pilih
            else:
                pilih = min(kandidat, key=lambda k: jarak(pusat, k))
            kandidat.remove(pilih)
            sisa[s] -= pilih["n"]
            await db.set_assignment(pilih["group_uid"], t["teknisi_id"],
                                    by=None, catatan="pembagian awal")
            dibagi += 1

    print(f"\nklaster dibagikan: {dibagi}")
    for r in await db.pool().fetch(
            """SELECT t.sektor, COUNT(DISTINCT t.teknisi_id) AS tek,
                      COUNT(o.no_inet) AS order_dibagi
               FROM teknisi t
               JOIN assignment a ON a.teknisi_id = t.teknisi_id AND a.aktif
               JOIN orders o ON o.group_uid = a.group_uid
                            AND o.status NOT IN ('CLOSED','BATAL')
               WHERE t.aktif
               GROUP BY t.sektor ORDER BY t.sektor NULLS LAST"""):
        label = f"sektor {r['sektor']}" if r["sektor"] else "bebas"
        print(f"  {label}: {r['tek']} teknisi, {r['order_dibagi']} order "
              f"({r['order_dibagi'] / r['tek']:.0f} per orang)")

    k = await db.ringkas_kolam()
    print(f"\nsisa di kolam: {k['klaster']} klaster, {k['order_sisa']} order")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3))
