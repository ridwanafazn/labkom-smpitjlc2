let scanBusy = false;
let scanner = null;

function showView(id, button) {
  const target = document.getElementById("view-" + id);
  if (!target) return;
  document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view === target));
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === id);
  });
  if (id !== "murid") stopScanner();
  if (window.location.hash !== "#" + id) {
    history.replaceState(null, "", id === "dashboard" ? window.location.pathname : "#" + id);
  }
  if (window.matchMedia("(max-width: 760px)").matches) toggleSidebar(false);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function goView(id) {
  const button = document.querySelector('.nav-item[data-view="' + id + '"]');
  showView(id, button);
}

function toggleSidebar(force) {
  const app = document.querySelector(".app");
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  if (!app || !sidebar) return;
  if (window.matchMedia("(max-width: 760px)").matches) {
    app.classList.remove("sidebar-collapsed");
    const open = typeof force === "boolean" ? force : !sidebar.classList.contains("open");
    sidebar.classList.toggle("open", open);
    backdrop?.classList.toggle("show", open);
  } else {
    sidebar.classList.remove("open");
    backdrop?.classList.remove("show");
    if (typeof force === "boolean") app.classList.toggle("sidebar-collapsed", !force);
    else app.classList.toggle("sidebar-collapsed");
  }
}

window.addEventListener("resize", () => {
  const app = document.querySelector(".app");
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  if (!app || !sidebar) return;
  if (window.matchMedia("(max-width: 760px)").matches) {
    app.classList.remove("sidebar-collapsed");
  } else {
    sidebar.classList.remove("open");
    backdrop?.classList.remove("show");
  }
});

function filterTable(input, tableId) {
  const query = input.value.trim().toLowerCase();
  const rows = document.querySelectorAll("#" + tableId + " tbody tr[data-search]");
  rows.forEach((row) => {
    row.style.display = row.dataset.search.includes(query) ? "" : "none";
  });
}

function togglePassword(id, button) {
  const input = document.getElementById(id);
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  button.textContent = show ? "Sembunyikan" : "Lihat";
  button.setAttribute("aria-label", show ? "Sembunyikan password" : "Tampilkan password");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[char]));
}

function setScannerStatus(message) {
  const status = document.getElementById("scannerStatus");
  if (status) status.textContent = message;
}

async function handleScan(decoded) {
  if (scanBusy) return;
  scanBusy = true;
  const nis = decoded.trim();
  try {
    const response = await fetch("/api/student/" + encodeURIComponent(nis));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "NIS tidak ditemukan");
    document.getElementById("nis").value = data.nis;
    document.getElementById("studentbox").innerHTML =
      "<b>" + escapeHtml(data.nama) + "</b>Kelas: " + escapeHtml(data.kelas) +
      "<br>NIS: " + escapeHtml(data.nis);
    setScannerStatus("QR terbaca.");
    await stopScanner();
  } catch (error) {
    document.getElementById("studentbox").textContent =
      "QR terbaca, tetapi " + error.message + ". Pastikan QR berasal dari daftar murid.";
  } finally {
    scanBusy = false;
  }
}

async function startScanner() {
  if (!window.Html5Qrcode) {
    setScannerStatus("Pustaka scanner tidak termuat. Muat ulang halaman.");
    return;
  }
  setScannerStatus("Membuka kamera...");
  try {
    const cameras = await Html5Qrcode.getCameras();
    if (!cameras || cameras.length === 0) {
      setScannerStatus("Kamera tidak ditemukan.");
      return;
    }
    if (scanner) {
      try { await scanner.clear(); } catch (error) { /* abaikan */ }
    }
    const camera = cameras[cameras.length - 1] || cameras[0];
    scanner = new Html5Qrcode("reader");
    await scanner.start(
      camera.id,
      { fps: 10, qrbox: { width: 240, height: 240 } },
      handleScan,
      () => {}
    );
    setScannerStatus("Kamera aktif. Arahkan QR ke kamera.");
  } catch (error) {
    const name = error && error.name ? error.name : "";
    const text = String(error || "");
    if (name === "NotAllowedError" || text.includes("Permission")) {
      setScannerStatus("Izin kamera ditolak. Aktifkan izin kamera di browser.");
    } else if (name === "NotFoundError" || text.includes("NotFound")) {
      setScannerStatus("Kamera tidak ditemukan.");
    } else if (name === "NotReadableError" || text.includes("NotReadable")) {
      setScannerStatus("Kamera sedang dipakai aplikasi lain.");
    } else if (name === "OverconstrainedError") {
      setScannerStatus("Kamera tidak mendukung mode yang diminta.");
    } else {
      setScannerStatus("Kamera tidak dapat dibuka. Gunakan localhost atau HTTPS.");
    }
    scanner = null;
  }
}

async function stopScanner() {
  if (scanner) {
    try {
      if (scanner.isScanning) await scanner.stop();
    } catch (error) {
      // abaikan
    }
  }
  setScannerStatus("Kamera dimatikan.");
}

function confirmClearLoans(form) {
  if (form.confirmation.value.trim() !== "BERSIHKAN") {
    alert("Ketik BERSIHKAN dengan benar.");
    return false;
  }
  return confirm("Semua riwayat peminjaman dan foto akan dihapus. Backup dibuat otomatis. Lanjutkan?");
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("form[data-once]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      const button = form.querySelector('button[type="submit"]');
      if (!button) return;
      setTimeout(() => {
        button.disabled = true;
        button.dataset.original = button.textContent;
        button.textContent = "Memproses...";
      }, 0);
    });
  });

  const view = window.location.hash.replace("#", "");
  if (view && document.getElementById("view-" + view)) {
    goView(view);
  }
});
