// Progressive enhancement only: every form still works without JavaScript.
document.addEventListener("submit", function (event) {
    var message = event.target.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
        event.preventDefault();
    }
});

// Evidence form: show only the fields that matter for the chosen type.
(function () {
    var select = document.getElementById("evidence_type");
    if (!select) return;
    var urlRow = document.getElementById("url-row");
    var hint = document.getElementById("evidence-hint");
    var hints = {
        github_repo: "Repository root, e.g. https://github.com/owner/repo",
        commit: "e.g. https://github.com/owner/repo/commit/<sha>",
        pull_request: "e.g. https://github.com/owner/repo/pull/12",
        deployed_url: "A live URL a reviewer can open",
        demo_url: "A video or walkthrough link",
        writeup: "Describe what you built and what you personally did (50+ characters).",
        other_link: "Explain what this link proves (20+ characters)."
    };
    function update() {
        var type = select.value;
        urlRow.style.display = type === "writeup" ? "none" : "";
        hint.textContent = hints[type] || "";
    }
    select.addEventListener("change", update);
    update();
})();

// Demo probe: point the form at the selected event's URL.
(function () {
    var select = document.getElementById("probe-event");
    var button = document.getElementById("probe-btn");
    if (!select || !button) return;
    function update() {
        button.setAttribute("formaction", button.getAttribute("data-base").replace("__EVENT__", select.value));
    }
    select.addEventListener("change", update);
    update();
})();

// Select a read-only link box on focus/click so it is easy to copy (no inline handlers: see the CSP).
document.addEventListener("click", function (event) {
    var el = event.target;
    if (el && el.hasAttribute && el.hasAttribute("data-select-on-focus")) el.select();
});
