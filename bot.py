import logging
import re
from datetime import datetime, date, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

import config
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("botont")

HEX16 = re.compile(r"^[0-9A-Fa-f]{16}$")
TIKET_INSERA = re.compile(r"^INC\d{8}$", re.I)
TIKET_DSC = re.compile(r"^1-[0-9A-Z]{6,10}$", re.I)


# ============================================================
# util
# ============================================================

def kb(rows):
    return InlineKeyboardMarkup(rows)


async def guard(update: Update):
    """Kembalikan record teknisi, atau None kalau tidak terdaftar."""
    uid = update.effective_user.id
    t = await db.get_teknisi(uid)
    if not t or not t["aktif"]:
        return None
    return t


def kartu(o) -> str:
    maps = f"https://maps.google.com/?q={o['lat']},{o['lon']}" if o["lat"] else "-"
    lines = [
        f"<b>{o['no_inet']}</b>",
        f"Status  : {config.LABEL.get(o['status'], o['status'])}",
        f"Klaster : {o['group_uid']}  ·  Zona {o['zona']}",
        f"Speed   : {o['speed_mb']} Mb",
        f"ONT lama: {o['type_old'] or '-'} ({o['vendor_old'] or '-'})",
        f"SN lama : <code>{o['sn_old'] or '-'}</code>",
        f"Lokasi  : {maps}",
    ]
    if o["flag"] and o["flag"] != "OK":
        lines.append(f"⚠ Flag  : {o['flag']}")
    if o["status"] == "KENDALA":
        lines.append(f"Kendala : {o['kode_kendala']} — {o['catatan_kendala'] or ''}")
        if o["followup_date"]:
            lines.append(f"Follow-up: {o['followup_date']}")
    return "\n".join(lines)


def aksi_untuk(status: str, no_inet: str):
    """Tombol yang relevan dengan status saat ini saja."""
    m = {
        "ASSIGNED": [("Caring OK", "caring"), ("Ada kendala", "kendala")],
        "KENDALA": [("Coba lagi — caring OK", "caring"), ("Update kendala", "kendala")],
        "CARING_OK": [("Sudah request tiket di grup", "reqtiket"), ("Ada kendala", "kendala")],
        "REQ_TIKET": [("Input nomor tiket", "tiket"), ("Ada kendala", "kendala")],
        "TIKET_OPEN": [("Input SN baru", "sn"), ("Ada kendala", "kendala")],
        "GANTI_OK": [("Sudah request config", "reqconfig")],
        "REQ_CONFIG": [("Config OK", "configok")],
        "CONFIG_OK": [("Close order", "close")],
    }
    rows = [[InlineKeyboardButton(t, callback_data=f"{a}|{no_inet}")]
            for t, a in m.get(status, [])]
    rows.append([InlineKeyboardButton("« Daftar order", callback_data="list|0")])
    return kb(rows)


# ============================================================
# onboarding
# ============================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ok = await db.onboard(u.id, u.username, u.first_name)
    if ok:
        t = await db.get_teknisi(u.id)
        await update.message.reply_text(
            f"Terdaftar: {t['nama']} ({t['nik']}).\n\n"
            "Ketik /order untuk melihat order hari ini.\n"
            "/bantuan untuk daftar perintah."
        )
    elif await db.is_admin(u.id):
        await update.message.reply_text("Mode admin aktif. /adminhelp untuk daftar perintah.")
    else:
        await update.message.reply_text(
            f"User ID Anda: <code>{u.id}</code>\n"
            "ID ini belum terdaftar. Kirim ke Officer RJW untuk didaftarkan.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_bantuan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/order — order yang harus dikerjakan hari ini\n"
        "/sisa — jumlah order tersisa\n"
        "/cari <no_inet> — buka satu order\n"
        "/batal — batalkan input yang sedang berjalan\n\n"
        "Alur: caring dulu → baru request tiket di grup TSEL → "
        "input nomor tiket di sini → ganti ONT + input SN baru → "
        "request config → close."
    )


async def cmd_batal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("pending", None)
    await update.message.reply_text("Input dibatalkan.")


# ============================================================
# daftar order
# ============================================================

