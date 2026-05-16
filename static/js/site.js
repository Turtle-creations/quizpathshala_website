document.addEventListener("DOMContentLoaded", function () {
    var header = document.querySelector("[data-collapsible-navbar]");
    if (!header) {
        return;
    }

    var shell = header.querySelector("[data-navbar-shell]");
    var logo = header.querySelector("[data-navbar-logo]");
    var logoMark = header.querySelector(".brand-mark");
    var navItems = Array.prototype.slice.call(header.querySelectorAll(".site-nav a, .nav-actions > *"));
    var isLocked = header.getAttribute("data-navbar-locked") === "true";
    var collapseThreshold = 80;
    var expandThreshold = 40;
    var throttleMs = 90;
    var lastKnownScrollY = window.scrollY || 0;
    var lastRunAt = 0;
    var ticking = false;
    var isCollapsed = false;

    function setAnimationMetrics() {
        if (!shell || !logoMark) {
            return;
        }

        header.classList.remove("navbar-collapsed");
        shell.style.width = "auto";

        var expandedWidth = shell.scrollWidth;
        var collapsedSize = logoMark.offsetWidth;

        shell.style.setProperty("--nav-expanded-width", expandedWidth + "px");
        shell.style.setProperty("--nav-collapsed-size", collapsedSize + "px");

        if (isCollapsed) {
            header.classList.add("navbar-collapsed");
        }
    }

    function setItemDelays() {
        var total = navItems.length;
        navItems.forEach(function (item, index) {
            item.style.setProperty("--nav-order", String(index));
            item.style.setProperty("--collapse-delay", (index * 28) + "ms");
            item.style.setProperty("--expand-delay", ((total - index - 1) * 20) + "ms");
        });
    }

    function applyNavbarState(shouldCollapse) {
        if (isLocked && !shouldCollapse) {
            return;
        }
        if (shouldCollapse === isCollapsed) {
            return;
        }

        isCollapsed = shouldCollapse;
        header.classList.toggle("navbar-collapsed", isCollapsed);
    }

    function processScroll() {
        if (isLocked) {
            applyNavbarState(true);
            lastRunAt = performance.now();
            ticking = false;
            return;
        }

        var currentScrollY = lastKnownScrollY;

        if (!isCollapsed && currentScrollY >= collapseThreshold) {
            applyNavbarState(true);
        } else if (isCollapsed && currentScrollY <= expandThreshold) {
            applyNavbarState(false);
        }

        lastRunAt = performance.now();
        ticking = false;
    }

    function requestScrollUpdate() {
        lastKnownScrollY = window.scrollY || 0;
        var now = performance.now();

        if (ticking || now - lastRunAt < throttleMs) {
            return;
        }

        ticking = true;
        window.requestAnimationFrame(processScroll);
    }

    window.addEventListener("scroll", requestScrollUpdate, { passive: true });

    window.addEventListener("resize", function () {
        window.requestAnimationFrame(function () {
            setAnimationMetrics();
            lastKnownScrollY = window.scrollY || 0;
            processScroll();
        });
    });

    if (logo) {
        logo.addEventListener("click", function (event) {
            if (isCollapsed && !isLocked) {
                event.preventDefault();
                event.stopPropagation();
                applyNavbarState(false);
            }
        });

        logo.addEventListener("touchstart", function () {
            if (isCollapsed && !isLocked) {
                applyNavbarState(false);
            }
        }, { passive: true });

        logo.addEventListener("keydown", function (event) {
            if (isCollapsed && !isLocked && (event.key === "Enter" || event.key === " ")) {
                event.preventDefault();
                applyNavbarState(false);
            }
        });
    }

    setItemDelays();
    setAnimationMetrics();
    processScroll();
});


document.addEventListener("DOMContentLoaded", function () {
    var protectedForms = Array.prototype.slice.call(document.querySelectorAll("form[data-disable-on-submit='true']"));

    protectedForms.forEach(function (form) {
        form.addEventListener("submit", function (event) {
            var confirmMessage = form.getAttribute("data-confirm");
            if (confirmMessage && !window.confirm(confirmMessage)) {
                event.preventDefault();
                return;
            }

            if (form.dataset.submitting === "true") {
                event.preventDefault();
                return;
            }

            form.dataset.submitting = "true";
            Array.prototype.slice.call(form.querySelectorAll("button, input[type='submit']")).forEach(function (button) {
                button.disabled = true;
            });
        });
    });
});


document.addEventListener("DOMContentLoaded", function () {
    if (!document.querySelector("[data-admin-dashboard='true']")) {
        return;
    }

    var storageKey = "admin-dashboard-scroll-state";

    function sanitizeAnchor(value) {
        if (!value) {
            return "";
        }
        return String(value).replace(/^#/, "").trim();
    }

    function resolvePanelId(form) {
        var explicitAnchor = sanitizeAnchor(form.getAttribute("data-return-anchor"));
        if (explicitAnchor) {
            return explicitAnchor;
        }

        var existingAnchor = form.querySelector("input[name='return_anchor']");
        if (existingAnchor && sanitizeAnchor(existingAnchor.value)) {
            return sanitizeAnchor(existingAnchor.value);
        }

        var panel = form.closest("[data-admin-panel]");
        return panel && panel.id ? panel.id : "";
    }

    function ensureHiddenField(form, name, value) {
        var field = form.querySelector("input[name='" + name + "']");
        if (!field) {
            field = document.createElement("input");
            field.type = "hidden";
            field.name = name;
            form.appendChild(field);
        }
        field.value = value;
    }

    function saveState(panelId) {
        var state = {
            anchor: panelId || "",
            scrollY: window.scrollY || window.pageYOffset || 0,
            path: window.location.pathname,
            query: window.location.search || ""
        };
        window.sessionStorage.setItem(storageKey, JSON.stringify(state));
    }

    Array.prototype.slice.call(document.querySelectorAll("form[data-admin-preserve='true']")).forEach(function (form) {
        form.addEventListener("submit", function () {
            var panelId = resolvePanelId(form);
            saveState(panelId);
            ensureHiddenField(form, "return_anchor", panelId);
        });
    });

    Array.prototype.slice.call(document.querySelectorAll("a[href*='#']")).forEach(function (link) {
        if (!link.href || link.href.indexOf(window.location.pathname) === -1) {
            return;
        }
        link.addEventListener("click", function () {
            saveState(sanitizeAnchor(link.hash));
        });
    });

    var storedState = null;
    try {
        storedState = JSON.parse(window.sessionStorage.getItem(storageKey) || "null");
    } catch (error) {
        storedState = null;
    }

    var hashAnchor = sanitizeAnchor(window.location.hash);
    var targetId = hashAnchor || (storedState && storedState.path === window.location.pathname ? sanitizeAnchor(storedState.anchor) : "");
    var targetPanel = targetId ? document.getElementById(targetId) : null;

    if (targetPanel) {
        window.requestAnimationFrame(function () {
            targetPanel.scrollIntoView({ block: "start", behavior: "auto" });
            window.setTimeout(function () {
                try {
                    targetPanel.focus({ preventScroll: true });
                } catch (error) {
                    targetPanel.focus();
                }
            }, 20);
        });
    } else if (storedState && storedState.path === window.location.pathname && typeof storedState.scrollY === "number") {
        window.requestAnimationFrame(function () {
            window.scrollTo(0, storedState.scrollY);
        });
    }
});
