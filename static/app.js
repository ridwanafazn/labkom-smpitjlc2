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
  toggleSidebar(false);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function goView(id) {
  const button = document.querySelector('.nav-item[data-view="' + id + '"]');
  showView(id, button);
}

function toggleSidebar(force) {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("sidebarBackdrop");
  if (!sidebar) return;
  const open = typeof force === "boolean" ? force : !sidebar.classList.contains("open");
  sidebar.classList.toggle("open", open);
  backdrop?.classList.toggle("show", open);
}

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

async function startScanner() {
  if (!window.Html5Qrcode) {
    alert("Pustaka scanner tidak termuat.");
    return;
  }
  try {
    if (!scanner) scanner = new Html5Qrcode("reader");
    await scanner.start(
      { facingMode: "environment" },
      { fps: 10, qrbox: { width: 240, height: 240 } },
      async (decoded) => {
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
          await stopScanner();
        } catch (error) {
          document.getElementById("studentbox").textContent =
            "QR terbaca, tetapi " + error.message + ". Pastikan QR berasal dari daftar murid.";
        } finally {
          scanBusy = false;
        }
      },
      () => {}
    );
  } catch (error) {
    alert("Kamera tidak dapat dibuka. Beri izin kamera dan buka web melalui localhost atau HTTPS.");
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
}

function confirmClearLoans(form) {
  if (form.confirmation.value.trim() !== "BERSIHKAN") {
    alert("Ketik BERSIHKAN dengan benar.");
    return false;
  }
  return confirm("Semua riwayat peminjaman dan foto akan dihapus. Backup dibuat otomatis. Lanjutkan?");
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".toast").forEach((toast) => {
    setTimeout(() => toast.remove(), 6500);
  });

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
