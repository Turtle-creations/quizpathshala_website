document.addEventListener("DOMContentLoaded", function () {
    var header = document.querySelector("[data-collapsible-navbar]");
    if (!header) {
        return;
    }

    var shell = header.querySelector("[data-navbar-shell]");
    var logo = header.querySelector("[data-navbar-logo]");
    var logoMark = header.querySelector(".brand-mark");
    var closeButton = header.querySelector("[data-navbar-close]");
    var backdrop = header.querySelector("[data-navbar-backdrop]");
    var navItems = Array.prototype.slice.call(header.querySelectorAll(".site-nav a, .nav-actions > *"));
    var navbarMode = header.getAttribute("data-navbar-mode") || "default";
    var isQuizSidebar = navbarMode === "quiz-sidebar";
    var collapseThreshold = 80;
    var expandThreshold = 40;
    var throttleMs = 90;
    var lastKnownScrollY = window.scrollY || 0;
    var lastRunAt = 0;
    var ticking = false;
    var isCollapsed = header.classList.contains("navbar-collapsed");

    function syncBodyState() {
        document.body.classList.toggle("quiz-sidebar-open", isQuizSidebar && !isCollapsed);
    }

    function setAnimationMetrics() {
        if (!shell || !logoMark) {
            return;
        }

        var shouldRestoreCollapsed = isCollapsed;
        header.classList.remove("navbar-collapsed", "navbar-open");
        shell.style.width = "auto";

        var expandedWidth = shell.scrollWidth;
        var collapsedSize = logoMark.offsetWidth;

        shell.style.setProperty("--nav-expanded-width", expandedWidth + "px");
        shell.style.setProperty("--nav-collapsed-size", collapsedSize + "px");

        if (shouldRestoreCollapsed) {
            header.classList.add("navbar-collapsed");
        } else if (isQuizSidebar) {
            header.classList.add("navbar-open");
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
        if (shouldCollapse === isCollapsed) {
            return;
        }

        isCollapsed = shouldCollapse;
        header.classList.toggle("navbar-collapsed", isCollapsed);
        if (isQuizSidebar) {
            header.classList.toggle("navbar-open", !isCollapsed);
        }
        syncBodyState();
    }

    function processScroll() {
        if (isQuizSidebar) {
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
        if (isQuizSidebar) {
            return;
        }

        lastKnownScrollY = window.scrollY || 0;
        var now = performance.now();

        if (ticking || now - lastRunAt < throttleMs) {
            return;
        }

        ticking = true;
        window.requestAnimationFrame(processScroll);
    }

    function expandQuizSidebar(event) {
        if (!isQuizSidebar || !isCollapsed) {
            return;
        }
        if (event) {
            event.preventDefault();
            event.stopPropagation();
        }
        applyNavbarState(false);
    }

    function collapseQuizSidebar() {
        if (!isQuizSidebar || isCollapsed) {
            return;
        }
        applyNavbarState(true);
    }

    if (!isQuizSidebar) {
        window.addEventListener("scroll", requestScrollUpdate, { passive: true });
    }

    window.addEventListener("resize", function () {
        window.requestAnimationFrame(function () {
            setAnimationMetrics();
            lastKnownScrollY = window.scrollY || 0;
            processScroll();
        });
    });

    if (logo) {
        logo.addEventListener("click", function (event) {
            if (isQuizSidebar) {
                expandQuizSidebar(event);
                return;
            }
            if (isCollapsed) {
                event.preventDefault();
                event.stopPropagation();
                applyNavbarState(false);
            }
        });

        logo.addEventListener("touchstart", function () {
            if (isQuizSidebar) {
                expandQuizSidebar();
                return;
            }
            if (isCollapsed) {
                applyNavbarState(false);
            }
        }, { passive: true });

        logo.addEventListener("keydown", function (event) {
            if (event.key !== "Enter" && event.key !== " ") {
                return;
            }
            if (isQuizSidebar) {
                expandQuizSidebar(event);
                return;
            }
            if (isCollapsed) {
                event.preventDefault();
                applyNavbarState(false);
            }
        });
    }

    if (closeButton) {
        closeButton.addEventListener("click", function () {
            collapseQuizSidebar();
            if (logo) {
                logo.focus();
            }
        });
    }

    if (backdrop) {
        backdrop.addEventListener("click", function () {
            collapseQuizSidebar();
        });
    }

    document.addEventListener("click", function (event) {
        if (!isQuizSidebar || isCollapsed) {
            return;
        }
        if (!header.contains(event.target)) {
            collapseQuizSidebar();
        }
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape" && isQuizSidebar && !isCollapsed) {
            collapseQuizSidebar();
            if (logo) {
                logo.focus();
            }
        }
    });

    setItemDelays();
    setAnimationMetrics();
    processScroll();
    syncBodyState();
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
    if (!document.querySelector("[data-admin-scroll-preserve='true']") && !document.querySelector("[data-admin-dashboard='true']")) {
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
