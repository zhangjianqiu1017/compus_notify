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

  // 底部 Tab 栏同样是实测高度：它含 env(safe-area-inset-bottom) 的内边距，
  // CSS 里的兜底值不一定准，算错了要么挡住最后一行内容、要么提示压在栏上。
  const tabbar = document.querySelector(".tabbar");
  if (tabbar) {
    const syncTabbarHeight = function () {
      document.documentElement.style.setProperty("--tabbar-h", tabbar.offsetHeight + "px");
    };
    syncTabbarHeight();
    window.addEventListener("resize", syncTabbarHeight);
    window.addEventListener("orientationchange", syncTabbarHeight);
  }

  // ---------------------------------------------------------------- 日历空格子
  // 窄屏每个日期格子上盖了一层链接（整格可点，见 calendar.html）——但那只在
  // 「这天有事」时才渲染。没事的格子点了不能毫无反应，否则像是界面卡住了：
  // 不跳转，只出一条轻提示。
  document.addEventListener("click", function (event) {
    const cell = event.target.closest(".calendar td.cal-cell");
    if (!cell || cell.classList.contains("has-events")) return;
    showToast("这一天没有待办。", "info");
  });

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
  // 「全选」有两个：桌面在表头，手机在工具栏（表头在窄屏是隐藏的），两处都要同步
  const selectAlls = Array.prototype.slice.call(document.querySelectorAll("[data-select-all]"));
  const rowBoxes = Array.prototype.slice.call(document.querySelectorAll("[data-row-checkbox]"));
  const counter = document.querySelector("[data-selected-count]");

  function refreshSelection() {
    const checked = rowBoxes.filter(function (box) { return box.checked; }).length;
    if (counter) {
      counter.textContent = checked ? "已选择 " + checked + " 条" : "未选择";
    }
    selectAlls.forEach(function (box) {
      box.checked = checked > 0 && checked === rowBoxes.length;
      box.indeterminate = checked > 0 && checked < rowBoxes.length;
    });
  }

  if (selectAlls.length && rowBoxes.length) {
    selectAlls.forEach(function (selectAll) {
      selectAll.addEventListener("change", function () {
        rowBoxes.forEach(function (box) { box.checked = selectAll.checked; });
        refreshSelection();
      });
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

  // ---------------------------------------------------------------- 等待态
  // 凡是点了要等服务端的按钮，都得当场给出「在处理」的反馈，否则用户以为点空了，
  // 接着连点第二下——注册会报「验证码已用过」，识别会重复建事项。
  // 三档反馈，按等待时长递进：
  //   1. 按钮里的 spinner：任何 .btn / .input-group-action 提交按钮自动获得；
  //   2. data-loading-label：把按钮文字换成「正在识别…」，说清在等什么；
  //   3. data-busy-title / data-busy-tips：再盖一层全屏遮罩（发邮件、识别这种要等几秒的）；
  //      写在表单上对所有按钮生效，写在某个按钮上只对该按钮生效。
  // 请求结束页面就跳走了，spinner 随之消失；只有浏览器「后退」把页面从 bfcache
  // 里原样捞回来时，才会看到残留的转圈，所以 pageshow 时统一清一遍。
  const BUSY_SELECTOR = ".btn, .input-group-action, [data-loading-label]";
  const OVERLAY_DELAY = 240; // 快请求（保存一条事项）不闪遮罩，过了这个点才浮出来
  const TIP_INTERVAL = 2600;
  const SLOW_HINT_DELAY = 15000; // 等这么久还没走，说明真慢，补一句「别急着关」

  const busyStates = new WeakMap();
  let busyNodes = [];
  let overlay = null;
  let overlayTimer = null;
  let tipTimer = null;
  let slowTimer = null;

  function makeSpinner(extraClass) {
    const spinner = document.createElement("span");
    spinner.className = "spinner" + (extraClass ? " " + extraClass : "");
    spinner.setAttribute("aria-hidden", "true");
    return spinner;
  }

  // data-busy-tips 用半角逗号分隔（中文文案里的「，」不算分隔符）
  function splitTips(raw) {
    return (raw || "")
      .split(",")
      .map(function (item) { return item.trim(); })
      .filter(function (item) { return item.length > 0; });
  }

  function enterButtonBusy(button) {
    if (!button || busyStates.has(button)) return;
    const label = (button.getAttribute("data-loading-label") || "").trim();
    const width = Math.ceil(button.getBoundingClientRect().width);
    busyStates.set(button, { html: button.innerHTML, width: width });
    busyNodes.push(button);

    button.setAttribute("data-busy", "");
    button.setAttribute("aria-busy", "true");
    // 锁住宽度：「开始识别」换成「正在识别…」时按钮不会忽宽忽窄，旁边的按钮也不跳
    if (width > 0) button.style.minWidth = width + "px";

    // 整块换掉（图标位正好给 spinner 用），原文案留着，恢复时原样放回
    button.textContent = "";
    button.appendChild(makeSpinner());
    if (label) {
      const text = document.createElement("span");
      text.className = "btn-busy-text";
      text.textContent = label;
      button.appendChild(text);
    }
  }

  function openOverlay(title, tips) {
    if (overlay) return;

    overlay = document.createElement("div");
    overlay.className = "busy-overlay";
    overlay.setAttribute("role", "status");
    overlay.setAttribute("aria-live", "polite");

    const bar = document.createElement("div");
    bar.className = "busy-bar";
    bar.setAttribute("aria-hidden", "true");
    overlay.appendChild(bar);

    const card = document.createElement("div");
    card.className = "busy-card";
    card.appendChild(makeSpinner("spinner-lg"));

    const heading = document.createElement("p");
    heading.className = "busy-title";
    heading.textContent = title;
    card.appendChild(heading);

    if (tips.length) {
      const tip = document.createElement("p");
      tip.className = "busy-tip";
      tip.textContent = tips[0];
      card.appendChild(tip);
      let index = 0;
      tipTimer = window.setInterval(function () {
        index = (index + 1) % tips.length;
        tip.classList.add("is-fading");
        window.setTimeout(function () {
          tip.textContent = tips[index];
          tip.classList.remove("is-fading");
        }, 250);
      }, TIP_INTERVAL);
    }

    overlay.appendChild(card);

    // 等服务端等过了头（网络差、识别排队）时补一句，免得用户以为页面卡死就关掉
    slowTimer = window.setTimeout(function () {
      slowTimer = null;
      if (!overlay || card.querySelector(".busy-slow")) return;
      const slow = document.createElement("p");
      slow.className = "busy-slow";
      slow.textContent = "还在处理，网络慢的时候会久一点，先别关掉页面。";
      card.appendChild(slow);
    }, SLOW_HINT_DELAY);

    document.body.appendChild(overlay);
  }

  function closeOverlay() {
    if (overlayTimer) {
      window.clearTimeout(overlayTimer);
      overlayTimer = null;
    }
    if (tipTimer) {
      window.clearInterval(tipTimer);
      tipTimer = null;
    }
    if (slowTimer) {
      window.clearTimeout(slowTimer);
      slowTimer = null;
    }
    if (!overlay) return;
    overlay.remove();
    overlay = null;
  }

  // 把页面恢复成「没在等」的样子：后退回本页、或请求压根没发出去时都靠它兜底
  function resetBusy() {
    closeOverlay();
    busyNodes.forEach(function (button) {
      const state = busyStates.get(button);
      if (!state) return;
      busyStates.delete(button);
      button.innerHTML = state.html;
      button.removeAttribute("data-busy");
      button.removeAttribute("aria-busy");
      button.style.minWidth = "";
    });
    busyNodes = [];
    Array.prototype.slice.call(document.querySelectorAll("form[data-submitting]")).forEach(function (form) {
      delete form.dataset.submitting;
    });
  }

  document.addEventListener("submit", function (event) {
    const form = event.target;
    if (!form || form.tagName !== "FORM") return;

    // 已经发过一次就挡住第二次：连点两下只该产生一个请求
    if (form.dataset.submitting) {
      event.preventDefault();
      return;
    }

    const submitter =
      (event.submitter && event.submitter.matches && event.submitter) ||
      form.querySelector('[type="submit"]');
    if (!submitter || !submitter.matches(BUSY_SELECTOR)) return;

    form.dataset.submitting = "1";
    enterButtonBusy(submitter);

    // 按钮上的说明比表单上的更具体（同一张注册表单里，「发送验证码」和
    // 「注册并登录」等的东西不一样），所以按钮优先
    const title = (
      submitter.getAttribute("data-busy-title") ||
      form.getAttribute("data-busy-title") ||
      ""
    ).trim();
    if (!title) return;

    const tips = splitTips(
      submitter.getAttribute("data-busy-tips") || form.getAttribute("data-busy-tips")
    );
    overlayTimer = window.setTimeout(function () {
      overlayTimer = null;
      openOverlay(title, tips);
    }, OVERLAY_DELAY);
  });

  // 后退（bfcache）恢复的是离开前那份 DOM：不清掉的话页面会一直转圈、
  // 按钮也一直是禁用的，看起来像卡死
  window.addEventListener("pageshow", resetBusy);

  // ---------------------------------------------------------------- 导入快捷键
  const importForm = document.querySelector("form[data-submit-hotkey]");
  if (importForm) {
    const area = importForm.querySelector("textarea");
    if (area) {
      area.addEventListener("keydown", function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
          // 走 requestSubmit 而不是 submit：submit() 不触发 submit 事件，
          // 等待态就不会亮起来，用户按完快捷键只觉得「没反应」
          if (typeof importForm.requestSubmit === "function") {
            importForm.requestSubmit();
          } else {
            importForm.submit();
          }
        }
      });

      // 字数统计：粘贴前先看一眼会不会太长，比提交后才知道友好
      const charCount = document.querySelector("[data-char-count]");
      if (charCount) {
        const syncCount = function () {
          charCount.textContent = area.value.length;
        };
        area.addEventListener("input", syncCount);
        syncCount();
      }
    }
  }

  // ---------------------------------------------------------------- 提醒轮询
  // 桌面顶栏和手机顶栏各有一枚角标（同一时刻只有一个可见），两枚一起更新
  const badges = Array.prototype.slice.call(document.querySelectorAll("[data-reminder-poll]"));
  if (badges.length) {
    const poll = function () {
      fetch("/api/notifications/", { headers: { "X-Requested-With": "fetch" } })
        .then(function (response) { return response.ok ? response.json() : null; })
        .then(function (data) {
          if (!data) return;
          badges.forEach(function (badge) {
            badge.textContent = data.unread > 0 ? data.unread : "";
            badge.style.display = data.unread > 0 ? "" : "none";
          });
        })
        .catch(function () { /* 离线时静默失败，下次轮询再试 */ });
    };
    window.setInterval(poll, 60000);
  }
})();
