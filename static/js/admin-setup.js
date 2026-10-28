/* The setup token stays in this page's closure, never in a cookie or URL request. */
(() => {
  "use strict";
  let token = "";
  const readSetupFragment = (event) => {
    // Ignore older queued navigations, including ones predating our URL cleanup.
    if (event && new URL(event.newURL).hash !== window.location.hash) return;
    const fragment = window.location.hash;
    token = "";
    window.history.replaceState(null, "", window.location.pathname);
    token = new URLSearchParams(fragment.slice(1)).get("token") || "";
  };
  readSetupFragment();
  window.addEventListener("hashchange", readSetupFragment);
  window.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("setup-form");
    const error = document.getElementById("setup-error");
    const button = form.querySelector("button");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!token) {
        error.textContent = "Open the private setup link from the startup console.";
        return;
      }
      if (form.elements.password.value !== form.elements.confirmation.value) {
        error.textContent = "Passwords must match.";
        return;
      }
      button.disabled = true;
      const data = new URLSearchParams(new FormData(form));
      data.set("token", token);
      try {
        const response = await fetch(form.action, { method: "POST", body: data, credentials: "same-origin" });
        if (response.redirected && response.ok) {
          token = "";
          form.reset();
          window.location.replace(new URL("login", form.action).pathname);
          return;
        }
        const result = await response.json();
        error.textContent = result.error || "Setup could not be completed.";
      } catch {
        error.textContent = "Setup could not be completed. Check the connection and try again.";
      } finally {
        button.disabled = false;
      }
    });
  });
})();
