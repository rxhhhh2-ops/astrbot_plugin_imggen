(function () {
  "use strict";

  var bridge = window.AstrBotPluginView || window.AstrBotPluginPage || null;
  var $ = function (id) { return document.getElementById(id); };
  var state = { data: null, settings: null, reference: null, busy: false, stylesById: {} };
  var thumbQueue = [];
  var thumbActive = 0;
  var MAX_REFERENCE_BYTES = 10 * 1024 * 1024;

  // ---------- 基础工具 ----------

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function fmtBytes(n) {
    n = Number(n || 0);
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  function showBanner(message, kind) {
    var banner = $("banner");
    banner.textContent = message;
    banner.className = "banner" + (kind === "info" ? " info" : "");
  }

  function hideBanner() {
    $("banner").className = "banner hidden";
  }

  var toastTimer = null;
  function toast(message, kind, timeout) {
    var node = $("toast");
    node.textContent = message;
    node.className = "toast " + (kind || "");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      node.className = "toast hidden";
    }, timeout || 4200);
  }

  function setBusy(busy, text) {
    state.busy = busy;
    $("btn-generate").disabled = busy;
    $("btn-generate").textContent = busy ? "绘制中…" : "开始绘制";
    $("overlay").classList.toggle("hidden", !busy);
    $("overlay").setAttribute("aria-hidden", busy ? "false" : "true");
    if (text) $("overlay-text").textContent = text;
  }

  // ---------- 页面桥接 ----------

  function unwrap(result) {
    if (result && typeof result === "object" && result.status === "ok" && result.data !== undefined) {
      return result.data;
    }
    return result;
  }

  async function apiGet(endpoint, params) {
    if (!bridge || typeof bridge.apiGet !== "function") {
      throw new Error("当前 AstrBot 未提供插件页面桥接，请升级 AstrBot 后再试");
    }
    return unwrap(await bridge.apiGet(endpoint, params || {}));
  }

  async function apiPost(endpoint, body) {
    if (!bridge || typeof bridge.apiPost !== "function") {
      throw new Error("当前 AstrBot 未提供插件页面桥接，请升级 AstrBot 后再试");
    }
    return unwrap(await bridge.apiPost(endpoint, body || {}));
  }

  // ---------- 渲染 ----------

  function pill(label, tone, value) {
    return (
      '<span class="pill ' + (tone || "") + '"><b>' + escapeHtml(label) + "</b>" +
      (value ? "<span>" + escapeHtml(value) + "</span>" : "") +
      "</span>"
    );
  }

  function renderPills() {
    if (!state.data) return;
    var p = state.data.provider || {};
    var storage = state.data.storage || {};
    var queue = state.data.queue || {};
    var pills = [];
    pills.push(
      pill(
        p.ready ? "已就绪" : "未配置",
        p.ready ? "ok" : "warn",
        (p.kind || "") + " · " + (p.model || "-")
      )
    );
    pills.push(pill("图生图", p.edit ? "ok" : "warn", p.edit ? "可用" : "当前服务商不支持"));
    var backup = state.data.backup;
    if (backup) {
      pills.push(
        pill(
          backup.ready ? "备用就绪" : "备用未配置",
          backup.ready ? "ok" : "warn",
          (backup.kind || "") + " · " + (backup.model || "-")
        )
      );
    }
    pills.push(pill("风格", "", (state.data.styles || []).length + " 种"));
    pills.push(pill("存储", "", (storage.count || 0) + " 张 · " + fmtBytes(storage.bytes)));
    if (queue.inflight || queue.waiting) {
      pills.push(
        pill("队列", "", "进行中 " + (queue.inflight || 0) + " / 排队 " + (queue.waiting || 0))
      );
    }
    $("pills").innerHTML = pills.join("");
  }

  function renderStyles(styles) {
    var select = $("style");
    var previous = select.value;
    select.innerHTML = "";
    var auto = el("option", "", "自动识别");
    auto.value = "";
    select.appendChild(auto);

    var groups = new Map();
    (styles || []).forEach(function (style) {
      var key = style.category || "其他";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(style);
    });
    groups.forEach(function (items, category) {
      var group = document.createElement("optgroup");
      group.label = category;
      items.forEach(function (style) {
        var option = el("option", "", style.name);
        option.value = style.id;
        group.appendChild(option);
      });
      select.appendChild(group);
    });
    if (previous) select.value = previous;
    updateStyleHint();
  }

  function updateStyleHint() {
    var id = $("style").value;
    var hint = $("style-hint");
    if (!id) {
      hint.innerHTML = "自动识别：会根据描述里的关键词（如「水墨」「像素」「海报」）挑选风格。";
      return;
    }
    var style = state.stylesById[id];
    if (!style) {
      hint.textContent = "";
      return;
    }
    var bits = [
      "<strong>" + escapeHtml(style.name) + "</strong>",
      escapeHtml(style.summary || "")
    ];
    if (style.ratio) bits.push("默认比例 " + escapeHtml(style.ratio));
    if (style.aliases && style.aliases.length) {
      bits.push("别名：" + escapeHtml(style.aliases.join("、")));
    }
    var html = bits.join(" · ");
    if (style.prefix) {
      html +=
        "<br><span>模板：" + escapeHtml(style.prefix) +
        (style.negative ? "｜避免：" + escapeHtml(style.negative) : "") +
        "</span>";
    }
    hint.innerHTML = html;
  }

  function renderResults(data) {
    var grid = $("results");
    grid.innerHTML = "";
    var images = data.images || [];
    images.forEach(function (image) {
      var card = el("figure", "card");
      if (image.thumb) {
        var img = el("img");
        img.src = image.thumb;
        img.loading = "lazy";
        img.alt = "生成结果";
        card.appendChild(img);
      } else {
        card.appendChild(el("div", "thumb-loading", "无预览，可下载原图"));
      }
      var caption = el("figcaption");
      caption.appendChild(el("div", "card-title", data.style ? data.style.name : "生成结果"));
      var meta = [];
      if (image.width && image.height) meta.push(image.width + "×" + image.height);
      if (image.provider) meta.push(image.provider);
      if (image.model) meta.push(image.model);
      caption.appendChild(el("div", "card-meta", meta.join(" · ")));
      var download = el("button", "btn ghost small", "下载原图");
      download.type = "button";
      download.addEventListener("click", function () {
        downloadImage(image);
      });
      caption.appendChild(download);
      card.appendChild(caption);
      grid.appendChild(card);
    });
    if (!images.length) {
      grid.innerHTML = '<div class="empty">没有返回图片。</div>';
    }
    $("result-meta").textContent = data.style
      ? data.style.name + " · " + data.ratio + " · " + data.elapsed + "s" +
        (data.edited ? " · 图生图" : "")
      : "";
  }

  function renderHistory(items) {
    var box = $("history");
    box.innerHTML = "";
    thumbQueue = [];
    if (!items || !items.length) {
      box.innerHTML = '<div class="empty">还没有历史图片。</div>';
      return;
    }
    items.forEach(function (item) {
      var card = el("button", "history-card");
      card.type = "button";
      card.title = item.name;
      var holder = el("div", "thumb-loading", "加载中…");
      var meta = el("div", "history-meta");
      meta.appendChild(el("span", "", item.time || ""));
      meta.appendChild(el("span", "", fmtBytes(item.size)));
      card.appendChild(holder);
      card.appendChild(meta);
      card.addEventListener("click", function () {
        downloadImage(item);
      });
      box.appendChild(card);
      thumbQueue.push({ item: item, holder: holder });
    });
    pumpThumbs();
  }

  function pumpThumbs() {
    while (thumbActive < 3 && thumbQueue.length) {
      var job = thumbQueue.shift();
      thumbActive += 1;
      (async function (task) {
        try {
          var data = await apiGet("thumb", { name: task.item.name });
          if (data && data.thumb) {
            var img = el("img");
            img.src = data.thumb;
            img.alt = task.item.time || "历史图片";
            img.loading = "lazy";
            task.holder.replaceWith(img);
          } else {
            task.holder.textContent = "无预览";
          }
        } catch (err) {
          task.holder.textContent = "加载失败";
        } finally {
          thumbActive -= 1;
          pumpThumbs();
        }
      })(job);
    }
  }

  async function downloadImage(image) {
    if (!image || !image.name) return;
    if (!bridge || typeof bridge.download !== "function") {
      toast("当前版本不支持页面下载，可在服务器数据目录查看：" + image.name, "error", 7000);
      return;
    }
    try {
      var filename = String(image.name).split("/").pop();
      await bridge.download("image", { name: image.name }, filename);
      toast("已开始下载 " + filename, "ok");
    } catch (err) {
      toast("下载失败：" + (err && err.message ? err.message : err), "error", 6000);
    }
  }

  // ---------- 参考图 ----------

  function setReferenceFile(file) {
    if (!file) return;
    if (!/^image\/(png|jpeg|webp)$/.test(file.type)) {
      toast("参考图仅支持 PNG / JPEG / WebP", "error");
      $("reference").value = "";
      return;
    }
    if (file.size > MAX_REFERENCE_BYTES) {
      toast("参考图不能超过 10MB", "error");
      $("reference").value = "";
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      state.reference = { name: file.name, dataUrl: String(reader.result || "") };
      $("reference-thumb").src = state.reference.dataUrl;
      $("reference-name").textContent =
        file.name + " · " + (file.size / 1024).toFixed(0) + " KB";
      $("reference-preview").classList.remove("hidden");
    };
    reader.onerror = function () {
      toast("参考图读取失败", "error");
    };
    reader.readAsDataURL(file);
  }

  function clearReference() {
    state.reference = null;
    $("reference").value = "";
    $("reference-thumb").removeAttribute("src");
    $("reference-name").textContent = "";
    $("reference-preview").classList.add("hidden");
  }

  // ---------- 数据加载 ----------

  async function loadState() {
    var data = await apiGet("state");
    state.data = data || {};
    state.stylesById = {};
    (state.data.styles || []).forEach(function (style) {
      state.stylesById[style.id] = style;
    });

    var settings = state.data.settings || {};
    renderStyles(state.data.styles);
    renderPills();

    var counts = $("count");
    var previousCount = counts.value;
    counts.innerHTML = "";
    var max = Math.max(1, Number(settings.max_images || 1));
    for (var i = 1; i <= max; i += 1) {
      var option = el("option", "", i + " 张");
      option.value = String(i);
      counts.appendChild(option);
    }
    var wanted = previousCount && Number(previousCount) <= max
      ? previousCount
      : String(Math.min(Number(settings.default_images || 1), max));
    counts.value = wanted;

    var ratio = $("ratio");
    var preferred = settings.default_ratio || "1:1";
    var hasPreferred = Array.prototype.some.call(ratio.options, function (option) {
      return option.value === preferred;
    });
    if (hasPreferred) ratio.value = preferred;

    $("show-prompt").checked = Boolean(settings.show_prompt);
    if (state.data.history) renderHistory(state.data.history);
  }

  async function loadHistory() {
    try {
      var data = await apiGet("history", { limit: 12 });
      renderHistory(data.items || []);
    } catch (err) {
      $("history").innerHTML =
        '<div class="empty">历史加载失败：' + escapeHtml(err.message || err) + "</div>";
    }
  }

  // ---------- 设置弹层 ----------

  function optionValue(item) {
    return typeof item === "string" ? item : String(item.id || item.value || "");
  }

  function optionLabel(item) {
    if (typeof item === "string") return item;
    return String(item.label || item.name || item.id || item.value || "");
  }

  function fillSelect(select, items, value) {
    select.innerHTML = "";
    (items || []).forEach(function (item) {
      var option = el("option", "", optionLabel(item));
      option.value = optionValue(item);
      select.appendChild(option);
    });
    if (value != null && value !== "") {
      select.value = String(value);
      if (!select.value && select.options.length) select.selectedIndex = 0;
    }
  }

  function fillDatalist(list, items) {
    list.innerHTML = "";
    (items || []).forEach(function (item) {
      var option = document.createElement("option");
      option.value = optionValue(item);
      list.appendChild(option);
    });
  }

  function providerMeta(id) {
    var providers = (state.settings && state.settings.providers) || [];
    for (var i = 0; i < providers.length; i += 1) {
      if (providers[i].id === id) return providers[i];
    }
    return null;
  }

  function showSettingsError(message) {
    var box = $("settings-error");
    box.textContent = message;
    box.className = "banner";
  }

  function hideSettingsError() {
    $("settings-error").className = "banner hidden";
  }

  function updateProviderHints() {
    var meta = providerMeta($("set-provider").value);
    if (!meta) return;
    fillDatalist($("set-model-options"), meta.models || []);
    $("set-model").placeholder = meta.default_model
      ? "默认 " + meta.default_model
      : "填写模型名";
    $("set-base-url").placeholder = meta.default_base_url
      ? "默认 " + meta.default_base_url
      : "mock 不需要接口地址";
    var needsKey = meta.needs_key !== false;
    $("set-api-key").disabled = !needsKey;
    $("set-clear-key").disabled = !needsKey;
    if (!needsKey) {
      $("set-key-hint").textContent = "离线 mock 不需要 API Key。";
    } else {
      $("set-key-hint").textContent = state.settings && state.settings.has_api_key
        ? "已保存密钥：" + state.settings.api_key_masked
        : "尚未保存密钥。";
    }
  }

  function updateBackupHints() {
    var meta = providerMeta($("set-backup-provider").value);
    if (!meta) return;
    fillDatalist($("set-backup-model-options"), meta.models || []);
    $("set-backup-model").placeholder = meta.default_model
      ? "默认 " + meta.default_model
      : "填写模型名";
    $("set-backup-base-url").placeholder = meta.default_base_url
      ? "默认 " + meta.default_base_url
      : "mock 不需要接口地址";
    var needsKey = meta.needs_key !== false;
    $("set-backup-api-key").disabled = !needsKey;
    $("set-clear-backup-key").disabled = !needsKey;
    if (!needsKey) {
      $("set-backup-key-hint").textContent = "离线 mock 不需要 API Key。";
    } else {
      $("set-backup-key-hint").textContent = state.settings && state.settings.backup_has_api_key
        ? "已保存密钥：" + state.settings.backup_api_key_masked
        : "尚未保存备用密钥。";
    }
  }

  function updateBackupVisibility() {
    var enabled = $("set-backup-enabled").checked;
    var box = $("set-backup-fields");
    box.classList.toggle("disabled", !enabled);
    Array.prototype.forEach.call(box.querySelectorAll("input, select"), function (node) {
      node.disabled = !enabled;
    });
    if (enabled) updateBackupHints();
  }

  function fillSettingsForm(data) {
    var providers = data.providers || [];
    fillSelect($("set-provider"), providers, data.provider);
    fillSelect($("set-backup-provider"), providers, data.backup_provider);
    fillSelect($("set-quality"), data.quality_options || [], data.quality);
    fillSelect($("set-response-format"), data.response_formats || [], data.openai_response_format);

    fillSelect($("set-default-style"), data.styles || [], data.default_style);
    fillSelect($("set-default-ratio"), data.ratios || [], data.default_ratio);
    var counts = [];
    var max = Math.max(1, Number(data.max_images || 1));
    for (var i = 1; i <= max; i += 1) {
      counts.push({ id: String(i), label: i + " 张" });
    }
    fillSelect($("set-default-images"), counts, String(data.default_images || 1));

    $("set-model").value = data.model || "";
    $("set-base-url").value = data.base_url || "";
    $("set-api-key").value = "";
    $("set-api-key").placeholder = data.has_api_key
      ? "已保存 " + data.api_key_masked + "，留空不修改"
      : "粘贴 API Key";
    $("set-clear-key").checked = false;

    $("set-backup-enabled").checked = Boolean(data.backup_enabled);
    $("set-backup-model").value = data.backup_model || "";
    $("set-backup-base-url").value = data.backup_base_url || "";
    $("set-backup-api-key").value = "";
    $("set-backup-api-key").placeholder = data.backup_has_api_key
      ? "已保存 " + data.backup_api_key_masked + "，留空不修改"
      : "粘贴备用 API Key";
    $("set-clear-backup-key").checked = false;

    updateProviderHints();
    updateBackupVisibility();
  }

  async function openSettings() {
    hideSettingsError();
    var modal = $("settings-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    $("btn-settings-save").disabled = true;
    try {
      var data = await apiGet("settings");
      state.settings = data || {};
      fillSettingsForm(state.settings);
      $("btn-settings-save").disabled = false;
      $("set-provider").focus();
    } catch (err) {
      showSettingsError("读取设置失败：" + (err && err.message ? err.message : err));
    }
  }

  function closeSettings() {
    var modal = $("settings-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    hideSettingsError();
  }

  async function saveSettings() {
    var button = $("btn-settings-save");
    button.disabled = true;
    button.textContent = "保存中…";
    hideSettingsError();
    try {
      var data = await apiPost("save_settings", {
        provider: $("set-provider").value,
        model: $("set-model").value.trim(),
        base_url: $("set-base-url").value.trim(),
        api_key: $("set-api-key").value.trim(),
        clear_api_key: $("set-clear-key").checked,
        quality: $("set-quality").value,
        openai_response_format: $("set-response-format").value,
        backup_enabled: $("set-backup-enabled").checked,
        backup_provider: $("set-backup-provider").value,
        backup_model: $("set-backup-model").value.trim(),
        backup_base_url: $("set-backup-base-url").value.trim(),
        backup_api_key: $("set-backup-api-key").value.trim(),
        clear_backup_api_key: $("set-clear-backup-key").checked,
        default_style: $("set-default-style").value,
        default_ratio: $("set-default-ratio").value,
        default_images: Number($("set-default-images").value || 1)
      });
      if (data && data.warning) {
        toast(data.warning, "error", 9000);
      } else {
        toast(
          "设置已保存：" + (data.provider || "") + " · " + (data.model || ""),
          "ok"
        );
      }
      closeSettings();
      await loadState();
      await loadHistory();
    } catch (err) {
      showSettingsError("保存失败：" + (err && err.message ? err.message : err));
    } finally {
      button.disabled = false;
      button.textContent = "保存设置";
    }
  }

  // ---------- 动作 ----------

  async function generate() {
    if (state.busy) return;
    hideBanner();
    var prompt = $("prompt").value.trim();
    if (!prompt && !state.reference) {
      toast("先写点画面描述，或上传一张参考图", "error");
      $("prompt").focus();
      return;
    }

    var action = state.reference ? "正在改图" : "正在绘制";
    var startedAt = Date.now();
    setBusy(true, action + "…");
    var timer = setInterval(function () {
      $("overlay-text").textContent =
        action + "… " + ((Date.now() - startedAt) / 1000).toFixed(1) + "s";
    }, 100);

    try {
      var data = await apiPost("generate", {
        prompt: prompt,
        image: state.reference ? state.reference.dataUrl : "",
        style: $("style").value,
        ratio: $("ratio").value,
        count: Number($("count").value || 1),
        negative: $("negative").value.trim()
      });
      renderResults(data);
      var promptBox = $("final-prompt");
      if ($("show-prompt").checked && data.prompt) {
        promptBox.textContent = data.prompt;
        promptBox.classList.remove("hidden");
      } else {
        promptBox.textContent = "";
        promptBox.classList.add("hidden");
      }
      toast("完成：" + (data.count || 0) + " 张，用时 " + (data.elapsed || 0) + "s", "ok");
      try {
        await loadState();
      } catch (err) {
        /* 状态刷新失败不影响出图 */
      }
      loadHistory();
    } catch (err) {
      var message = err && err.message ? err.message : String(err);
      showBanner(message);
      toast(message, "error", 8000);
    } finally {
      clearInterval(timer);
      setBusy(false);
    }
  }

  async function reloadConfig() {
    try {
      var data = await apiPost("reload", {});
      toast("已重载：" + data.styles + " 种风格，" + data.providers + " 个服务商", "ok");
      await loadState();
      await loadHistory();
    } catch (err) {
      toast("重载失败：" + (err.message || err), "error", 7000);
    }
  }

  async function cleanup() {
    var button = $("btn-cleanup");
    if (!button.dataset.armed) {
      button.dataset.armed = "1";
      button.textContent = "再点一次确认清理";
      setTimeout(function () {
        delete button.dataset.armed;
        button.textContent = "清理旧图";
      }, 4000);
      return;
    }
    delete button.dataset.armed;
    button.textContent = "清理旧图";
    try {
      var data = await apiPost("cleanup", {});
      toast(
        "已删除 " + (data.deleted || 0) + " 张，剩余 " + (data.remaining || 0) + " 张",
        "ok"
      );
      await loadState();
      loadHistory();
    } catch (err) {
      toast("清理失败：" + (err.message || err), "error", 7000);
    }
  }

  // ---------- 初始化 ----------

  function bindEvents() {
    $("btn-generate").addEventListener("click", generate);
    $("btn-settings").addEventListener("click", openSettings);
    $("btn-settings-close").addEventListener("click", closeSettings);
    $("btn-settings-cancel").addEventListener("click", closeSettings);
    $("btn-settings-save").addEventListener("click", saveSettings);
    $("btn-reload").addEventListener("click", reloadConfig);
    $("btn-cleanup").addEventListener("click", cleanup);
    $("btn-history").addEventListener("click", loadHistory);
    $("style").addEventListener("change", updateStyleHint);
    $("reference").addEventListener("change", function () {
      setReferenceFile(this.files && this.files[0]);
    });
    $("btn-reference-clear").addEventListener("click", clearReference);
    $("set-provider").addEventListener("change", updateProviderHints);
    $("set-backup-provider").addEventListener("change", updateBackupHints);
    $("set-backup-enabled").addEventListener("change", updateBackupVisibility);
    $("settings-modal").addEventListener("click", function (event) {
      if (event.target === $("settings-modal")) closeSettings();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !$("settings-modal").classList.contains("hidden")) {
        closeSettings();
      }
    });
    $("prompt").addEventListener("input", function () {
      $("prompt-count").textContent = String($("prompt").value.length);
    });
    $("prompt").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        generate();
      }
    });
  }

  async function init() {
    bindEvents();
    if (!bridge) {
      showBanner(
        "未能加载插件页面桥接（bridge SDK）。请确认 AstrBot 版本支持插件页面，并重新打开本页。"
      );
      $("btn-generate").disabled = true;
      return;
    }
    try {
      if (typeof bridge.ready === "function") await bridge.ready();
    } catch (err) {
      /* ready 失败不阻塞，后续调用会给出具体错误 */
    }
    try {
      await loadState();
    } catch (err) {
      showBanner("读取插件状态失败：" + (err.message || err));
    }
    try {
      await loadHistory();
    } catch (err) {
      /* loadHistory 内部已处理 */
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
