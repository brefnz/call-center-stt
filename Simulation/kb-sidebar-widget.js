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
 *   3. Begitu panggilan selesai (WebSocket ditutup backend), widget balik
 *      ke mode polling, nunggu panggilan berikutnya.
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
      this._ws = null;
      this._currentCallId = null;
      this._stopped = false;

      this._buildDom();
      this._startPolling();
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
      this._ws = new WebSocket(wsUrl);

    this._ws.onopen = () => {
      this.statusEl.textContent = `Panggilan aktif (call_id: ${callId})`;
      this.statusEl.classList.add("live");

      if (!this._interimLineEls) {
        this._interimLineEls = {};
      }
    };

      this._ws.onmessage = (evt) => {
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

      this._ws.onclose = () => {
        // Panggilan selesai (atau koneksi putus) -> balik ke mode polling
        if (!this._stopped) {
          this._startPolling();
        }
      };

      this._ws.onerror = () => {
        this.statusEl.textContent = "Koneksi terputus, mencoba ulang...";
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
