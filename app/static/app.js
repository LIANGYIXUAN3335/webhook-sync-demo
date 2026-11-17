// Small progressive enhancements for the server-rendered pages. Everything works
// without JavaScript; this only adds live refresh, countdowns, and flash cleanup.
(function () {
  "use strict";

  // Flash message: drop ?msg=&level= from the URL so a reload doesn't repeat it,
  // and fade it out after a few seconds.
  const flash = document.querySelector(".flash");
  if (flash) {
    try {
      const url = new URL(location.href);
      url.searchParams.delete("msg");
      url.searchParams.delete("level");
      history.replaceState(null, "", url.pathname + (url.search || ""));
    } catch (e) { /* ignore */ }
    setTimeout(() => flash.classList.add("flash-hide"), 6000);
  }

  // Countdown to the next retry, ticking between server refreshes.
  function tick() {
    const now = Date.now() / 1000;
    document.querySelectorAll(".countdown[data-at]").forEach((el) => {
      const left = parseFloat(el.dataset.at) - now;
      el.textContent = left <= 0 ? "due now" : "in " + left.toFixed(1) + "s";
    });
  }
  setInterval(tick, 200);
  tick();

  // Dashboard: poll the server-rendered fragment and swap the live regions in place.
  const pause = document.getElementById("pause");
  if (pause && window.LIVE_QUERY) {
    try { pause.checked = localStorage.getItem("pauseRefresh") === "1"; } catch (e) { /* ignore */ }
    pause.addEventListener("change", () => {
      try { localStorage.setItem("pauseRefresh", pause.checked ? "1" : "0"); } catch (e) { /* ignore */ }
    });
    const params = new URLSearchParams();
    Object.entries(window.LIVE_QUERY).forEach(([k, v]) => { if (v) params.set(k, v); });
    const endpoint = "/partials/live" + (params.toString() ? "?" + params : "");
    async function refresh() {
      if (pause.checked || document.hidden) return;
      try {
        const r = await fetch(endpoint, { headers: { "X-Requested-With": "fetch" } });
        if (!r.ok) return;
        const doc = new DOMParser().parseFromString(await r.text(), "text/html");
        doc.querySelectorAll("[data-live]").forEach((fresh) => {
          const cur = document.querySelector('[data-live="' + fresh.dataset.live + '"]');
          if (cur && cur.innerHTML !== fresh.innerHTML) cur.replaceWith(fresh);
        });
        tick();
      } catch (e) { /* server restarting; try again next time */ }
    }
    setInterval(refresh, 1500);
  }

  // Event detail: keep reloading while the event is still in flight.
  if (window.AUTO_RELOAD) {
    setTimeout(() => { if (!document.hidden) location.reload(); }, 2000);
  }
})();
