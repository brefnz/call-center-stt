/**
 * kb-sidebar-widget.js
 *
 * Widget sidebar kanan untuk halaman ticketing CRM Data Kelola -- nampilin
 * saran Knowledge Base secara real-time mengikuti obrolan agent & customer.
 *
 * ALUR:
 *   1. Begitu halaman ticketing kebuka (biasanya sebelum call dijawab),
 *      widget mulai POLLING ke backend nanya "extension agent ini lagi di
 *      panggilan yang mana" -- karena call_id baru ada SETELAH call dijawab.
 *   2. Begitu call_id ketemu, widget connect WebSocket dan mulai render
 *      transkrip + saran KB yang masuk secara live.
 *   3. Begitu panggilan selesai, BACKEND yang menutup WebSocket ini
 *      (lihat app/ari_client.py + app/ws_manager.py: ws_manager.close_call
 *      dipanggil saat StasisEnd/ChannelDestroyed) -- widget cukup dengar
 *      event `onclose` dan langsung balik ke mode polling, tanpa perlu
 *      nebak-nebak/timeout di sisi sini.
 *
 * FIX (revisi ini):
 *   - Sebelumnya onclose/onerror TIDAK nge-log kenapa WebSocket ditutup
 *     (event.code / event.reason), jadi kalau widget reconnect terus tidak
 *     ada cara tahu dari console apakah itu backend yang sengaja nutup
 *     (call selesai, kode 1000) atau koneksi gagal dari awal (mis. mixed
 *     content http/https, salah host, port keblokir firewall -- semua ini
 *     biasanya muncul sebagai kode 1006 "abnormal closure").
 *   - Sebelumnya reconnect ke polling terjadi TANPA jeda sama sekali kalau
 *     WebSocket gagal connect/ke-close instan -- kalau penyebabnya persisten
 *     (bukan sekadar "call selesai normal"), ini jadi loop rapat tanpa henti
 *     (connect -> gagal instan -> poll -> connect -> gagal instan -> ...).
 *     Sekarang dibedakan: kalau WS sempat OPEN dulu (berarti call memang
 *     baru selesai/putus di tengah), langsung poll lagi seperti biasa.
 *     Kalau WS GAGAL connect dari awal (never opened), kasih jeda +
 *     exponential backoff supaya tidak flood & supaya ada waktu untuk
 *     ketahuan dari console/network tab.
 *   - Deteksi otomatis mixed-content (halaman https tapi backendBaseUrl
 *     http) dan tampilkan warning yang jelas di console + status bar,
 *     karena ini kandidat penyebab paling umum untuk reconnect loop tanpa
 *     pesan error yang jelas.
 *
 * CARA PAKAI (taruh di halaman ticketing CRM):
 *
 *   <div id="kb-sidebar"></div>
 *   <script src="kb-sidebar-widget.js"></script>
 *   <script>
 *     KbSidebarWidget.init({
 *       containerId: "kb-sidebar",
 *       backendBaseUrl: "http://100.126.177.23:8000",  // ganti ke IP/host production nanti
 *       agentExtension: "202",  // ambil dari session/context CRM yang sedang login
 *     });
 *   </script>
 *
 * `agentExtension` HARUS diisi sesuai extension agent yang sedang login di
 * CRM saat itu -- kalau CRM sudah tahu nomor extension agent dari sesi
 * login, tinggal oper ke sini. Kalau belum ada, tanya ke tim yang pegang
 * bagian login/session CRM di mana nilai itu tersimpan.
 */
