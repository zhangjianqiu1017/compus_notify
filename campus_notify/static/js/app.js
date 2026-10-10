/* 轻量前端脚本：删除二次确认 + 批量勾选 + 提醒轮询 + 导入快捷键 */
(function () {
  "use strict";

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
        window.alert("请先勾选要删除的事项。");
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
