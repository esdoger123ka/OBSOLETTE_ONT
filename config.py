import os

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

# Timezone operasional (Asia/Jakarta = WIB)
TZ = os.getenv("TZ", "Asia/Jakarta")

# user_id admin awal, dipisah koma. Setelah bot jalan, kelola lewat tabel admin_user.
BOOTSTRAP_ADMINS = [
    int(x) for x in os.getenv("BOOTSTRAP_ADMINS", "").replace(" ", "").split(",") if x
]

# Prefix serial number per vendor (8 hex pertama).
SN_PREFIX = {
    "HUAWEI": "48575443",
    "ZTE": "5A544547",
    "FIBERHOME": "46485454",
}

# Urutan status yang sah. Index dipakai untuk mencegah mundur tanpa sengaja.
ALUR = [
    "NEW",
    "ASSIGNED",
    "CARING_OK",
    "REQ_TIKET",
    "TIKET_OPEN",
    "GANTI_OK",
    "REQ_CONFIG",
    "CONFIG_OK",
    "CLOSED",
]

LABEL = {
    "NEW": "Belum diassign",
    "ASSIGNED": "Belum dicaring",
    "CARING_OK": "Caring OK, belum request tiket",
    "KENDALA": "Kendala",
    "REQ_TIKET": "Menunggu tiket terbit",
    "TIKET_OPEN": "Tiket terbit, belum dikerjakan",
    "GANTI_OK": "ONT terpasang, belum request config",
    "REQ_CONFIG": "Menunggu config helpdesk",
    "CONFIG_OK": "Config OK, belum close",
    "CLOSED": "Selesai",
    "BATAL": "Dibatalkan",
}

# Status yang boleh dipindahkan tanpa peringatan / dengan peringatan / diblokir lunak
PINDAH_BEBAS = {"NEW", "ASSIGNED"}
PINDAH_WARNING = {"CARING_OK", "KENDALA"}
# selebihnya perlu alasan tertulis
