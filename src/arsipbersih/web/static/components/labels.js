// Category labels come from the server once per page (embedded in base.html), so there is
// no second copy to keep in sync. A missing or broken block falls back to the slug.
function readLabels() {
  try {
    const node = document.getElementById("category-labels");
    return node ? JSON.parse(node.textContent) : {};
  } catch { return {}; }
}

export const CATEGORY_LABELS = readLabels();
