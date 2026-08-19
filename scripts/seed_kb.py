"""
Isi database dengan beberapa artikel KB contoh (Bahasa Indonesia).
Jalankan: python -m scripts.seed_kb
"""
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db  # noqa: E402

SAMPLES = [
    {
        "title": "Cara reset password akun",
        "content": (
            "Untuk reset password, arahkan pelanggan ke halaman login lalu klik "
            "'Lupa Password'. Link reset akan dikirim ke email terdaftar dan "
            "berlaku selama 30 menit. Jika email tidak masuk, minta pelanggan "
            "cek folder spam atau verifikasi email yang terdaftar di sistem."
        ),
        "tags": ["password", "lupa password", "reset", "login", "akun"],
        "category": "akun",
    },
    {
        "title": "Tagihan belum masuk / belum terupdate",
        "content": (
            "Jika pelanggan komplain tagihan bulanan belum muncul, cek status "
            "billing cycle di sistem. Tagihan biasanya terbit H+1 setelah akhir "
            "periode. Jika sudah lewat H+2 dan belum muncul, eskalasi ke tim billing "
            "dengan menyertakan nomor pelanggan dan periode tagihan."
        ),
        "tags": ["tagihan", "billing", "belum masuk", "invoice"],
        "category": "billing",
    },
    {
        "title": "Cara mengajukan pembatalan langganan",
        "content": (
            "Pembatalan langganan bisa diajukan lewat menu Pengaturan > Langganan > "
            "Batalkan Langganan. Informasikan ke pelanggan bahwa akses tetap aktif "
            "hingga akhir periode yang sudah dibayar. Tidak ada refund prorata "
            "kecuali disebutkan lain dalam kebijakan promo yang diikuti pelanggan."
        ),
        "tags": ["batal", "cancel", "berhenti langganan", "unsubscribe"],
        "category": "billing",
    },
    {
        "title": "Koneksi/aplikasi tidak bisa diakses (troubleshooting dasar)",
        "content": (
            "Langkah dasar troubleshooting: minta pelanggan restart aplikasi, cek "
            "koneksi internet, dan update ke versi aplikasi terbaru. Jika masih "
            "gagal, cek status sistem di halaman status internal untuk memastikan "
            "tidak ada outage yang sedang berlangsung."
        ),
        "tags": ["error", "tidak bisa masuk", "aplikasi error", "troubleshooting"],
        "category": "teknis",
    },
    {
        "title": "Komplain layanan lambat / pengiriman terlambat",
        "content": (
            "Untuk komplain keterlambatan, cek nomor resi/nomor transaksi di sistem "
            "tracking. Sampaikan estimasi terbaru ke pelanggan dan tawarkan kompensasi "
            "sesuai kebijakan yang berlaku (mis. voucher) jika keterlambatan melewati SLA."
        ),
        "tags": ["komplain", "terlambat", "pengiriman", "delay"],
        "category": "komplain",
    },
]


def main():
    db.init_db()
    existing = {a["title"] for a in db.kb_list()}
    created = 0
    for sample in SAMPLES:
        if sample["title"] in existing:
            continue
        db.kb_create(**sample)
        created += 1
    print(f"Selesai. {created} artikel baru ditambahkan (total sekarang: {len(db.kb_list())}).")


if __name__ == "__main__":
    main()
