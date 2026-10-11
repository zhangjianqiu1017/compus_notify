/* 轻量前端脚本：轻提示 + 删除二次确认 + 批量勾选 + 提醒轮询 + 导入快捷键 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- 轻提示
  // 服务端渲染的提示都落在 [data-toast-stack] 里，这里只负责自动消失、手动关闭
  // 和位置对齐。提示本身是 position: fixed，不占文档流，不会把页面内容挤下去。
  const TOAST_DURATION = {
    "toast-success": 4500,
    "toast-info": 5200,
    "toast-warning": 7500,
    "toast-error": 0, // 错误留着，等用户自己关掉
  };
  const TOAST_DEFAULT_DURATION = 5000;
  const TOAST_MAX = 4; // 同时最多叠 4 条，再多就把最早的收掉

  function toastKind(toast) {
    const names = Array.prototype.slice.call(toast.classList);
    for (let i = 0; i < names.length; i += 1) {
      if (names[i].indexOf("toast-") === 0) return names[i];
    }
    return "toast-info";
  }

  function toastStack() {
    let stack = document.querySelector("[data-toast-stack]");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "toast-stack";
      stack.setAttribute("data-toast-stack", "");
      stack.setAttribute("aria-live", "polite");
      document.body.appendChild(stack);
    }
    return stack;
  }

  function dismissToast(toast) {
    if (toast.dataset.leaving) return;
    toast.dataset.leaving = "1";
    toast.classList.add("is-leaving");
    let removed = false;
    const remove = function () {
      if (removed) return;
      removed = true;
      toast.remove();
    };
    toast.addEventListener("animationend", remove);
    // 兜底：动画被 prefers-reduced-motion 关掉时不会触发 animationend
    window.setTimeout(remove, 320);
  }

  function armToast(toast) {
    if (toast.dataset.armed) return;
    toast.dataset.armed = "1";

    let duration = parseInt(toast.getAttribute("data-toast-duration") || "", 10);
    if (isNaN(duration)) {
      duration = toast.hasAttribute("data-toast-sticky")
        ? 0
        : TOAST_DURATION[toastKind(toast)];
    }
    if (duration === undefined) duration = TOAST_DEFAULT_DURATION;
    if (!duration || duration <= 0) return;

    let timer = null;
    let startedAt = 0;
    let remaining = duration;
    const start = function () {
      startedAt = Date.now();
      timer = window.setTimeout(function () { dismissToast(toast); }, remaining);
    };
    const pause = function () {
      if (!timer) return;
      window.clearTimeout(timer);
      timer = null;
      remaining = Math.max(800, remaining - (Date.now() - startedAt));
    };
    // 鼠标停留或键盘聚焦时停下倒计时，免得用户正要看/点就被收走
    toast.addEventListener("mouseenter", pause);
    toast.addEventListener("mouseleave", start);
    toast.addEventListener("focusin", pause);
    toast.addEventListener("focusout", start);
    start();
  }

  function trimToasts(stack) {
    const toasts = stack.querySelectorAll("[data-toast]");
    for (let i = 0; i < toasts.length - TOAST_MAX; i += 1) dismissToast(toasts[i]);
  }

  function showToast(text, kind) {
    const toast = document.createElement("div");
    toast.className = "toast toast-" + (kind || "info");
    toast.setAttribute("data-toast", "");

    const content = document.createElement("span");
    content.className = "toast-text";
    content.textContent = text;
    toast.appendChild(content);

    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("data-toast-close", "");
    close.setAttribute("aria-label", "关闭提示");
    close.textContent = "✕";
    toast.appendChild(close);

    const stack = toastStack();
    stack.appendChild(toast);
    trimToasts(stack);
    armToast(toast);
    return toast;
  }

  const toastRoot = toastStack();
  // 提示分散在两个容器里（右上角一次性提示、右下角常驻提示），都要接管
  Array.prototype.slice.call(document.querySelectorAll("[data-toast]")).forEach(armToast);

  document.addEventListener("click", function (event) {
    const button = event.target.closest("[data-toast-close]");
    if (!button) return;
    const toast = button.closest("[data-toast]");
    if (toast) dismissToast(toast);
  });

  // 让开吸顶导航：顶栏在窄屏会折行，高度只能实测
  const topbar = document.querySelector(".topbar");
  if (topbar) {
    const alignToasts = function () {
      toastRoot.style.setProperty("--toast-top", topbar.offsetHeight + 12 + "px");
    };
    alignToasts();
    window.addEventListener("resize", alignToasts);
  }

  // ---------------------------------------------------------------- 二次确认
  // data-confirm="文案"：点击时按文案确认
  // data-confirm-any：按当前勾选条数生成确认文案（用于批量删除）
  document.addEventListener("click", function (event) {
    const target = event.target.closest("[data-confirm], [data-confirm-any]");
    if (!target) return;

    let prompt = target.getAttribute("data-confirm") || "";
    if (target.hasAttribute("data-confirm-any")) {
      const count = document.querySelectorAll("[data-row-checkbox]:checked").length;
      if (!count) {
        event.preventDefault();
        event.stopPropagation();
        showToast("请先勾选要删除的事项。", "warning");
        return;
      }
      prompt = "确定删除选中的 " + count + " 条吗？此操作不可恢复。";
    }

    if (!window.confirm(prompt)) {
      event.preventDefault();
      event.stopPropagation();
    }
  });

  // ---------------------------------------------------------------- 批量勾选
  const selectAll = document.querySelector("[data-select-all]");
  const rowBoxes = Array.prototype.slice.call(document.querySelectorAll("[data-row-checkbox]"));
  const counter = document.querySelector("[data-selected-count]");

  function refreshSelection() {
    const checked = rowBoxes.filter(function (box) { return box.checked; }).length;
    if (counter) {
      counter.textContent = checked ? "已选择 " + checked + " 条" : "未选择";
    }
    if (selectAll) {
      selectAll.checked = checked > 0 && checked === rowBoxes.length;
      selectAll.indeterminate = checked > 0 && checked < rowBoxes.length;
    }
  }

  if (selectAll && rowBoxes.length) {
    selectAll.addEventListener("change", function () {
      rowBoxes.forEach(function (box) { box.checked = selectAll.checked; });
      refreshSelection();
    });
    rowBoxes.forEach(function (box) { box.addEventListener("change", refreshSelection); });
    refreshSelection();
  }

  // ---------------------------------------------------------------- 回车键语义
  // 表单里「发送验证码」是第一个提交按钮，浏览器默认会用它在密码框里回车时提交，
  // 结果用户填完密码一按回车，密码被清空、还多发一条验证码。
  // 这里把密码框的回车钉到主按钮上。
  document.querySelectorAll("form[data-primary-submit]").forEach(function (form) {
    const primary = form.querySelector("[data-primary-submit-button]");
    if (!primary) return;
    form.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      const target = event.target;
      if (!target.matches('input[type="password"]')) return;
      event.preventDefault();
      if (typeof form.requestSubmit === "function") {
        form.requestSubmit(primary);
      } else {
        form.submit();
      }
    });
  });

  // ---------------------------------------------------------------- 验证码倒计时
  // 服务端会把「还剩多少秒才能重发」渲染进 data-resend-in，这里负责倒计时与按钮状态。
  // 用服务端值做初值，刷新页面也绕不过冷却。
  document.querySelectorAll("[data-resend-button]").forEach(function (button) {
    const idleLabel = button.getAttribute("data-idle-label") || "发送验证码";
    const sentTo = (button.getAttribute("data-resend-email") || "").toLowerCase();
    const form = button.form;
    const emailInput = form ? form.querySelector('input[name="email"]') : null;
    let remaining = parseInt(button.getAttribute("data-resend-in") || "0", 10);
    if (isNaN(remaining)) remaining = 0;
    let timer = null;

    function render() {
      if (remaining > 0) {
        button.disabled = true;
        button.textContent = "重新发送 " + remaining + "s";
      } else {
        button.disabled = false;
        button.textContent = idleLabel;
      }
    }

    function stop() {
      if (timer) window.clearInterval(timer);
      timer = null;
      remaining = 0;
      render();
    }

    render();
    if (remaining > 0) {
      timer = window.setInterval(function () {
        remaining -= 1;
        if (remaining <= 0) {
          stop();
        } else {
          render();
        }
      }, 1000);
    }

    // 用户把邮箱改成另一个地址时，冷却不再适用于当前输入，立即放开按钮
    if (emailInput && sentTo) {
      emailInput.addEventListener("input", function () {
        if (timer && emailInput.value.trim().toLowerCase() !== sentTo) stop();
      });
    }
  });

  // ---------------------------------------------------------------- 导入快捷键
  const importForm = document.querySelector("form[data-submit-hotkey]");
  if (importForm) {
    const area = importForm.querySelector("textarea");
    if (area) {
      area.addEventListener("keydown", function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
          importForm.submit();
        }
      });
    }
  }

  // ---------------------------------------------------------------- 提醒轮询
  const badge = document.querySelector("[data-reminder-poll]");
  if (badge) {
    const poll = function () {
      fetch("/api/notifications/", { headers: { "X-Requested-With": "fetch" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data) return;
          badge.textContent = data.unread > 0 ? data.unread : "";
          badge.style.display = data.unread > 0 ? "" : "none";
        })
        .catch(function () { /* 离线时静默失败，下次轮询再试 */ });
    };
    window.setInterval(poll, 60000);
  }
})();
