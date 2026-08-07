# Bot monitoring penggantian ONT obsolete — RJW

## Urutan deploy

1. Railway → New Project → tambahkan **PostgreSQL**. Salin `DATABASE_URL`.
2. Jalankan `schema.sql` sekali (Railway punya query console, atau `psql "$DATABASE_URL" -f schema.sql`).
3. Buat bot lewat @BotFather, ambil token.
4. Set variable di Railway:
   - `BOT_TOKEN`
   - `DATABASE_URL`
   - `TZ=Asia/Jakarta`
   - `BOOTSTRAP_ADMINS=<user_id Anda>` (dipisah koma kalau lebih dari satu)
5. Isi data: `python seed.py MASTER_ONT_OBSOLETE_RJW.xlsx`
   Aman dijalankan ulang — status order yang sudah jalan tidak ditimpa.
6. Deploy (`Procfile` sudah ada, tipe service = worker, bukan web).

## Sebelum distribusi pertama

Bot **tidak bisa mengirim DM ke user yang belum menekan Start**. Urutannya:

1. Sebar link bot ke grup teknisi
2. `/onboarding` → daftar siapa yang belum
3. Kejar sisanya lewat TL
4. Baru nyalakan distribusi pagi

Job `job_distribusi` melewati teknisi yang `onboarded_at IS NULL`, jadi aman
dijalankan sebelum semua onboarding.

## Perintah teknisi

| Perintah | Fungsi |
|---|---|
| `/start` | Daftar / aktifkan DM |
| `/order` | Order hari ini (sesuai kuota) |
| `/sisa` | Ringkasan sisa, kendala, tunggu tiket |
| `/cari <no_inet>` | Buka satu order |
| `/batal` | Batalkan input yang sedang berjalan |

Selebihnya lewat tombol. Tombol yang muncul hanya yang relevan dengan status
saat itu, jadi teknisi tidak bisa melompati tahap.

## Perintah admin

| Perintah | Fungsi |
|---|---|
| `/beban` | Sisa order per teknisi, `⚠` = belum onboarding |
| `/assign <group_uid> <nama\|nik>` | Pindah satu klaster (~10 order) |
| `/pindahzona <zona> <nama\|nik>` | Pindah seluruh zona (~8 klaster) |
| `/stagnan` | Klaster tanpa progres ≥ N hari |
| `/tunggutiket` | Order mandek menunggu tiket, diurut terlama |
| `/rekap` | Funnel status + pareto kendala |
| `/onboarding` | Siapa yang belum tekan `/start` |
| `/setkuota <n>` | Kuota order per teknisi per hari |
| `/nonaktif <nama\|nik>` | Nonaktifkan teknisi, tampilkan sisa order yang menggantung |

## Yang divalidasi otomatis

**Nomor tiket** — `INC` + 8 angka (INSERA) atau `1-` + 6–10 karakter (DSC).
Nomor yang sudah dipakai order lain ditolak.

**SN baru** — empat lapis:
1. 16 karakter heksadesimal
2. Prefix cocok vendor: HUAWEI `48575443` · ZTE `5A544547` · FIBERHOME `46485454`
3. Tidak sama dengan `sn_old`
4. Belum pernah dipakai order lain (`UNIQUE` di kolom `sn_new`)

**Close** — ditolak kalau `sn_new` kosong atau salah satu foto belum ada.

## Perpindahan kepemilikan

Pemilik order = hasil join `orders.group_uid` → `assignment`, bukan kolom di
`orders`. Ganti pemilik = satu baris baru di `assignment`, baris lama
di-`aktif=FALSE`. Riwayat utuh.

`/assign` akan memberi peringatan kalau ada order di klaster itu yang sudah
melewati `REQ_TIKET` — tiketnya tetap tercatat atas nama perequest asli di
`tickets.requested_by`, karena di grup TSEL tiket itu terikat ke orang, bukan
ke sistem.

Untuk satu order tunggal tanpa memindahkan klaster: isi `orders.teknisi_override`.
Pintu darurat, bukan mekanisme rutin.

## Akuntansi produktivitas

`orders.closed_by` = yang menutup. `orders.req_tiket_by` = yang meminta tiket.
Kalau order pindah tangan di tengah jalan, dua kolom ini akan berbeda — itu
disengaja, supaya terlihat siapa yang mengerjakan bagian mana.

## Yang belum ada

- **Rekonsiliasi mingguan** terhadap export DPMIGNTE baru. Definisi "selesai"
  yang sahih adalah record hilang dari export berikutnya, bukan teknisi menekan
  Close. Perlu script terpisah.
- **Dashboard peta.** Sementara pakai `/beban` dan `/stagnan`.
- **Mirror ke Google Sheets** untuk Looker Studio.
- **Stok ONT.** Sengaja ditunda; kalau nanti dimasukkan, gate-nya di antara
  `CARING_OK` dan `REQ_TIKET`.
- `settings.distribusi_jam` sudah ada di tabel tapi jam distribusi masih
  hardcoded 07:30 di `bot.py`. Ubah di sana kalau perlu.

## Catatan pengujian

Kode sudah lolos pemeriksaan sintaks Python, tapi **belum pernah dijalankan
terhadap Postgres sungguhan** — tidak ada instance database di lingkungan tempat
ini dibuat. Uji dengan 1 teknisi dan 1 klaster dulu sebelum seed penuh 5.493 baris.