(function (global) {
  "use strict";

  const POLL_INTERVAL_MS = 1500;
  const WS_FAIL_BACKOFF_START_MS = 1000;
  const WS_FAIL_BACKOFF_MAX_MS = 15000;

  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  class KbSidebarWidget {
    constructor(opts) {
      this.backendBaseUrl = opts.backendBaseUrl.replace(/\/$/, "");
      this.agentExtension = opts.agentExtension;
      this.container = document.getElementById(opts.containerId);
      if (!this.container) {
        console.error("[KbSidebarWidget] containerId tidak ditemukan:", opts.containerId);
        return;
      }

      this._pollTimer = null;
      this._wsFailBackoffTimer = null;
      this._wsFailBackoffMs = WS_FAIL_BACKOFF_START_MS;
      this._ws = null;
      this._currentCallId = null;
      this._stopped = false;
      this._interimLineEls = {};

      this._warnIfMixedContent();
      this._buildDom();
      this._startPolling();
    }

    _warnIfMixedContent() {
      // Halaman https:// yang connect ke ws:// (bukan wss://) akan
      // di-block browser secara DIAM-DIAM (mixed content) -- WebSocket
      // gagal connect tanpa pesan error yang jelas, onopen tidak pernah
      // kepanggil, tapi onclose/onerror langsung kepanggil. Efeknya persis
      // seperti "reconnecting terus" tanpa sebab yang kelihatan. Ini
      // kandidat paling umum kalau backendBaseUrl masih http:// padahal
      // CRM-nya sudah dibuka lewat https://.
      const pageIsHttps = global.location && global.location.protocol === "https:";
      const backendIsHttp = /^http:\/\//i.test(this.backendBaseUrl);
      if (pageIsHttps && backendIsHttp) {
        console.error(
          "[KbSidebarWidget] MIXED CONTENT WARNING: halaman ini dibuka lewat HTTPS " +
            "tapi backendBaseUrl masih HTTP (" + this.backendBaseUrl + "). " +
            "Browser akan MEMBLOKIR koneksi WebSocket ws:// dari halaman https:// " +
            "secara diam-diam -- ini penyebab paling umum untuk reconnect loop " +
            "tanpa error yang jelas. Backend HARUS diakses lewat HTTPS/WSS " +
            "(pasang reverse proxy TLS mis. nginx/caddy di depan uvicorn), " +
            "lalu ganti backendBaseUrl ke https://..."
        );
        this._mixedContentSuspected = true;
      }
    }

    _buildDom() {
      this.container.innerHTML = "";
      this.container.classList.add("kb-sidebar-widget");

      this.statusEl = el("div", "kb-sw-status", "Menunggu panggilan...");
      this.transcriptEl = el("div", "kb-sw-transcript");
      this.suggestionsEl = el("div", "kb-sw-suggestions");

      this.container.appendChild(this.statusEl);
      this.container.appendChild(el("div", "kb-sw-section-title", "Transkrip Live"));
      this.container.appendChild(this.transcriptEl);
      this.container.appendChild(el("div", "kb-sw-section-title", "Saran Knowledge Base"));
      this.container.appendChild(this.suggestionsEl);

      this._injectDefaultStyles();
    }

    _injectDefaultStyles() {
      if (document.getElementById("kb-sw-styles")) return; // jangan dobel
      const style = document.createElement("style");
      style.id = "kb-sw-styles";
      style.textContent = `
        .kb-sidebar-widget { font-family: system-ui, sans-serif; font-size: 13px; padding: 12px; }
        .kb-sw-status { padding: 6px 10px; border-radius: 6px; background: #f0f0f0; color: #555; margin-bottom: 12px; }
        .kb-sw-status.live { background: #e6f4ea; color: #1e7e34; }
        .kb-sw-status.error { background: #fde8e8; color: #b91c1c; }
        .kb-sw-section-title { font-weight: 600; margin: 14px 0 6px; color: #333; }
        .kb-sw-transcript { max-height: 220px; overflow-y: auto; border: 1px solid #eee; border-radius: 6px; padding: 8px; }
        .kb-sw-line { margin-bottom: 6px; line-height: 1.4; }
        .kb-sw-line .spk { font-weight: 600; margin-right: 4px; }
        .kb-sw-line.customer .spk { color: #b45309; }
        .kb-sw-line.agent .spk { color: #1d4ed8; }
        .kb-sw-line.interim { color: #999; font-style: italic; }
        .kb-sw-suggestion-card { border: 1px solid #ddd; border-radius: 8px; padding: 10px; margin-bottom: 8px; background: #fff; }
        .kb-sw-suggestion-card .title { font-weight: 600; margin-bottom: 4px; }
        .kb-sw-suggestion-card .content { color: #555; font-size: 12px; line-height: 1.4; }
        .kb-sw-empty { color: #999; font-style: italic; }
      `;
      document.head.appendChild(style);
    }

    async _pollOnce() {
      try {
        const resp = await fetch(
          `${this.backendBaseUrl}/agents/${encodeURIComponent(this.agentExtension)}/active-call`
        );
        const data = await resp.json();
        if (data.in_call && data.call_id) {
          const callId = String(data.call_id);

          console.log("[KbSidebarWidget] Active call:", callId);

          // Kalau call yang sama dan WebSocket masih aktif,
          // jangan membuat koneksi baru.
          if (
            this._currentCallId === callId &&
            this._ws &&
            (this._ws.readyState === WebSocket.OPEN ||
              this._ws.readyState === WebSocket.CONNECTING)
          ) {
            return;
          }

          this._currentCallId = callId;
          this._connectWebSocket(callId);
          return;
        }
      } catch (err) {
        console.warn("[KbSidebarWidget] Gagal cek active-call:", err);
        this.statusEl.textContent = "Gagal terhubung ke backend, coba lagi...";
      }
      if (!this._stopped) {
        this._pollTimer = setTimeout(() => this._pollOnce(), POLL_INTERVAL_MS);
      }
    }

    _startPolling() {
      this.statusEl.textContent = "Menunggu panggilan...";
      this.statusEl.classList.remove("live");
      this.statusEl.classList.remove("error");
      this._pollOnce();
    }

    _connectWebSocket(callId) {
      if (
        this._ws &&
        (this._ws.readyState === WebSocket.CONNECTING ||
          this._ws.readyState === WebSocket.OPEN)
      ) {
        console.log("[KbSidebarWidget] WebSocket masih aktif, skip reconnect");
        return;
      }

      const wsUrl =
        this.backendBaseUrl.replace(/^http/, "ws") + `/ws/${encodeURIComponent(callId)}`;
      console.log("[KbSidebarWidget] Menghubungkan WebSocket ke", wsUrl);

      let didOpen = false; // dipakai buat bedain "call baru selesai" vs "gagal connect dari awal"
      const ws = new WebSocket(wsUrl);
      this._ws = ws;

      ws.onopen = () => {
        didOpen = true;
        this._wsFailBackoffMs = WS_FAIL_BACKOFF_START_MS; // reset backoff, koneksi berhasil
        console.log("[KbSidebarWidget] WebSocket terhubung, call_id=", callId);
        this.statusEl.textContent = `Panggilan aktif (call_id: ${callId})`;
        this.statusEl.classList.remove("error");
        this.statusEl.classList.add("live");
        this._interimLineEls = {};
      };

      ws.onmessage = (evt) => {
        let msg;
        try {
          msg = JSON.parse(evt.data);
        } catch (e) {
          return;
        }
        if (msg.type === "transcript_interim" || msg.type === "transcript_live_final") {
          this._upsertInterimLine(msg.speaker, msg.text, msg.type === "transcript_live_final");
        } else if (msg.type === "transcript") {
          this._finalizeLine(msg.speaker, msg.text);
        } else if (msg.type === "kb_suggestions") {
          this._renderSuggestions(msg.suggestions || []);
        }
      };

      ws.onclose = (evt) => {
        // Log code & reason -- INI KUNCI untuk diagnosis. Kode umum:
        //  1000 = normal close (biasanya backend nutup krn call selesai)
        //  1006 = abnormal closure (koneksi gagal/putus tanpa handshake
        //         close yang benar -- SERING berarti: salah host/port,
        //         firewall blokir, atau MIXED CONTENT http/https)
        //  1011 = server error (exception di backend saat handle koneksi)
        console.log(
          "[KbSidebarWidget] WebSocket ditutup. code=%s reason=%s wasClean=%s didOpen=%s",
          evt.code, evt.reason || "(kosong)", evt.wasClean, didOpen
        );

        // FIX: reset supaya widget tidak "nyangkut" mengira call ini
        // masih yang aktif kalau nanti reconnect ke call BARU yang lain.
        this._currentCallId = null;
        this._interimLineEls = {};
        if (this._ws === ws) this._ws = null;

        if (this._stopped) return;

        if (didOpen) {
          // Koneksi SEMPAT berhasil sebelumnya -> ini memang tanda call
          // sudah selesai/putus di tengah jalan (perilaku normal yang
          // sudah didesain: backend yang nutup WS saat StasisEnd). Balik
          // ke polling seperti biasa, tanpa delay tambahan.
          this._startPolling();
          return;
        }

        // Koneksi TIDAK PERNAH sempat open -- ini bukan "call selesai
        // normal", ini kegagalan connect (salah URL, port diblokir,
        // mixed content, backend down, dst). Reconnect langsung tanpa
        // jeda di sini akan bikin loop rapat tanpa henti persis seperti
        // gejala "reconnecting terus". Kasih backoff + tampilkan di UI
        // supaya kelihatan jelas ada masalah, jangan diam-diam retry mulus.
        this.statusEl.textContent = this._mixedContentSuspected
          ? "Gagal konek (kemungkinan http/https tidak cocok) -- cek console"
          : `Gagal konek ke server (code ${evt.code}), mencoba lagi dalam ${Math.round(this._wsFailBackoffMs / 1000)}s...`;
        this.statusEl.classList.remove("live");
        this.statusEl.classList.add("error");

        clearTimeout(this._wsFailBackoffTimer);
        this._wsFailBackoffTimer = setTimeout(() => {
          this._wsFailBackoffMs = Math.min(this._wsFailBackoffMs * 2, WS_FAIL_BACKOFF_MAX_MS);
          this._startPolling();
        }, this._wsFailBackoffMs);
      };

      ws.onerror = (evt) => {
        console.error("[KbSidebarWidget] WebSocket error:", evt);
      };
    }

    _upsertInterimLine(speaker, text, vividLock) {
      // Satu baris "sementara" per speaker -- kalau sudah ada, update teksnya
      // di tempat (bukan nambah baris baru), sampai nanti dikunci jadi final
      // (final BENERAN itu dari faster-whisper, bukan dari Vosk -- lihat
      // catatan di pipeline.py). vividLock=true (dari Vosk) cuma menghapus
      // "..." di akhir, teks tetap abu-abu karena belum tentu final.
      if (!this._interimLineEls) this._interimLineEls = {};
      let lineEl = this._interimLineEls[speaker];
      if (!lineEl) {
        lineEl = el("div", `kb-sw-line ${speaker} interim`);
        const spk = el("span", "spk", speaker === "customer" ? "Customer:" : "Agent:");
        lineEl.appendChild(spk);
        lineEl.appendChild(document.createTextNode(""));
        this.transcriptEl.appendChild(lineEl);
        this._interimLineEls[speaker] = lineEl;
      }
      lineEl.lastChild.textContent = vividLock ? text : text + " …";
      this.transcriptEl.scrollTop = this.transcriptEl.scrollHeight;
    }

    _finalizeLine(speaker, text) {
      if (!this._interimLineEls) this._interimLineEls = {};
      let lineEl = this._interimLineEls[speaker];
      if (lineEl) {
        // Kunci baris interim yang sudah ada jadi teks final (hapus gaya "sementara")
        lineEl.classList.remove("interim");
        lineEl.lastChild.textContent = text;
        delete this._interimLineEls[speaker];
      } else {
        // Tidak ada baris interim sebelumnya (mis. ucapan pendek yang
        // langsung selesai sebelum interim pertama sempat kekirim) -- buat baris baru
        this._appendTranscriptLine(speaker, text);
      }
      this.transcriptEl.scrollTop = this.transcriptEl.scrollHeight;
    }

    _appendTranscriptLine(speaker, text) {
      const line = el("div", `kb-sw-line ${speaker}`);
      const spk = el("span", "spk", speaker === "customer" ? "Customer:" : "Agent:");
      line.appendChild(spk);
      line.appendChild(document.createTextNode(text));
      this.transcriptEl.appendChild(line);
      this.transcriptEl.scrollTop = this.transcriptEl.scrollHeight;
    }

    _renderSuggestions(suggestions) {
      this.suggestionsEl.innerHTML = "";
      if (!suggestions.length) {
        this.suggestionsEl.appendChild(el("div", "kb-sw-empty", "Belum ada saran untuk topik ini."));
        return;
      }
      suggestions.forEach((s) => {
        const card = el("div", "kb-sw-suggestion-card");
        card.appendChild(el("div", "title", s.title || "(tanpa judul)"));
        card.appendChild(el("div", "content", s.content || ""));
        this.suggestionsEl.appendChild(card);
      });
    }

    destroy() {
      this._stopped = true;
      if (this._pollTimer) clearTimeout(this._pollTimer);
      if (this._wsFailBackoffTimer) clearTimeout(this._wsFailBackoffTimer);
      if (this._ws) this._ws.close();
    }
  }

  global.KbSidebarWidget = {
    /** @returns {KbSidebarWidget} instance -- simpan kalau perlu panggil .destroy() nanti (mis. saat agent logout / pindah halaman) */
    init(opts) {
      return new KbSidebarWidget(opts);
    },
  };
})(window);
