(() => {
  "use strict";

  const resetSubmittingState = (form) => {
    if (!(form instanceof HTMLFormElement)) return;
    delete form.dataset.eamSubmitting;
    form.removeAttribute("aria-busy");
  };

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (event.defaultPrevented) return;
    if (form.dataset.eamSubmitting === "true") {
      event.preventDefault();
      return;
    }
    form.dataset.eamSubmitting = "true";
    form.setAttribute("aria-busy", "true");
  });

  for (const eventName of ["htmx:afterRequest", "htmx:responseError", "htmx:sendError"]) {
    document.addEventListener(eventName, (event) => {
      resetSubmittingState(event.target?.closest?.("form"));
    });
  }

  window.addEventListener("pageshow", () => {
    document.querySelectorAll("form[data-eam-submitting]").forEach(resetSubmittingState);
  });

  document.querySelectorAll("[data-menu-search]").forEach((input) => {
    const navigation = input.closest("nav");
    const links = Array.from(navigation.querySelectorAll("a.app-nav-link"));
    const groups = Array.from(navigation.querySelectorAll("details.app-nav-group"));
    const originalOpen = new Map(groups.map(group => [group, group.open]));
    const message = navigation.querySelector("[data-menu-search-empty]");
    let searching = false;
    input.addEventListener("input", () => {
      const query = input.value.trim().toLocaleLowerCase();
      if (query && !searching) groups.forEach(group => originalOpen.set(group, group.open));
      links.forEach(link => {
        const section = link.closest("details")?.querySelector("summary")?.textContent || "";
        link.hidden = Boolean(query && !(link.textContent + " " + section).toLocaleLowerCase().includes(query));
      });
      groups.forEach(group => {
        group.hidden = !Array.from(group.querySelectorAll("a.app-nav-link")).some(link => !link.hidden);
        group.open = query ? !group.hidden : originalOpen.get(group);
      });
      message.hidden = links.some(link => !link.hidden);
      searching = Boolean(query);
    });
    input.addEventListener("keydown", event => {
      if (event.key === "Escape") { input.value = ""; input.dispatchEvent(new Event("input")); }
    });
  });
  document.addEventListener("keydown", event => {
    const typing = event.target.closest?.("input,textarea,select,[contenteditable='true']");
    if (event.key !== "/" || typing || event.ctrlKey || event.metaKey || event.altKey) return;
    const search = document.getElementById("global-asset-search");
    if (search && search.offsetParent !== null) { event.preventDefault(); search.focus(); search.select(); }
  });
})();
