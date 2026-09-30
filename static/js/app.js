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

  const guardedForms = Array.from(document.querySelectorAll("form[data-unsaved-guard]")).map(form => {
    const snapshot = () => JSON.stringify(Array.from(form.elements)
      .filter(field => field.name && !field.disabled && !["hidden", "submit", "button"].includes(field.type))
      .map(field => [field.name, ["checkbox", "radio"].includes(field.type) ? field.checked : field.value]));
    return { form, snapshot, initial: snapshot(), pending: form.dataset.unsavedGuard === "true" };
  });
  let leaveAllowed = false;
  let leaveDialog = null;
  const hasUnsaved = () => !leaveAllowed && guardedForms.some(entry =>
    entry.form.dataset.eamSubmitting !== "true" && (entry.pending || entry.snapshot() !== entry.initial));
  const requestLeave = proceed => {
    if (leaveDialog) return;
    leaveDialog = document.createElement("dialog");
    leaveDialog.className = "border-0 rounded p-4 shadow";
    leaveDialog.setAttribute("aria-labelledby", "unsaved-input-title");
    leaveDialog.innerHTML = '<h2 class="h5" id="unsaved-input-title">尚有未保存的输入</h2><p>离开会丢失本页尚未保存的内容。</p><div class="d-flex gap-2"><button type="button" class="btn btn-primary" data-stay>继续编辑</button><button type="button" class="btn btn-outline-danger" data-leave>放弃输入并继续</button></div>';
    const dialog = leaveDialog;
    dialog.addEventListener("close", () => { dialog.remove(); leaveDialog = null; });
    dialog.querySelector("[data-stay]").addEventListener("click", () => dialog.close());
    dialog.querySelector("[data-leave]").addEventListener("click", () => {
      leaveAllowed = true;
      dialog.close();
      proceed();
    });
    document.body.appendChild(dialog);
    dialog.showModal();
  };
  document.addEventListener("click", event => {
    const link = event.target.closest?.("a[href]");
    if (!link || !hasUnsaved() || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (link.download || (link.target && link.target !== "_self")) return;
    const destination = new URL(link.href, window.location.href);
    if (!["http:", "https:"].includes(destination.protocol)) return;
    if (destination.hash && destination.pathname === window.location.pathname && destination.search === window.location.search) return;
    event.preventDefault();
    requestLeave(() => window.location.assign(destination.href));
  });
  document.addEventListener("submit", event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || guardedForms.some(entry => entry.form === form) || !hasUnsaved()) return;
    event.preventDefault();
    resetSubmittingState(form);
    const submitter = event.submitter;
    requestLeave(() => submitter ? form.requestSubmit(submitter) : form.requestSubmit());
  });
  window.addEventListener("beforeunload", event => {
    if (!hasUnsaved()) return;
    event.preventDefault();
    event.returnValue = "";
  });
  window.addEventListener("pageshow", () => { leaveAllowed = false; });

  document.querySelectorAll("[data-page-jump]").forEach(container => {
    const input = container.querySelector("[data-page-jump-value]");
    const button = container.querySelector("[data-page-jump-go]");
    if (!input || !button) return;
    const go = () => {
      if (!input.reportValidity()) return;
      const url = new URL(window.location.href);
      if (container.dataset.pageQuery) url.search = container.dataset.pageQuery;
      url.searchParams.set(container.dataset.pageParameter || "page", String(Number(input.value)));
      if (container.dataset.pageAnchor) url.hash = container.dataset.pageAnchor;
      window.location.assign(url.pathname + url.search + url.hash);
    };
    input.disabled = button.disabled = false;
    button.addEventListener("click", go);
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") { event.preventDefault(); go(); }
    });
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
