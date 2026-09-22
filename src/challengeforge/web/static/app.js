const CF = {
  currentUser: null,
  esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  },
  banner(message, isError = false) {
    const el = document.getElementById("banner");
    if (!el) return;
    el.hidden = !message;
    el.classList.toggle("error", Boolean(isError));
    el.textContent = message || "";
  },
  async api(path, options = {}) {
    const headers = {
      "Content-Type": "application/json",
      "X-User-Id": localStorage.getItem("cf_user_id") || "",
      ...(options.headers || {}),
    };
    const response = await fetch(path, { ...options, headers });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const message = data.error?.message || `Request failed (${response.status})`;
      this.banner(message, true);
      throw new Error(message);
    }
    return data;
  },
};

async function boot() {
  const users = await fetch("/api/v1/dev/users").then((r) => r.json());
  const select = document.getElementById("user-switcher");
  const stored = localStorage.getItem("cf_user_id") || users[0]?.id;
  if (stored) {
    localStorage.setItem("cf_user_id", stored);
    document.cookie = `cf_user_id=${stored}; path=/`;
  }
  select.innerHTML = users
    .map(
      (u) =>
        `<option value="${u.id}" ${u.id === stored ? "selected" : ""}>${CF.esc(u.display_name)} (${u.role})</option>`
    )
    .join("");
  select.addEventListener("change", () => {
    localStorage.setItem("cf_user_id", select.value);
    document.cookie = `cf_user_id=${select.value}; path=/`;
    location.reload();
  });
  try {
    CF.currentUser = await CF.api("/api/v1/me");
  } catch {
    CF.currentUser = null;
  }
  document.dispatchEvent(new Event("cf:ready"));
}

boot();
