"""
Konversi fonem (IPA) hasil model bookbot/sherpa-onnx-pruned-transducer-
stateless7-streaming-id menjadi tulisan Bahasa Indonesia biasa.

Model ini TIDAK memprediksi kata langsung, tapi urutan fonem (mis.
['p','ə','r','b','u','a','t','a','n'] untuk kata "perbuatan"). Untungnya,
ejaan Bahasa Indonesia sangat fonemis (hampir 1-banding-1 antara bunyi dan
huruf, beda jauh dari Bahasa Inggris) -- jadi peta konversi sederhana
seperti di bawah ini bisa memberi hasil yang cukup terbaca tanpa perlu
kamus/lexicon besar.

KETERBATASAN YANG PERLU DIKETAHUI (bukan bug, tapi sifat bahasa):
  - 'e' dan 'ə' (schwa) SAMA-SAMA ditulis "e" dalam ejaan Indonesia baku
    (mis. "sore" vs "besar") -- ini ambigu bahkan buat penutur asli saat
    menulis biasa, jadi wajar hasil konversi ini juga tidak bisa
    membedakan keduanya.
  - Glottal stop (ʔ) di akhir suku kata pada ejaan Indonesia baku
    biasanya ditulis "k" (mis. "tidak" /tidaʔ/) -- itu asumsi default di
    sini; kadang meleset untuk kasus di tengah kata.
  - Ini APROKSIMASI untuk preview instan, BUKAN pengganti transkrip akurat
    dari faster-whisper (yang tetap jadi sumber kebenaran untuk KB search
    & penyimpanan DB).
"""

# Peta fonem IPA (persis sesuai tokens.txt model) -> ejaan Indonesia.
# Diurutkan tidak masalah di dict, tapi lihat _PHONEME_SYMBOLS_BY_LENGTH di
# bawah untuk urutan pencocokan (multi-karakter dicek duluan).
_PHONEME_TO_GRAPHEME = {
    "ɡ": "g",
    "o": "o",
    "d": "d",
    "ʃ": "sy",
    "v": "v",
    "t": "t",
    "x": "kh",
    "r": "r",
    "ʔ": "k",   # glottal stop akhir suku kata -> "k" (lihat catatan di atas)
    "b": "b",
    "s": "s",
    "p": "p",
    "i": "i",
    "dʒ": "j",
    "ə": "e",   # schwa -> "e" (ambigu dengan /e/, lihat catatan modul)
    "z": "z",
    "f": "f",
    "n": "n",
    "m": "m",
    "ɲ": "ny",
    "tʃ": "c",
    "ŋ": "ng",
    "k": "k",
    "j": "y",
    "l": "l",
    "h": "h",
    "w": "w",
    "a": "a",
    "u": "u",
    "e": "e",
}

WORD_BOUNDARY = "|"
_SPECIAL_TOKENS = {"<eps>", "<UNK>", "#0", ""}

# Simbol multi-karakter HARUS dicek duluan sebelum yang 1-karakter waktu
# tokenizing string yang mungkin datang tanpa spasi antar-fonem (mis.
# "dʒalan" -- kalau dicek 'd' duluan, salah segmentasi jadi 'd'+'ʒ'... tapi
# 'ʒ' sendiri bukan simbol valid di model ini, jadi harus cocokkan "dʒ"
# dulu sebagai satu unit).
_MULTI_CHAR_PHONEMES = sorted(
    (p for p in _PHONEME_TO_GRAPHEME if len(p) > 1), key=len, reverse=True
)


def _tokenize_word_chunk(chunk: str) -> list[str]:
    """
    Pecah satu potongan teks (tanpa word-boundary '|') jadi daftar token
    fonem individual. Robust terhadap dua kemungkinan format output
    sherpa-onnx: fonem dipisah spasi ("d ʒ a l a n") ATAU digabung tanpa
    spasi ("dʒalan") -- caranya, buang dulu semua spasi, baru segmentasi
    ulang pakai pencocokan simbol terpanjang (greedy longest-match).
    """
    s = chunk.replace(" ", "")
    tokens = []
    i = 0
    while i < len(s):
        matched = None
        for sym in _MULTI_CHAR_PHONEMES:
            if s.startswith(sym, i):
                matched = sym
                break
        if matched is None:
            matched = s[i]  # fallback: 1 karakter apa adanya
        tokens.append(matched)
        i += len(matched)
    return tokens


def phonemes_to_indonesian_text(raw: str) -> str:
    """
    Input: string mentah dari sherpa_onnx OnlineRecognizer.get_result()
    (urutan token fonem + '|' sebagai pemisah kata, format spasi bisa
    bervariasi tergantung versi/decoding).
    Output: perkiraan tulisan Bahasa Indonesia yang bisa dibaca.
    """
    if not raw:
        return ""

    words = []
    for chunk in raw.split(WORD_BOUNDARY):
        chunk = chunk.strip()
        if not chunk or chunk in _SPECIAL_TOKENS:
            continue
        letters = []
        for tok in _tokenize_word_chunk(chunk):
            if tok in _SPECIAL_TOKENS:
                continue
            letters.append(_PHONEME_TO_GRAPHEME.get(tok, tok))  # fallback: tampilkan apa adanya kalau simbol tak dikenal
        word = "".join(letters)
        if word:
            words.append(word)

    return " ".join(words)
