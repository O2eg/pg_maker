/* Retain the theme's accessible details/summary controls, adding a short transition. */
document.addEventListener("DOMContentLoaded", () => {
  // Browsers block fetch() of neighbouring file:// pages. Search still uses the
  // complete local index; only snippet requests need to be disabled in this mode.
  if (location.protocol === "file:" && typeof DOCUMENTATION_OPTIONS !== "undefined") {
    DOCUMENTATION_OPTIONS.SHOW_SEARCH_SUMMARY = false;
  }
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  document.querySelectorAll(".bd-sidebar-primary details").forEach((details) => {
    const summary = details.querySelector(":scope > summary");
    const list = details.querySelector(":scope > ul");
    if (!summary || !list) return;
    let animation = null;
    let opening = details.open;
    summary.addEventListener("click", (event) => {
      if (reducedMotion.matches) return;
      event.preventDefault();
      const startHeight = details.open ? list.getBoundingClientRect().height : 0;
      opening = animation ? !opening : !details.open;
      if (animation) animation.cancel();
      // Keep the list laid out while closing, then restore native details state.
      details.open = true;
      list.style.overflow = "hidden";
      animation = list.animate([
        { height: `${startHeight}px`, opacity: startHeight ? 1 : 0 },
        { height: opening ? `${list.scrollHeight}px` : "0px", opacity: opening ? 1 : 0 },
      ], { duration: 220, easing: "ease-out" });
      animation.onfinish = () => {
        details.open = opening;
        list.style.removeProperty("overflow");
        animation = null;
      };
    });
  });
});
