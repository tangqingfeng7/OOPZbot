(function () {
  const NAV_ITEMS = [
    { key: "dashboard", href: "/admin", label: "总览" },
    { key: "music", href: "/admin/music", label: "音乐" },
    { key: "config", href: "/admin/config", label: "配置" },
    { key: "stats", href: "/admin/stats", label: "统计" },
    { key: "activity", href: "/admin/activity", label: "活跃" },
    { key: "scheduler", href: "/admin/scheduler", label: "定时" },
    { key: "members", href: "/admin/members", label: "成员" },
    { key: "areas", href: "/admin/areas", label: "域管理" },
    { key: "plugins", href: "/admin/plugins", label: "插件" },
    { key: "setup", href: "/admin/setup", label: "体检" },
    { key: "system", href: "/admin/system", label: "系统" },
  ];

  function byId(target) {
    if (!target) {
      return null;
    }
    return typeof target === "string" ? document.getElementById(target) : target;
  }

  function prefersReducedMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  async function req(url, options) {
    const response = await fetch(url, {
      credentials: "include",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...((options && options.headers) || {}),
      },
      ...(options || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) {
      const detail = Array.isArray(data.errors) && data.errors.length > 0
        ? data.errors.join(" | ")
        : "";
      throw new Error(data.error || detail || ("HTTP " + response.status));
    }
    return data;
  }

  function showMessage(target, text, isError) {
    const element = byId(target);
    if (!element) {
      return;
    }
    element.textContent = text || "";
    element.classList.toggle("is-error", !!isError);
    element.classList.toggle("is-success", !!text && !isError);
  }

  function setStatus(text, variant, target) {
    const element = byId(target || "topStatus");
    if (!element) {
      return;
    }
    const kind = variant === "error" ? "danger" : (variant || "neutral");
    element.textContent = text || "";
    element.className = "status-text";
    element.classList.add("is-" + kind);
  }

  function setMicroStatus(text, variant, target) {
    const element = byId(target);
    if (!element) {
      return;
    }
    const kind = variant === "error" ? "danger" : (variant || "neutral");
    element.textContent = text || "";
    element.className = "micro-status";
    element.classList.add("is-" + kind);
  }

  function formatNumber(value, finalValue) {
    const current = Number(value);
    const finalNumber = Number(finalValue ?? value);
    const integerMode = Number.isInteger(finalNumber);
    return new Intl.NumberFormat("zh-CN", {
      maximumFractionDigits: integerMode ? 0 : 2,
    }).format(current);
  }

  function animateNumber(target, nextValue) {
    const element = byId(target);
    if (!element) {
      return;
    }
    const parsed = Number(nextValue);
    if (!Number.isFinite(parsed)) {
      element.textContent = nextValue ?? "-";
      delete element.dataset.value;
      return;
    }
    const current = Number(element.dataset.value);
    element.dataset.value = String(parsed);
    if (!window.gsap || prefersReducedMotion() || !Number.isFinite(current)) {
      element.textContent = formatNumber(parsed);
      return;
    }
    const state = { value: current };
    window.gsap.to(state, {
      value: parsed,
      duration: 0.55,
      ease: "power2.out",
      onUpdate() {
        element.textContent = formatNumber(state.value, parsed);
      },
      onComplete() {
        element.textContent = formatNumber(parsed);
      },
    });
  }

  function flashUpdate(target) {
    const element = byId(target);
    if (!element || !window.gsap || prefersReducedMotion()) {
      return;
    }
    window.gsap.fromTo(
      element,
      { opacity: 0.55 },
      { opacity: 1, duration: 0.3, ease: "power2.out" }
    );
  }

  function getNavMarkup(currentPage) {
    return (
      '<div class="top-nav__marker"></div>' +
      NAV_ITEMS.map((item) => {
        const activeClass = item.key === currentPage ? " is-active" : "";
        const current = item.key === currentPage ? ' aria-current="page"' : "";
        return `<a class="nav-link${activeClass}" href="${item.href}" data-nav-key="${item.key}"${current}>${item.label}</a>`;
      }).join("")
    );
  }

  function updateNavMarker(container) {
    if (!container) {
      return;
    }
    const marker = container.querySelector(".top-nav__marker");
    const active = container.querySelector(".nav-link.is-active");
    if (!marker || !active) {
      return;
    }
    const left = active.offsetLeft;
    const width = active.offsetWidth;
    if (!window.gsap || prefersReducedMotion()) {
      marker.style.opacity = "1";
      marker.style.transform = "translateX(" + left + "px)";
      marker.style.width = width + "px";
      return;
    }
    window.gsap.to(marker, {
      x: left,
      width,
      opacity: 1,
      duration: 0.32,
      ease: "power2.out",
    });
  }

  function renderNav(currentPage) {
    const desktop = byId("topNav");
    const mobile = byId("mobileNav");
    const markup = getNavMarkup(currentPage);
    if (desktop) {
      desktop.innerHTML = markup;
    }
    if (mobile) {
      mobile.innerHTML = markup;
    }
    requestAnimationFrame(() => {
      updateNavMarker(desktop);
      updateNavMarker(mobile);
    });
  }

  function animateEnter() {
    if (!window.gsap || prefersReducedMotion()) {
      return;
    }
    var topbar = document.querySelector(".shell-topbar");
    var mobileNav = document.querySelector(".mobile-nav-shell");
    var hero = document.querySelector(".page-hero");
    var cards = document.querySelectorAll(
      ".auth-card, .surface-card, .metric-card, .summary-card, .table-card, .console-card, .sticky-rail"
    );
    var timeline = window.gsap.timeline({ defaults: { ease: "power3.out" } });

    if (topbar) {
      timeline.from(topbar, {
        y: -16,
        opacity: 0,
        scale: 0.98,
        duration: 0.45,
        clearProps: "transform,opacity",
      });
    }
    if (mobileNav) {
      timeline.from(
        mobileNav,
        {
          y: -10,
          opacity: 0,
          duration: 0.3,
          clearProps: "transform,opacity",
        },
        "-=0.25"
      );
    }
    if (hero) {
      timeline.from(
        hero,
        {
          y: 16,
          opacity: 0,
          scale: 0.98,
          duration: 0.35,
          clearProps: "transform,opacity",
        },
        "-=0.15"
      );
    }
    if (cards.length) {
      timeline.from(
        cards,
        {
          y: 20,
          opacity: 0,
          scale: 0.97,
          duration: 0.35,
          stagger: 0.05,
          clearProps: "transform,opacity",
        },
        "-=0.1"
      );
    }
  }

  function animatePanel(target) {
    const root = byId(target);
    if (!root || !window.gsap || prefersReducedMotion()) {
      return;
    }
    const cards = root.querySelectorAll(
      ".surface-card, .metric-card, .summary-card, .table-card, .console-card, .sticky-rail"
    );
    if (!cards.length) {
      return;
    }
    window.gsap.fromTo(
      cards,
      { y: 18, opacity: 0, scale: 0.97 },
      {
        y: 0,
        opacity: 1,
        scale: 1,
        duration: 0.35,
        stagger: 0.04,
        ease: "power2.out",
        clearProps: "transform,opacity",
      }
    );
  }

  function setAuthState(options) {
    const settings = {
      loginId: "loginCard",
      panelId: "panel",
      loggedInText: "已登录",
      loggedOutText: "等待登录",
      statusTargets: ["topStatus", "mobileStatus"],
      ...(options || {}),
    };
    const loginCard = byId(settings.loginId);
    const panel = byId(settings.panelId);

    if (loginCard) {
      loginCard.classList.toggle("hidden", !!settings.loggedIn);
    }
    if (panel) {
      panel.classList.toggle("hidden", !settings.loggedIn);
    }

    const text = settings.loggedIn ? settings.loggedInText : settings.loggedOutText;
    const variant = settings.loggedIn ? "success" : "warning";
    (settings.statusTargets || []).forEach((target) => {
      setStatus(text, variant, target);
    });

    if (settings.loggedIn && panel) {
      animatePanel(panel);
    }
  }

  function bindEnterSubmit(inputId, handler) {
    const input = byId(inputId);
    if (!input || input.dataset.enterBound === "1") {
      return;
    }
    input.dataset.enterBound = "1";
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") {
        return;
      }
      event.preventDefault();
      handler();
    });
  }

  async function copyText(value) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(value);
      return;
    }
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "readonly");
    textarea.style.position = "absolute";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    document.body.removeChild(textarea);
  }

  function init(options) {
    const settings = options || {};
    const currentPage = settings.page || document.body.dataset.adminPage || "";
    renderNav(currentPage);
    animateEnter();
    if (settings.passwordHandler) {
      bindEnterSubmit(settings.passwordId || "pwd", settings.passwordHandler);
    }
    const refreshMarker = () => {
      updateNavMarker(byId("topNav"));
      updateNavMarker(byId("mobileNav"));
    };
    window.addEventListener("resize", refreshMarker);
    window.addEventListener("load", refreshMarker);
  }

  // -------------------------------------------------------------------------
  // Custom Select (replaces native <select> with styled dropdown)
  // -------------------------------------------------------------------------

  var _csInstances = new Map();

  function _csClose(wrap) {
    wrap.classList.remove("is-open");
    var card = wrap.closest(".surface-card, .table-card, .action-row");
    if (card) card.classList.remove("cs-elevated");
  }

  function _csCloseAll(except) {
    _csInstances.forEach(function (inst) {
      if (inst.wrap !== except) _csClose(inst.wrap);
    });
  }

  document.addEventListener("click", function (e) {
    _csInstances.forEach(function (inst) {
      if (!inst.wrap.contains(e.target)) _csClose(inst.wrap);
    });
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") _csCloseAll();
  });

  function upgradeSelect(selectId) {
    var sel = byId(selectId);
    if (!sel || sel.tagName !== "SELECT") return null;

    var existing = _csInstances.get(selectId);
    if (existing) return existing;

    var wrap = document.createElement("div");
    wrap.className = "cs-wrap";
    if (sel.classList.contains("m-area-select")) {
      wrap.style.maxWidth = "200px";
    }

    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "cs-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");

    var dropdown = document.createElement("div");
    dropdown.className = "cs-dropdown";
    dropdown.setAttribute("role", "listbox");

    sel.style.display = "none";
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(trigger);
    wrap.appendChild(dropdown);
    wrap.appendChild(sel);

    trigger.addEventListener("click", function (e) {
      e.stopPropagation();
      var isOpen = wrap.classList.contains("is-open");
      _csCloseAll();
      if (!isOpen) {
        wrap.classList.add("is-open");
        var card = wrap.closest(".surface-card, .table-card, .action-row");
        if (card) card.classList.add("cs-elevated");
        var selected = dropdown.querySelector(".is-selected");
        if (selected) selected.scrollIntoView({ block: "nearest" });
      }
    });

    function syncOptions() {
      dropdown.innerHTML = "";
      var opts = sel.options;
      if (!opts.length) {
        dropdown.innerHTML = '<span class="cs-empty">无选项</span>';
        trigger.textContent = "请选择";
        return;
      }
      var children = sel.children;
      for (var c = 0; c < children.length; c++) {
        var child = children[c];
        if (child.tagName === "OPTGROUP") {
          var groupLabel = document.createElement("div");
          groupLabel.className = "cs-group-label";
          groupLabel.textContent = child.label || "";
          dropdown.appendChild(groupLabel);
          for (var g = 0; g < child.children.length; g++) {
            _appendOption(child.children[g]);
          }
        } else if (child.tagName === "OPTION") {
          _appendOption(child);
        }
      }
      _refreshSelected();
    }

    function _appendOption(opt) {
      var idx = opt.index;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "cs-option";
      btn.setAttribute("role", "option");
      btn.textContent = opt.text;
      btn.dataset.value = opt.value;
      btn.dataset.idx = idx;
      if (idx === sel.selectedIndex) btn.classList.add("is-selected");
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        sel.selectedIndex = idx;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
        _refreshSelected();
        _csClose(wrap);
      });
      dropdown.appendChild(btn);
    }

    function _refreshSelected() {
      var idx = sel.selectedIndex;
      trigger.textContent = idx >= 0 && sel.options[idx] ? sel.options[idx].text : "请选择";
      var btns = dropdown.querySelectorAll(".cs-option");
      btns.forEach(function (b) {
        b.classList.toggle("is-selected", parseInt(b.dataset.idx, 10) === idx);
      });
    }

    var observer = new MutationObserver(function () {
      syncOptions();
    });
    observer.observe(sel, { childList: true, subtree: true, attributes: true });

    syncOptions();

    var inst = { wrap: wrap, trigger: trigger, dropdown: dropdown, sel: sel, sync: syncOptions };
    _csInstances.set(selectId, inst);
    return inst;
  }

  function refreshCustomSelect(selectId) {
    var inst = _csInstances.get(selectId);
    if (inst) inst.sync();
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  // -------------------------------------------------------------------------
  // Modal subsystem (shared dialog reused across admin pages)
  // Pages must provide #modalOverlay / #modalDialog / #modalTitle /
  // #modalBody / #modalFooter in their markup.
  // -------------------------------------------------------------------------

  let _modalCloseCb = null;

  function _modalEls() {
    return {
      overlay: byId("modalOverlay"),
      dialog: byId("modalDialog"),
      title: byId("modalTitle"),
      body: byId("modalBody"),
      footer: byId("modalFooter"),
    };
  }

  function openModal(title, bodyHtml, footerHtml, onClose) {
    const els = _modalEls();
    if (!els.dialog || !els.overlay) {
      return els;
    }
    _modalCloseCb = typeof onClose === "function" ? onClose : null;
    if (els.title) {
      els.title.textContent = title || "";
    }
    if (els.body) {
      els.body.innerHTML = bodyHtml || "";
    }
    if (els.footer) {
      els.footer.innerHTML = footerHtml || "";
    }
    els.overlay.classList.add("is-open");
    els.dialog.classList.add("is-open");
    return els;
  }

  function closeModal() {
    const els = _modalEls();
    if (els.dialog) {
      els.dialog.classList.remove("is-open");
    }
    if (els.overlay) {
      els.overlay.classList.remove("is-open");
    }
    const cb = _modalCloseCb;
    _modalCloseCb = null;
    if (cb) {
      cb();
    }
  }

  function confirm(title, message, options) {
    const opts = options || {};
    return new Promise((resolve) => {
      const els = _modalEls();
      if (!els.dialog || !els.footer) {
        resolve(false);
        return;
      }
      let settled = false;
      const done = (value) => {
        if (settled) {
          return;
        }
        settled = true;
        closeModal();
        resolve(value);
      };
      openModal(
        title,
        '<div class="m-confirm-text">' + (message || "") + "</div>",
        null,
        () => {
          if (!settled) {
            settled = true;
            resolve(false);
          }
        }
      );
      els.footer.innerHTML = "";
      const cancelBtn = document.createElement("button");
      cancelBtn.className = "btn btn-ghost";
      cancelBtn.textContent = opts.cancelText || "取消";
      cancelBtn.addEventListener("click", () => done(false));
      const okBtn = document.createElement("button");
      okBtn.className = "btn " + (opts.danger === false ? "btn-primary" : "btn-danger");
      okBtn.textContent = opts.okText || "确认";
      okBtn.addEventListener("click", () => done(true));
      els.footer.appendChild(cancelBtn);
      els.footer.appendChild(okBtn);
    });
  }

  // -------------------------------------------------------------------------
  // Action registry (data-action + event delegation)
  // Pages register handlers by action key; topbar/page buttons carry only
  // data-action so behavior stays in JS instead of inline onclick markup.
  // -------------------------------------------------------------------------

  const _actionHandlers = Object.create(null);
  // 内置动作：所有页面共用的弹窗关闭与登录/登出，免去逐页重复注册。
  // login/logout 由各页脚本定义为全局函数，点击时已就绪。
  _actionHandlers["close-modal"] = () => closeModal();
  _actionHandlers["login"] = () => {
    if (typeof window.login === "function") {
      window.login();
    }
  };
  _actionHandlers["logout"] = () => {
    if (typeof window.logout === "function") {
      window.logout();
    }
  };

  function registerActions(handlers) {
    Object.assign(_actionHandlers, handlers || {});
  }

  function _dispatch(trigger, key, event) {
    const handler = _actionHandlers[key];
    if (!handler) {
      return;
    }
    Promise.resolve()
      .then(() => handler(trigger, event))
      .catch(() => {});
  }

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-action]");
    if (!trigger) {
      return;
    }
    event.preventDefault();
    _dispatch(trigger, trigger.dataset.action, event);
  });

  // 委托 change 事件：select / checkbox 用 data-change 指定动作键，
  // 处理函数从入参 el 读取 .value / .checked。
  document.addEventListener("change", (event) => {
    const trigger = event.target.closest("[data-change]");
    if (!trigger) {
      return;
    }
    _dispatch(trigger, trigger.dataset.change, event);
  });

  // 委托 Enter 键：输入框用 data-enter 指定动作键，仅在按下回车时触发。
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") {
      return;
    }
    const trigger = event.target.closest("[data-enter]");
    if (!trigger) {
      return;
    }
    event.preventDefault();
    _dispatch(trigger, trigger.dataset.enter, event);
  });

  window.AdminShell = {
    animateNumber,
    animatePanel,
    byId,
    closeModal,
    confirm,
    copyText,
    escapeHtml,
    flashUpdate,
    init,
    openModal,
    refreshCustomSelect,
    registerActions,
    req,
    setAuthState,
    setMicroStatus,
    setStatus,
    showMessage,
    updateNavMarker,
    upgradeSelect,
  };
})();