async def kirim_daftar(target, uid: int, ctx):
    kuota = int(await db.get_setting("kuota_harian", "3"))
    rows = await db.antrian(uid, kuota)
    if not rows:
        return await target.reply_text("Tidak ada order aktif untuk Anda hari ini.")
    btn = []
    for r in rows:
        tag = config.LABEL.get(r["status"], r["status"])
        btn.append([InlineKeyboardButton(
            f"{r['no_inet']} · {tag}", callback_data=f"open|{r['no_inet']}")])
    total = await db.pool().fetchval(
        """SELECT COUNT(*) FROM v_order_owner
           WHERE teknisi_id=$1 AND status NOT IN ('CLOSED','BATAL')""", uid)
    await target.reply_text(
        f"Order hari ini ({len(rows)} dari {total} sisa):", reply_markup=kb(btn))


async def cmd_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = await guard(update)
    if not t:
        return await update.message.reply_text("Anda belum terdaftar. Ketik /start.")
    await kirim_daftar(update.message, t["teknisi_id"], ctx)


async def cmd_sisa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = await guard(update)
    if not t:
        return
    r = await db.pool().fetchrow(
        "SELECT sisa, kendala, tunggu_tiket, closed FROM v_beban WHERE teknisi_id=$1",
        t["teknisi_id"])
    await update.message.reply_text(
        f"Sisa: {r['sisa']}\nSelesai: {r['closed']}\n"
        f"Kendala: {r['kendala']}\nMenunggu tiket: {r['tunggu_tiket']}")


