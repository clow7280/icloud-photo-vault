(() => {
  // 提交表单后禁用按钮并显示处理中，避免看起来没有反应或被重复提交
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    const button = event.submitter || form.querySelector("button");
    if (!button) return;
    button.dataset.label = button.textContent;
    button.innerHTML = '<span class="spinner"></span>';
    button.append(form.dataset.busy || "处理中…");
    // 推迟禁用，确保按钮本身的值随表单提交
    setTimeout(() => { button.disabled = true; });
  });

  // 从浏览器缓存返回本页时恢复按钮
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    document.querySelectorAll("button[data-label]").forEach((button) => {
      button.disabled = false;
      button.textContent = button.dataset.label;
    });
  });

  // Esc 关闭对话框，效果与点右上角的关闭按钮相同
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const close = document.querySelector(".modal [data-close]");
    if (close) close.click();
  });

  // 空间清理：勾选月份时汇总可删除的张数和大小，按住 Shift 连续选择
  const picker = document.getElementById("cleanup-select");
  if (picker) {
    const boxes = [...picker.querySelectorAll('input[name="months"]:not(:disabled)')];
    const all = document.getElementById("pick-all");
    const summary = document.getElementById("selection");
    const button = document.getElementById("review-button");
    let last = null;

    const bytes = (value) => {
      const units = ["B", "KB", "MB", "GB", "TB"];
      let size = value;
      let unit = 0;
      while (size >= 1024 && unit < units.length - 1) {
        size /= 1024;
        unit += 1;
      }
      return unit === 0 ? `${size} B` : `${size.toFixed(1)} ${units[unit]}`;
    };

    const update = () => {
      const picked = boxes.filter((box) => box.checked);
      const count = picked.reduce((sum, box) => sum + Number(box.dataset.count), 0);
      const size = picked.reduce((sum, box) => sum + Number(box.dataset.bytes), 0);
      summary.textContent = picked.length
        ? `已选 ${picked.length} 个月，可删除 ${count} 张，约 ${bytes(size)}`
        : "未选择";
      button.disabled = picked.length === 0;
      if (all) {
        all.checked = picked.length > 0 && picked.length === boxes.length;
        all.indeterminate = picked.length > 0 && picked.length < boxes.length;
      }
    };

    boxes.forEach((box) => {
      box.addEventListener("click", (event) => {
        if (event.shiftKey && last !== null) {
          const [from, to] = [boxes.indexOf(last), boxes.indexOf(box)].sort((a, b) => a - b);
          boxes.slice(from, to + 1).forEach((b) => { b.checked = box.checked; });
        }
        last = box;
        update();
      });
    });
    // 点行的任意位置都可以勾选
    picker.querySelectorAll("tbody tr").forEach((row) => {
      row.addEventListener("click", (event) => {
        const box = row.querySelector('input[name="months"]');
        if (!box || box.disabled || event.target === box) return;
        box.dispatchEvent(new MouseEvent("click", { shiftKey: event.shiftKey }));
      });
    });
    if (all) {
      all.addEventListener("change", () => {
        boxes.forEach((box) => { box.checked = all.checked; });
        update();
      });
    }
    update();
  }

  // 只更新底部状态栏，不刷新整个页面：轮询服务端渲染好的状态栏片段，整块替换
  const url = document.body.dataset.statusbarUrl;
  if (!url || !document.getElementById("statusbar")) return;
  const isRunning = () => document.getElementById("statusbar").dataset.running === "1";
  // 本页打开期间有任务结束，就一直提示刷新。多个任务排队时，每结束一个都算
  const finishedAtLoad = document.getElementById("statusbar").dataset.finished;

  async function poll() {
    try {
      const response = await fetch(url, { cache: "no-store", credentials: "same-origin" });
      if (response.ok) {
        const template = document.createElement("template");
        template.innerHTML = (await response.text()).trim();
        const bar = template.content.firstElementChild;
        if (bar && bar.id === "statusbar") {
          document.getElementById("statusbar").replaceWith(bar);
          if (bar.dataset.finished !== finishedAtLoad) {
            const link = document.createElement("a");
            link.href = "#";
            link.className = "sb-refresh";
            link.textContent = "刷新查看最新数据";
            link.addEventListener("click", (event) => {
              event.preventDefault();
              window.location.reload();
            });
            bar.querySelector(".sb-task").append(link);
          }
        }
      }
    } catch (_) {
      // 网络波动时下一轮再试
    }
    setTimeout(poll, isRunning() ? 2000 : 15000);
  }

  setTimeout(poll, isRunning() ? 2000 : 15000);
})();
