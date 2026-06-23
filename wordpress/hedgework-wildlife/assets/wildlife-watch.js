(function () {
  function apiBase() {
    return window.location.origin;
  }

  function el(id, root) {
    return root.querySelector("#" + id);
  }

  function renderCounts(grid, counts) {
    grid.innerHTML = "";
    if (!counts.length) {
      grid.innerHTML =
        '<p class="hww-empty">No visitors spotted yet today. Check back soon!</p>';
      return;
    }
    counts.forEach(function (row) {
      const tile = document.createElement("div");
      tile.className = "hww-count-tile";
      tile.innerHTML =
        '<div class="hww-count-number">' +
        row.count +
        '</div><div class="hww-count-label">' +
        (row.display_name || row.species) +
        "</div>";
      grid.appendChild(tile);
    });
  }

  function renderGallery(grid, sightings) {
    grid.innerHTML = "";
    if (!sightings.length) {
      grid.innerHTML =
        '<p class="hww-empty">No photos yet. The cameras are watching!</p>';
      return;
    }
    sightings.forEach(function (item) {
      const card = document.createElement("figure");
      card.className = "hww-card";
      const img = document.createElement("img");
      img.src = item.image_url || "";
      img.alt = item.display_name || item.species || "Wildlife";
      const cap = document.createElement("figcaption");
      cap.textContent = item.display_name || item.species || "Visitor";
      card.appendChild(img);
      card.appendChild(cap);
      grid.appendChild(card);
    });
  }

  async function refresh(root) {
    const countsGrid = el("hww-counts-grid", root);
    const galleryGrid = el("hww-gallery-grid", root);
    const base = apiBase();

    try {
      const [statsRes, recentRes] = await Promise.all([
        fetch(base + "/wp-json/hedgework/v1/stats?period=today"),
        fetch(base + "/wp-json/hedgework/v1/recent?limit=12"),
      ]);
      if (!statsRes.ok || !recentRes.ok) {
        throw new Error("API error");
      }
      const stats = await statsRes.json();
      const recent = await recentRes.json();
      renderCounts(countsGrid, stats.counts || []);
      renderGallery(galleryGrid, recent.sightings || []);
    } catch (err) {
      countsGrid.innerHTML =
        '<p class="hww-empty">Could not load wildlife data right now.</p>';
      galleryGrid.innerHTML = "";
    }
  }

  function init(root) {
    const interval = parseInt(root.dataset.pollIntervalMs || "60000", 10);
    refresh(root);
    setInterval(function () {
      refresh(root);
    }, interval);
  }

  document.querySelectorAll(".hedgework-wildlife-watch").forEach(init);
})();
