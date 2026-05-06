document.addEventListener("DOMContentLoaded", function () {
    var header = document.querySelector("[data-collapsible-navbar]");
    if (!header) {
        return;
    }

    var shell = header.querySelector("[data-navbar-shell]");
    var logo = header.querySelector("[data-navbar-logo]");
    var logoMark = header.querySelector(".brand-mark");
    var navItems = Array.prototype.slice.call(header.querySelectorAll(".site-nav a, .nav-actions > *"));
    var collapseThreshold = 120;
    var expandThreshold = 72;
    var minDirectionDelta = 8;
    var throttleMs = 90;
    var lastScrollY = window.scrollY || 0;
    var lastKnownScrollY = lastScrollY;
    var lastRunAt = 0;
    var ticking = false;

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
    }

    function setItemDelays() {
        var total = navItems.length;
        navItems.forEach(function (item, index) {
            item.style.setProperty("--nav-order", String(index));
            item.style.setProperty("--collapse-delay", (index * 28) + "ms");
            item.style.setProperty("--expand-delay", ((total - index - 1) * 20) + "ms");
        });
    }

    function expandNavbar() {
        header.classList.remove("navbar-collapsed");
    }

    function collapseNavbar() {
        header.classList.add("navbar-collapsed");
    }

    function processScroll() {
        var currentScrollY = lastKnownScrollY;
        var delta = currentScrollY - lastScrollY;
        var nearTop = currentScrollY <= expandThreshold;
        var shouldCollapse = currentScrollY > collapseThreshold && delta > minDirectionDelta;
        var shouldExpand = nearTop || delta < -minDirectionDelta;

        if (shouldExpand) {
            expandNavbar();
        } else if (shouldCollapse) {
            collapseNavbar();
        }

        lastScrollY = currentScrollY;
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
            requestScrollUpdate();
        });
    });

    if (logo) {
        logo.addEventListener("click", function (event) {
            if (header.classList.contains("navbar-collapsed")) {
                event.preventDefault();
                event.stopPropagation();
                expandNavbar();
                return;
            }
        });

        logo.addEventListener("touchstart", function () {
            if (header.classList.contains("navbar-collapsed")) {
                expandNavbar();
            }
        }, { passive: true });

        logo.addEventListener("keydown", function (event) {
            if (header.classList.contains("navbar-collapsed") && (event.key === "Enter" || event.key === " ")) {
                event.preventDefault();
                expandNavbar();
            }
        });
    }

    setItemDelays();
    setAnimationMetrics();
    requestScrollUpdate();
});
