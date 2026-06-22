    let _cfgPluginName = "";
    let _cfgSchema = null;

    function esc(s) { return AdminShell.escapeHtml(s); }

    function setState(text, variant) {
      AdminShell.setStatus(text, variant, "topStatus");
      AdminShell.setStatus(text, variant, "mobileStatus");
      AdminShell.setStatus(text, variant, "pluginState");
    }

    // ---- Plugin list ----

    async function loadPlugins() {
      const loadedTbody = AdminShell.byId("loadedRows");
      const availTbody = AdminShell.byId("availableRows");
      const availCard = AdminShell.byId("availableCard");
      try {
        const data = await AdminShell.req("/admin/api/plugins");
        const loaded = data.loaded || [];
        const available = data.available || [];

        AdminShell.byId("loadedCount").textContent = String(data.loaded_count || loaded.length);
        AdminShell.byId("availableCount").textContent = String(data.available_count || available.length);

        if (!loaded.length) {
          loadedTbody.innerHTML = '<tr><td colspan="6" class="empty-state">暂无已加载插件</td></tr>';
        } else {
          loadedTbody.innerHTML = loaded.map(function (p) {
            var typeTag = p.builtin
              ? '<span class="p-tag p-tag--builtin">内置</span>'
              : '<span class="p-tag p-tag--ext">扩展</span>';
            var cmds = [];
            (p.mention_prefixes || []).forEach(function (px) { cmds.push("<code>" + esc(px) + "</code>"); });
            (p.slash_commands || []).forEach(function (c) { cmds.push("<code>/" + esc(c) + "</code>"); });
            var cmdsHtml = cmds.length ? '<div class="p-cmds">' + cmds.join("") + "</div>" : '<span style="color:var(--ink-faint);font-size:12px">-</span>';

            var actions = [];
            if (!p.builtin) {
              actions.push('<button class="btn btn-danger" type="button" data-action="unload-plugin" data-name="' + esc(p.name) + '">卸载</button>');
            }
            actions.push('<button class="btn btn-ghost" type="button" data-action="reload-config" data-name="' + esc(p.name) + '">重载配置</button>');
            actions.push('<button class="btn btn-ghost" type="button" data-action="open-config-editor" data-name="' + esc(p.name) + '">编辑配置</button>');

            return '<tr>' +
              '<td style="font-weight:600">' + esc(p.name) + '</td>' +
              '<td style="font-size:12px;color:var(--ink-soft)">' + esc(p.description || "") + '</td>' +
              '<td style="font-size:12px">' + esc(p.version || "") + '</td>' +
              '<td>' + typeTag + '</td>' +
              '<td>' + cmdsHtml + '</td>' +
              '<td><div class="p-actions">' + actions.join("") + '</div></td>' +
              '</tr>';
          }).join("");
        }

        if (available.length) {
          availCard.style.display = "";
          availTbody.innerHTML = available.map(function (name) {
            return '<tr>' +
              '<td style="font-weight:600">' + esc(name) + '</td>' +
              '<td><div class="p-actions"><button class="btn btn-primary" type="button" data-action="load-plugin" data-name="' + esc(name) + '">加载</button></div></td>' +
              '</tr>';
          }).join("");
        } else {
          availCard.style.display = "none";
        }

        setState("已同步", "success");
      } catch (e) {
        loadedTbody.innerHTML = '<tr><td colspan="6" class="empty-state">加载失败: ' + esc(e.message) + '</td></tr>';
        setState("加载失败", "error");
      }
    }

    // ---- Plugin operations ----

    async function loadPlugin(name) {
      try {
        await AdminShell.req("/admin/api/plugins/" + encodeURIComponent(name) + "/load", {
          method: "POST", body: "{}",
        });
        setState("已加载: " + name, "success");
        await loadPlugins();
      } catch (e) {
        setState("加载失败: " + e.message, "error");
      }
    }

    async function unloadPlugin(name) {
      if (!confirm("确认卸载插件「" + name + "」？")) return;
      try {
        await AdminShell.req("/admin/api/plugins/" + encodeURIComponent(name) + "/unload", {
          method: "POST", body: "{}",
        });
        setState("已卸载: " + name, "success");
        await loadPlugins();
      } catch (e) {
        setState("卸载失败: " + e.message, "error");
      }
    }

    async function reloadConfig(name) {
      try {
        await AdminShell.req("/admin/api/plugins/" + encodeURIComponent(name) + "/reload-config", {
          method: "POST", body: "{}",
        });
        setState("配置已重载: " + name, "success");
      } catch (e) {
        setState("重载失败: " + e.message, "error");
      }
    }

    // ---- Config editor ----

    function normalizeSchema(schema) {
      if (!schema || typeof schema !== "object") {
        return {};
      }
      if (Array.isArray(schema.allOf)) {
        return schema.allOf.reduce(function (merged, item) {
          const normalized = normalizeSchema(item);
          return {
            ...merged,
            ...normalized,
            properties: {
              ...(merged.properties || {}),
              ...(normalized.properties || {}),
            },
          };
        }, { ...schema, properties: {} });
      }
      if (Array.isArray(schema.oneOf) && schema.oneOf.length) {
        return normalizeSchema(schema.oneOf[0]);
      }
      if (Array.isArray(schema.anyOf) && schema.anyOf.length) {
        return normalizeSchema(schema.anyOf[0]);
      }
      return schema;
    }

    function schemaType(meta) {
      meta = normalizeSchema(meta);
      if (Array.isArray(meta.type)) {
        return meta.type.find(function (item) { return item !== "null"; }) || "string";
      }
      if (meta.type) {
        return meta.type;
      }
      if (meta.properties) return "object";
      if (meta.items) return "array";
      return "string";
    }

    function schemaProperties(schema) {
      schema = normalizeSchema(schema);
      if (!schema || typeof schema !== "object" || !schema.properties || typeof schema.properties !== "object") {
        return {};
      }
      return schema.properties;
    }

    function schemaDefault(meta) {
      meta = normalizeSchema(meta);
      if (meta && Object.prototype.hasOwnProperty.call(meta, "default")) {
        return meta.default;
      }
      const type = schemaType(meta);
      if (type === "boolean") return false;
      if (type === "integer" || type === "number") return 0;
      if (type === "array") return [];
      if (type === "object") {
        const result = {};
        Object.keys(schemaProperties(meta)).forEach(function (key) {
          result[key] = schemaDefault(meta.properties[key]);
        });
        return result;
      }
      return "";
    }

    function fieldId(path) {
      return "cfg_plugin_field_" + path.join("_").replace(/[^a-zA-Z0-9_]/g, "_");
    }

    function pathAttr(path) {
      return encodeURIComponent(JSON.stringify(path));
    }

    function readPath(obj, path) {
      return path.reduce(function (current, key) {
        return current && Object.prototype.hasOwnProperty.call(current, key) ? current[key] : undefined;
      }, obj);
    }

    function writePath(obj, path, value) {
      let current = obj;
      path.forEach(function (key, index) {
        if (index === path.length - 1) {
          current[key] = value;
          return;
        }
        if (!current[key] || typeof current[key] !== "object" || Array.isArray(current[key])) {
          current[key] = {};
        }
        current = current[key];
      });
    }

    function renderJsonField(path, meta, value, rows) {
      const id = fieldId(path);
      const title = meta.title || path[path.length - 1];
      const desc = meta.description || "";
      const help = desc ? '<small>' + esc(desc) + '</small>' : "";
      return '<div class="p-config-field">' +
        '<label for="' + esc(id) + '">' + esc(title) + '</label>' +
        '<textarea id="' + esc(id) + '" rows="' + (rows || 4) + '" data-path="' + pathAttr(path) + '" data-type="json">' +
        esc(JSON.stringify(value ?? schemaDefault(meta), null, 2)) +
        '</textarea>' + help +
        '</div>';
    }

    function renderConfigField(path, meta, config, depth) {
      meta = normalizeSchema(meta || {});
      const key = path[path.length - 1];
      const type = schemaType(meta);
      const value = readPath(config, path);
      const resolvedValue = value !== undefined ? value : schemaDefault(meta);
      const id = fieldId(path);
      const title = meta.title || key;
      const desc = meta.description || "";
      const label = '<label for="' + esc(id) + '">' + esc(title) + '</label>';
      const help = desc ? '<small>' + esc(desc) + '</small>' : "";
      const data = ' data-path="' + pathAttr(path) + '" data-type="' + esc(type) + '"';

      if (type === "object" && Object.keys(schemaProperties(meta)).length) {
        const children = Object.keys(schemaProperties(meta)).map(function (childKey) {
          return renderConfigField(path.concat(childKey), meta.properties[childKey], config, depth + 1);
        }).join("");
        return '<fieldset class="p-config-group" style="margin-left:' + Math.min(depth * 10, 30) + 'px">' +
          '<legend>' + esc(title) + '</legend>' +
          (desc ? '<small>' + esc(desc) + '</small>' : "") +
          children +
          '</fieldset>';
      }

      if (type === "boolean") {
        return '<div class="p-config-field p-config-field--check" style="margin-left:' + Math.min(depth * 10, 30) + 'px">' +
          '<input id="' + esc(id) + '" type="checkbox"' + data + (resolvedValue ? " checked" : "") + ' />' +
          '<div>' + label + help + '</div>' +
          '</div>';
      }
      if (Array.isArray(meta.enum) && meta.enum.length > 0) {
        const options = meta.enum.map(function (item) {
          const selected = String(item) === String(resolvedValue) ? " selected" : "";
          return '<option value="' + esc(String(item)) + '"' + selected + '>' + esc(String(item)) + '</option>';
        }).join("");
        return '<div class="p-config-field" style="margin-left:' + Math.min(depth * 10, 30) + 'px">' + label +
          '<select id="' + esc(id) + '"' + data + '>' + options + '</select>' + help +
          '</div>';
      }
      if (type === "integer" || type === "number") {
        const step = type === "integer" ? "1" : "any";
        return '<div class="p-config-field" style="margin-left:' + Math.min(depth * 10, 30) + 'px">' + label +
          '<input id="' + esc(id) + '" type="number" step="' + step + '"' + data +
          ' value="' + esc(String(resolvedValue ?? "")) + '" />' + help +
          '</div>';
      }
      if (type === "array") {
        const itemType = schemaType(meta.items || {});
        if (itemType === "string") {
          return '<div class="p-config-field" style="margin-left:' + Math.min(depth * 10, 30) + 'px">' + label +
            '<textarea id="' + esc(id) + '" rows="4" data-path="' + pathAttr(path) + '" data-type="string-array">' +
            esc(Array.isArray(resolvedValue) ? resolvedValue.join("\n") : "") +
            '</textarea>' + help +
            '</div>';
        }
        return renderJsonField(path, meta, resolvedValue, 6);
      }
      if (type === "object") {
        return renderJsonField(path, meta, resolvedValue, 6);
      }
      return '<div class="p-config-field" style="margin-left:' + Math.min(depth * 10, 30) + 'px">' + label +
        '<input id="' + esc(id) + '" type="text"' + data +
        ' value="' + esc(String(resolvedValue ?? "")) + '" />' + help +
        '</div>';
    }

    function renderConfigForm(config, schema) {
      const form = AdminShell.byId("cfgForm");
      const props = schemaProperties(schema);
      const keys = Object.keys(props);
      if (!keys.length) {
        form.innerHTML = '<div class="p-config-empty">这个插件没有提供可视化配置结构，可以在下面编辑 JSON 原文。</div>';
        return;
      }

      form.innerHTML = keys.map(function (key) {
        return renderConfigField([key], props[key], config || {}, 0);
      }).join("");
    }

    function collectConfigForm(baseConfig, schema) {
      const props = schemaProperties(schema);
      const keys = Object.keys(props);
      if (!keys.length) {
        return null;
      }
      const result = { ...(baseConfig || {}) };
      const form = AdminShell.byId("cfgForm");
      form.querySelectorAll("[data-path]").forEach(function (field) {
        const path = JSON.parse(decodeURIComponent(field.dataset.path));
        const type = field.dataset.type;
        let value;
        if (type === "boolean") {
          value = !!field.checked;
        } else if (type === "integer") {
          const parsed = Number(field.value);
          value = Number.isFinite(parsed) ? Math.trunc(parsed) : 0;
        } else if (type === "number") {
          const parsed = Number(field.value);
          value = Number.isFinite(parsed) ? parsed : 0;
        } else if (type === "json") {
          value = JSON.parse(field.value || "null");
        } else if (type === "string-array") {
          value = field.value.split(/\r?\n/).map(function (item) { return item.trim(); }).filter(Boolean);
        } else {
          value = field.value;
        }
        writePath(result, path, value);
      });
      return result;
    }

    async function openConfigEditor(name) {
      _cfgPluginName = name;
      _cfgSchema = null;
      AdminShell.byId("cfgTitle").textContent = "编辑配置 - " + name;
      AdminShell.byId("cfgEditor").value = "加载中...";
      AdminShell.byId("cfgForm").innerHTML = '<div class="p-config-empty">加载中...</div>';
      AdminShell.showMessage("cfgMsg", "");
      AdminShell.byId("cfgSchemaHint").style.display = "none";
      AdminShell.byId("cfgOverlay").classList.add("is-open");
      AdminShell.byId("cfgDialog").classList.add("is-open");

      try {
        var data = await AdminShell.req("/admin/api/plugins/" + encodeURIComponent(name) + "/config");
        var config = data.config || {};
        _cfgSchema = data.schema || null;
        AdminShell.byId("cfgEditor").value = JSON.stringify(config, null, 2);
        renderConfigForm(config, _cfgSchema);
        if (data.schema) {
          var hint = AdminShell.byId("cfgSchemaHint");
          hint.textContent = data.schema.description || data.schema.title || "插件提供了配置结构";
          hint.style.display = "";
        }
      } catch (e) {
        AdminShell.byId("cfgEditor").value = "{}";
        AdminShell.byId("cfgForm").innerHTML = '<div class="p-config-empty">读取失败，可以稍后重试。</div>';
        AdminShell.showMessage("cfgMsg", "读取配置失败: " + e.message, true);
      }
    }

    function closeConfigEditor() {
      AdminShell.byId("cfgOverlay").classList.remove("is-open");
      AdminShell.byId("cfgDialog").classList.remove("is-open");
      _cfgPluginName = "";
    }

    async function saveConfig() {
      if (!_cfgPluginName) return;
      var raw = AdminShell.byId("cfgEditor").value;
      var parsed;
      try {
        parsed = JSON.parse(raw);
      } catch (e) {
        AdminShell.showMessage("cfgMsg", "JSON 格式无效: " + e.message, true);
        return;
      }
      try {
        var formConfig = collectConfigForm(parsed, _cfgSchema);
        if (formConfig) {
          parsed = formConfig;
          AdminShell.byId("cfgEditor").value = JSON.stringify(parsed, null, 2);
        }
      } catch (e) {
        AdminShell.showMessage("cfgMsg", "表单配置格式无效: " + e.message, true);
        return;
      }
      AdminShell.showMessage("cfgMsg", "");
      try {
        var resp = await AdminShell.req("/admin/api/plugins/" + encodeURIComponent(_cfgPluginName) + "/config", {
          method: "POST",
          body: JSON.stringify({ config: parsed }),
        });
        var msg = "配置已保存";
        if (resp.reload) msg += " (" + resp.reload + ")";
        AdminShell.showMessage("cfgMsg", msg);
        setState("配置已保存: " + _cfgPluginName, "success");
      } catch (e) {
        AdminShell.showMessage("cfgMsg", "保存失败: " + e.message, true);
      }
    }

    AdminShell.registerActions({
      "refresh-plugins": () => loadPlugins(),
      "load-plugin": (el) => loadPlugin(el.dataset.name),
      "unload-plugin": (el) => unloadPlugin(el.dataset.name),
      "reload-config": (el) => reloadConfig(el.dataset.name),
      "open-config-editor": (el) => openConfigEditor(el.dataset.name),
      "plugin-cfg-close": () => closeConfigEditor(),
      "plugin-cfg-save": () => saveConfig(),
    });
    AdminShell.bootstrapAuth({
      page: "plugins",
      loggedInText: "已登录插件管理",
      onLogin: () => loadPlugins(),
      onLoggedOut: () => setState("等待操作", "warning"),
      onLoginError: () => setState("登录失败", "error"),
    });