async def cmd_cari(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = await guard(update)
    if not t or not ctx.args:
        return await update.message.reply_text("Format: /cari <no_inet>")
    o = await db.get_order(ctx.args[0].strip())
    if not o:
        return await update.message.reply_text("Order tidak ditemukan.")
    if o["owner_id"] != t["teknisi_id"] and not await db.is_admin(t["teknisi_id"]):
        return await update.message.reply_text(
            f"Order ini milik {o['owner_nama'] or '-'}, bukan Anda.")
    await update.message.reply_text(kartu(o), parse_mode=ParseMode.HTML,
                                    reply_markup=aksi_untuk(o["status"], o["no_inet"]),
                                    disable_web_page_preview=True)


# ============================================================
# aksi per order
# ============================================================

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    aksi, _, arg = q.data.partition("|")
    uid = q.from_user.id

    if aksi == "list":
        return await kirim_daftar(q.message, uid, ctx)

    if aksi == "open":
        o = await db.get_order(arg)
        if not o:
            return await q.message.reply_text("Order tidak ditemukan.")
        return await q.message.reply_text(
            kartu(o), parse_mode=ParseMode.HTML,
            reply_markup=aksi_untuk(o["status"], o["no_inet"]),
            disable_web_page_preview=True)

    # kd membawa dua argumen: no_inet|kode
    kode_kendala = None
    if aksi == "kd":
        arg, _, kode_kendala = arg.partition("|")

    o = await db.get_order(arg)
    if not o:
        return await q.message.reply_text("Order tidak ditemukan.")
    if o["owner_id"] != uid and not await db.is_admin(uid):
        return await q.message.reply_text("Order ini bukan milik Anda.")

    # ---- caring OK ----
    if aksi == "caring":
        await db.transisi(arg, "CARING_OK", uid,
                          extra_sql=", caring_at=now(), kode_kendala=NULL, catatan_kendala=NULL, followup_date=NULL")
        link = await db.get_setting("link_grup_tsel", "")
        teks = (
            "Caring tercatat. Sekarang kirim request tiket di grup TSEL "
            "dengan format berikut — lengkapi nama & CP dari sumber lain:\n\n"
            f"<code>No Internet : {o['no_inet']}\n"
            "Nama Pelanggan : \n"
            "CP Pelanggan : \n"
            "Witel : BANDUNG\n"
            "STO : RJW\n"
            "Request : Tiket Symptom Z_PERMINTAAN_044 untuk penggantian ONT</code>\n\n"
            "Setelah terkirim, tekan tombol di bawah."
        )
        if link and link != "https://t.me/":
            teks += f"\n\nGrup: {link}"
        return await q.message.reply_text(
            teks, parse_mode=ParseMode.HTML,
            reply_markup=kb([[InlineKeyboardButton("Sudah saya kirim di grup",
                                                   callback_data=f"reqtiket|{arg}")]]))

    # ---- tandai sudah request tiket ----
    if aksi == "reqtiket":
        await db.transisi(arg, "REQ_TIKET", uid,
                          extra_sql=", req_tiket_at=now(), req_tiket_by=$3",
                          extra_args=(uid,))
        sla = await db.get_setting("sla_tiket_jam", "8")
        return await q.message.reply_text(
            f"Tercatat menunggu tiket. Begitu nomor tiket terbit di grup, "
            f"tekan tombol di bawah dan kirim nomornya.\n"
            f"Kalau lewat {sla} jam belum ada, sistem akan mengingatkan.",
            reply_markup=kb([[InlineKeyboardButton("Input nomor tiket",
                                                   callback_data=f"tiket|{arg}")]]))

    # ---- minta input teks ----
    if aksi == "tiket":
        ctx.user_data["pending"] = ("tiket", arg)
        return await q.message.reply_text(
            "Kirim nomor tiketnya.\n"
            "Format INSERA: INC12345678  ·  Format DSC: 1-A1B2C3D")

    if aksi == "sn":
        ctx.user_data["pending"] = ("sn", arg)
        return await q.message.reply_text(
            f"Kirim SN ONT baru (16 karakter).\nSN lama: <code>{o['sn_old'] or '-'}</code>",
            parse_mode=ParseMode.HTML)

    if aksi == "kendala":
        rows = await db.pool().fetch("SELECT kode,label FROM kendala_ref ORDER BY urutan")
        return await q.message.reply_text(
            "Pilih kendala:",
            reply_markup=kb([[InlineKeyboardButton(f"{r['kode']} {r['label']}",
                                                   callback_data=f"kd|{arg}|{r['kode']}")]
                             for r in rows]))

    if aksi == "kd":
        ref = await db.pool().fetchrow("SELECT * FROM kendala_ref WHERE kode=$1", kode_kendala)
        if ref is None:
            return await q.message.reply_text("Kode kendala tidak dikenali.")
        if ref["perlu_catatan"] or ref["perlu_tanggal"]:
            ctx.user_data["pending"] = ("kendala", arg, kode_kendala, ref["perlu_tanggal"])
            minta = ("tanggal follow-up (format YYYY-MM-DD)" if ref["perlu_tanggal"]
                     else "keterangan singkat")
            return await q.message.reply_text(f"{ref['label']}. Kirim {minta}.")
        return await simpan_kendala(q.message, arg, kode_kendala, uid, None, None)

    if aksi == "reqconfig":
        await db.transisi(arg, "REQ_CONFIG", uid, extra_sql=", req_config_at=now()")
        return await q.message.reply_text(
            "Tercatat menunggu config helpdesk.",
            reply_markup=kb([[InlineKeyboardButton("Config sudah OK",
                                                   callback_data=f"configok|{arg}")]]))

    if aksi == "configok":
        await db.transisi(arg, "CONFIG_OK", uid, extra_sql=", config_at=now()")
        return await q.message.reply_text(
            "Config OK. Pastikan layanan sudah normal sebelum close.",
            reply_markup=kb([[InlineKeyboardButton("Close order",
                                                   callback_data=f"close|{arg}")]]))

    if aksi == "close":
        if not o["sn_new"]:
            return await q.message.reply_text("Belum ada SN baru. Input SN dulu.")
        if not (o["foto_label_sn"] and o["foto_terpasang"]):
            return await q.message.reply_text(
                "Foto belum lengkap. Butuh foto label SN dan foto perangkat terpasang.")
        await db.transisi(arg, "CLOSED", uid,
                          extra_sql=", closed_at=now(), closed_by=$3", extra_args=(uid,))
        await db.pool().execute(
            "UPDATE tickets SET closed_at=now() WHERE no_inet=$1", arg)
        return await q.message.reply_text(
            f"Order {arg} selesai. Terima kasih.",
            reply_markup=kb([[InlineKeyboardButton("Order berikutnya",
                                                   callback_data="list|0")]]))


async def simpan_kendala(msg, no_inet, kode, uid, catatan, tgl):
    await db.transisi(
        no_inet, "KENDALA", uid, kode_kendala=kode, catatan=catatan,
        extra_sql=(", kode_kendala=$3, catatan_kendala=$4, followup_date=$5, "
                   "percobaan=percobaan+1"),
        extra_args=(kode, catatan, tgl))
    lanjut = f" Follow-up {tgl}." if tgl else " Akan muncul lagi di antrian besok."
    await msg.reply_text(f"Kendala {kode} tercatat.{lanjut}",
                         reply_markup=kb([[InlineKeyboardButton("« Daftar order",
                                                                callback_data="list|0")]]))


# ============================================================
# input teks & foto
# ============================================================

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ctx.user_data.get("pending")
    if not p:
        return
    uid = update.effective_user.id
    teks = update.message.text.strip()
    jenis = p[0]

    # ---- nomor tiket ----
    if jenis == "tiket":
        no_inet = p[1]
        val = teks.upper().replace(" ", "")
        if TIKET_INSERA.match(val):
            tipe = "INSERA"
        elif TIKET_DSC.match(val):
            tipe = "DSC"
        else:
            return await update.message.reply_text(
                "Format tidak dikenali. INSERA: INC + 8 angka. DSC: 1- diikuti 6-10 karakter.\n"
                "Kirim ulang, atau /batal.")
        dupe = await db.pool().fetchval(
            "SELECT no_inet FROM tickets WHERE no_tiket=$1 AND no_inet<>$2", val, no_inet)
        if dupe:
            return await update.message.reply_text(
                f"Nomor tiket ini sudah dipakai order {dupe}. Cek kembali.")
        o = await db.get_order(no_inet)
        await db.pool().execute(
            """INSERT INTO tickets(no_inet,no_tiket,jenis,requested_by,requested_at)
               VALUES($1,$2,$3,$4,$5)
               ON CONFLICT (no_inet) DO UPDATE SET
                 no_tiket=EXCLUDED.no_tiket, jenis=EXCLUDED.jenis, issued_at=now()""",
            no_inet, val, tipe, uid, o["req_tiket_at"])
        await db.transisi(no_inet, "TIKET_OPEN", uid, catatan=f"{tipe} {val}",
                          extra_sql=", tiket_at=now()")
        ctx.user_data.pop("pending")
        return await update.message.reply_text(
            f"Tiket {tipe} {val} tercatat. Silakan kerjakan penggantian ONT.",
            reply_markup=kb([[InlineKeyboardButton("Input SN baru",
                                                   callback_data=f"sn|{no_inet}")]]))

    # ---- SN baru ----
    if jenis == "sn":
        no_inet = p[1]
        sn = teks.upper().replace(" ", "").replace(":", "")
        o = await db.get_order(no_inet)
        if not HEX16.match(sn):
            return await update.message.reply_text(
                "SN harus 16 karakter heksadesimal (0-9, A-F). Kirim ulang atau /batal.")
        vendor = next((v for v, p8 in config.SN_PREFIX.items() if sn.startswith(p8)), None)
        if not vendor:
            return await update.message.reply_text(
                "Prefix SN tidak dikenali. HUAWEI 48575443 · ZTE 5A544547 · "
                "FIBERHOME 46485454.\nCek ulang label perangkat.")
        if o["sn_old"] and sn == o["sn_old"].upper():
            return await update.message.reply_text(
                "SN ini sama dengan SN lama. Yang diminta SN perangkat pengganti.")
        dipakai = await db.sn_dipakai(sn, no_inet)
        if dipakai:
            return await update.message.reply_text(
                f"SN ini sudah tercatat di order {dipakai}. Cek ulang perangkat.")
        await db.transisi(no_inet, "GANTI_OK", uid, sn_new=sn,
                          extra_sql=", sn_new=$3, vendor_new=$4, ganti_at=now()",
                          extra_args=(sn, vendor))
        ctx.user_data["pending"] = ("foto_label", no_inet)
        return await update.message.reply_text(
            f"SN {sn} ({vendor}) tercatat.\nKirim foto label SN perangkat baru.")

    # ---- kendala dengan keterangan / tanggal ----
    if jenis == "kendala":
        _, no_inet, kode, perlu_tgl = p
        tgl = None
        catatan = teks
        if perlu_tgl:
            try:
                tgl = datetime.strptime(teks, "%Y-%m-%d").date()
            except ValueError:
                return await update.message.reply_text("Format tanggal: YYYY-MM-DD. Kirim ulang.")
            if tgl <= date.today():
                return await update.message.reply_text("Tanggal harus setelah hari ini.")
            catatan = None
        ctx.user_data.pop("pending")
        return await simpan_kendala(update.message, no_inet, kode, uid, catatan, tgl)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    p = ctx.user_data.get("pending")
    if not p or p[0] not in ("foto_label", "foto_terpasang"):
        return
    fid = update.message.photo[-1].file_id
    no_inet = p[1]
    if p[0] == "foto_label":
        await db.pool().execute(
            "UPDATE orders SET foto_label_sn=$2, updated_at=now() WHERE no_inet=$1",
            no_inet, fid)
        ctx.user_data["pending"] = ("foto_terpasang", no_inet)
        return await update.message.reply_text("Foto label tersimpan. Kirim foto perangkat terpasang.")
    await db.pool().execute(
        "UPDATE orders SET foto_terpasang=$2, updated_at=now() WHERE no_inet=$1",
        no_inet, fid)
    ctx.user_data.pop("pending")
    await update.message.reply_text(
        "Foto lengkap. Lanjut request config ke helpdesk.",
        reply_markup=kb([[InlineKeyboardButton("Sudah request config",
                                               callback_data=f"reqconfig|{no_inet}")]]))


# ============================================================
# admin
# ============================================================

def admin_only(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not await db.is_admin(update.effective_user.id):
            return
        return await fn(update, ctx)
    return wrap


@admin_only
async def cmd_adminhelp(update: Update, ctx):
    await update.message.reply_text(
        "/beban — sisa order per teknisi\n"
        "/assign <group_uid> <nama|nik> — pindah satu klaster\n"
        "/pindahzona <zona> <nama|nik> — pindah seluruh zona\n"
        "/stagnan — klaster tanpa progres\n"
        "/rekap — funnel status + pareto kendala\n"
        "/tunggutiket — order yang mandek menunggu tiket\n"
        "/onboarding — siapa yang belum tekan /start\n"
        "/setkuota <n> — kuota order per teknisi per hari\n"
        "/nonaktif <nama|nik> — nonaktifkan teknisi")


@admin_only
async def cmd_beban(update: Update, ctx):
    rows = await db.beban()
    out = ["<b>Sisa order per teknisi</b>", "<code>sisa kdl tiket  nama</code>"]
    for r in rows[:70]:
        mark = "" if r["siap_dm"] else " ⚠"
        out.append(f"<code>{r['sisa']:>4} {r['kendala']:>3} {r['tunggu_tiket']:>5}</code>  {r['nama']}{mark}")
    out.append("\n⚠ = belum tekan /start, bot belum bisa kirim DM")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_assign(update: Update, ctx):
    if len(ctx.args) < 2:
        return await update.message.reply_text("Format: /assign <group_uid> <nama|nik>")
    gu, q = ctx.args[0], " ".join(ctx.args[1:])
    cand = await db.cari_teknisi(q)
    if len(cand) != 1:
        pesan = ("Teknisi tidak ditemukan." if not cand
                 else "Nama tidak unik: " + ", ".join(c["nama"] for c in cand))
        return await update.message.reply_text(pesan)
    blok = await db.pool().fetch(
        """SELECT no_inet,status FROM orders WHERE group_uid=$1
           AND status NOT IN ('NEW','ASSIGNED','CARING_OK','KENDALA','CLOSED','BATAL')""", gu)
    await db.set_assignment(gu, cand[0]["teknisi_id"], update.effective_user.id)
    msg = f"Klaster {gu} → {cand[0]['nama']}."
    if blok:
        msg += (f"\n\n⚠ {len(blok)} order sudah melewati tahap request tiket dan tetap "
                "tercatat atas nama perequest asli: "
                + ", ".join(f"{b['no_inet']}({b['status']})" for b in blok[:5]))
    await update.message.reply_text(msg)


@admin_only
async def cmd_pindahzona(update: Update, ctx):
    if len(ctx.args) < 2:
        return await update.message.reply_text("Format: /pindahzona <zona> <nama|nik>")
    zona, q = ctx.args[0].upper(), " ".join(ctx.args[1:])
    cand = await db.cari_teknisi(q)
    if len(cand) != 1:
        return await update.message.reply_text("Teknisi tidak unik/ketemu.")
    kl = await db.klaster_zona(zona)
    for k in kl:
        await db.set_assignment(k["group_uid"], cand[0]["teknisi_id"],
                                update.effective_user.id, catatan=f"pindah zona {zona}")
    await update.message.reply_text(
        f"{len(kl)} klaster di zona {zona} → {cand[0]['nama']}.")


@admin_only
async def cmd_stagnan(update: Update, ctx):
    hari = int(await db.get_setting("stagnan_hari", "7"))
    rows = await db.stagnan(hari)
    if not rows:
        return await update.message.reply_text(f"Tidak ada klaster diam ≥ {hari} hari.")
    out = [f"<b>Klaster tanpa progres ≥ {hari} hari</b>"]
    for r in rows:
        out.append(f"{r['group_uid']} · {r['nama'] or '-'} · sisa {r['sisa']} · diam {r['diam_hari']}h")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_rekap(update: Update, ctx):
    f = await db.funnel()
    p = await db.pareto_kendala()
    out = ["<b>Funnel status</b>"]
    for r in f:
        out.append(f"{config.LABEL.get(r['status'], r['status'])}: {r['n']}")
    if p:
        out.append("\n<b>Pareto kendala</b>")
        for r in p:
            out.append(f"{r['kode_kendala']} {r['label']}: {r['n']}")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_tunggutiket(update: Update, ctx):
    rows = await db.aging("REQ_TIKET", "req_tiket_at")
    if not rows:
        return await update.message.reply_text("Tidak ada order menunggu tiket.")
    out = [f"<b>Menunggu tiket ({len(rows)} terlama)</b>"]
    for r in rows[:25]:
        out.append(f"{r['no_inet']} · {r['nama'] or '-'} · {r['jam']:.0f} jam")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_onboarding(update: Update, ctx):
    rows = await db.belum_onboarding()
    if not rows:
        return await update.message.reply_text("Semua teknisi aktif sudah onboarding.")
    out = [f"<b>Belum tekan /start ({len(rows)})</b>"]
    for r in rows:
        out.append(f"{r['nik']} · {r['nama']} · <code>{r['teknisi_id']}</code>")
    await update.message.reply_text("\n".join(out), parse_mode=ParseMode.HTML)


@admin_only
async def cmd_setkuota(update: Update, ctx):
    if not ctx.args or not ctx.args[0].isdigit():
        k = await db.get_setting("kuota_harian", "3")
        return await update.message.reply_text(f"Kuota saat ini: {k}. Ubah: /setkuota <n>")
    await db.set_setting("kuota_harian", ctx.args[0], update.effective_user.id)
    await update.message.reply_text(f"Kuota harian → {ctx.args[0]} order per teknisi.")


@admin_only
async def cmd_nonaktif(update: Update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Format: /nonaktif <nama|nik>")
    cand = await db.cari_teknisi(" ".join(ctx.args))
    if len(cand) != 1:
        return await update.message.reply_text("Teknisi tidak unik/ketemu.")
    await db.pool().execute("UPDATE teknisi SET aktif=FALSE WHERE teknisi_id=$1",
                            cand[0]["teknisi_id"])
    sisa = await db.pool().fetchval(
        """SELECT COUNT(*) FROM v_order_owner
           WHERE teknisi_id=$1 AND status NOT IN ('CLOSED','BATAL')""",
        cand[0]["teknisi_id"])
    await update.message.reply_text(
        f"{cand[0]['nama']} dinonaktifkan. {sisa} order masih atas namanya — "
        "pindahkan dengan /pindahzona atau /assign.")


# ============================================================
# job harian
# ============================================================

async def job_distribusi(ctx: ContextTypes.DEFAULT_TYPE):
    """Push antrian pagi ke tiap teknisi yang sudah onboarding."""
    kuota = int(await db.get_setting("kuota_harian", "3"))
    rows = await db.pool().fetch(
        "SELECT teknisi_id, nama FROM teknisi WHERE aktif AND onboarded_at IS NOT NULL")
    for t in rows:
        ordr = await db.antrian(t["teknisi_id"], kuota)
        if not ordr:
            continue
        btn = [[InlineKeyboardButton(f"{r['no_inet']} · {config.LABEL.get(r['status'], r['status'])}",
                                     callback_data=f"open|{r['no_inet']}")] for r in ordr]
        try:
            await ctx.bot.send_message(
                t["teknisi_id"],
                f"Selamat pagi {t['nama'].title()}. Order hari ini ({len(ordr)}):",
                reply_markup=kb(btn))
            await db.pool().execute(
                "UPDATE orders SET dikirim_at=now() WHERE no_inet=ANY($1::text[])",
                [r["no_inet"] for r in ordr])
        except Exception as e:
            log.warning("gagal kirim ke %s: %s", t["teknisi_id"], e)


async def job_sla(ctx: ContextTypes.DEFAULT_TYPE):
    """Ingatkan order yang mandek menunggu tiket atau config."""
    sla_t = int(await db.get_setting("sla_tiket_jam", "8"))
    rows = await db.pool().fetch(
        """SELECT o.no_inet, v.teknisi_id,
                  EXTRACT(EPOCH FROM now()-o.req_tiket_at)/3600 AS jam
           FROM orders o JOIN v_order_owner v ON v.no_inet=o.no_inet
           WHERE o.status='REQ_TIKET' AND o.req_tiket_at < now() - ($1||' hours')::interval""",
        str(sla_t))
    for r in rows:
        try:
            await ctx.bot.send_message(
                r["teknisi_id"],
                f"Order {r['no_inet']} sudah {r['jam']:.0f} jam menunggu tiket. "
                "Kalau tiket sudah terbit, input nomornya.",
                reply_markup=kb([[InlineKeyboardButton(
                    "Input nomor tiket", callback_data=f"tiket|{r['no_inet']}")]]))
        except Exception as e:
            log.warning("sla dm gagal %s: %s", r["teknisi_id"], e)


# ============================================================

async def post_init(app: Application):
    await db.init()
    log.info("db siap")


async def post_shutdown(app: Application):
    await db.close()


def main():
    app = (Application.builder()
           .token(config.BOT_TOKEN)
           .post_init(post_init)
           .post_shutdown(post_shutdown)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("bantuan", cmd_bantuan))
    app.add_handler(CommandHandler("batal", cmd_batal))
    app.add_handler(CommandHandler("order", cmd_order))
    app.add_handler(CommandHandler("sisa", cmd_sisa))
    app.add_handler(CommandHandler("cari", cmd_cari))

    app.add_handler(CommandHandler("adminhelp", cmd_adminhelp))
    app.add_handler(CommandHandler("beban", cmd_beban))
    app.add_handler(CommandHandler("assign", cmd_assign))
    app.add_handler(CommandHandler("pindahzona", cmd_pindahzona))
    app.add_handler(CommandHandler("stagnan", cmd_stagnan))
    app.add_handler(CommandHandler("rekap", cmd_rekap))
    app.add_handler(CommandHandler("tunggutiket", cmd_tunggutiket))
    app.add_handler(CommandHandler("onboarding", cmd_onboarding))
    app.add_handler(CommandHandler("setkuota", cmd_setkuota))
    app.add_handler(CommandHandler("nonaktif", cmd_nonaktif))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    jq = app.job_queue
    import zoneinfo
    tz = zoneinfo.ZoneInfo(config.TZ)
    jq.run_daily(job_distribusi, time=datetime.strptime("07:30", "%H:%M").time().replace(tzinfo=tz))
    jq.run_repeating(job_sla, interval=timedelta(hours=2), first=timedelta(minutes=5))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
