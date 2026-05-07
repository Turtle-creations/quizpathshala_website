document.addEventListener("DOMContentLoaded", function () {
    var header = document.querySelector("[data-collapsible-navbar]");
    if (!header) {
        return;
    }

    var shell = header.querySelector("[data-navbar-shell]");
    var logo = header.querySelector("[data-navbar-logo]");
    var logoMark = header.querySelector(".brand-mark");
    var navItems = Array.prototype.slice.call(header.querySelectorAll(".site-nav a, .nav-actions > *"));
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
        if (shouldCollapse === isCollapsed) {
            return;
        }

        isCollapsed = shouldCollapse;
        header.classList.toggle("navbar-collapsed", isCollapsed);
    }

    function processScroll() {
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
            if (isCollapsed) {
                event.preventDefault();
                event.stopPropagation();
                applyNavbarState(false);
            }
        });

        logo.addEventListener("touchstart", function () {
            if (isCollapsed) {
                applyNavbarState(false);
            }
        }, { passive: true });

        logo.addEventListener("keydown", function (event) {
            if (isCollapsed && (event.key === "Enter" || event.key === " ")) {
                event.preventDefault();
                applyNavbarState(false);
            }
        });
    }

    setItemDelays();
    setAnimationMetrics();
    processScroll();
});
