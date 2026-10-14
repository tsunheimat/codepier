'use strict';
// Declarative forms only. Manifests never supply HTML, JavaScript or credentials.
window.CodePierExternalPlugins = (() => {
  let ticket = 0;
  const base = '/api/external-integrations';
  const button = (action, id, label) =>
    `<button class="btn small" data-ext-action="${action}" data-ext-id="${esc(id)}">${esc(label)}</button>`;
  const option = (value, label, selected = false) =>
    `<option value="${esc(value)}" ${selected ? 'selected' : ''}>${esc(label)}</option>`;
  const select = (name, label, options, extra = '') =>
    `<label class="field">${esc(label)}<select name="${name}" ${extra}>${options}</select></label>`;
  const json = (value) =>
    `<pre class="code-box gw-wrap">${esc(JSON.stringify(value, null, 2))}</pre>`;
  function intent() {
    const session = S.session,
      space = S.space_id,
      page = S.page,
      modalId = S.modalIntent || 0,
      current = ++ticket;
    return () =>
      session === S.session &&
      space === S.space_id &&
      page === S.page &&
      modalId === (S.modalIntent || 0) &&
      current === ticket;
  }
  async function inventory() {
    const [catalog, gateway] = await Promise.all([
      api('/api/external-plugins'),
      api('/api/mcp-gateway'),
    ]);
    const integrations =
      gateway.space_admin && gateway.enabled ? (await api(base)).integrations : [];
    return { catalog: catalog.plugins, gateway, integrations };
  }
  async function html() {
    const data = await inventory();
    if (!data.gateway.enabled) return '';
    const body = data.gateway.space_admin
      ? `<div class="actions">${button('configure', '', '添加插件集成')}</div>${data.integrations.map((row) => `<article class="grant-row" data-ext-integration="${esc(row.id)}"><div><strong>${esc(row.alias)}</strong> · ${esc(row.plugin_id)} · v${row.version} · ${row.enabled ? '启用' : '停用'}<p class="mono gw-wrap">${esc(row.id)}</p></div><div class="actions">${button('configure', row.id, '配置集成')}${button('projects', row.id, '项目绑定')}${button('role', row.id, '角色动作')}${button('toggle', row.id, row.enabled ? '停用' : '启用')}</div></article>`).join('') || '<p>尚无插件集成。</p>'}`
      : '<p>插件集成由空间管理员配置；客户端使用前仍需角色授权与独立同意。</p>';
    return `<section class="panel" id="external-plugins-panel"><div class="panel-head"><h2>External plugins · 外部插件</h2></div><div class="panel-body"><p>空间配置一次，项目填写外部资源 ID。插件动作与原始 MCP 工具分别授权。</p>${body}<details><summary>已安装契约与开发入口</summary>${data.catalog.map((item) => `<h3>${esc(item.manifest.title)} · ${esc(item.manifest.version)}</h3>${json(item.manifest)}`).join('')}<p>部署管理员可通过 CODEPIER_EXTERNAL_PLUGIN_DIR 加载只读 JSON 清单。项目编辑不会安装插件代码。</p></details></div></section>`;
  }
  function form(title, body, save) {
    const session = S.session,
      space = S.space_id,
      page = S.page;
    const dialog = modal(
      title,
      `<form id="ext-form">${body}<p id="ext-status" role="status"></p></form>`,
      '<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="ext-form">确认保存</button>',
      true,
    );
    const node = $('#ext-form', dialog);
    const current = () =>
      dialog.isConnected && session === S.session && space === S.space_id && page === S.page;
    node.onsubmit = async (event) => {
      event.preventDefault();
      if (!current() || node.dataset.submitting || node.dataset.uncertain) return;
      if (!node.reportValidity()) return;
      node.dataset.submitting = 'true';
      const submit = $('button[type="submit"]', dialog);
      await busy(submit, async () => {
        try {
          if (!current()) return;
          await save(node);
          if (!current()) return;
          $('#ext-status', dialog).textContent = '已保存，正在刷新配置…';
          await renderPage(false);
          if (!current()) return;
          closeModal(dialog);
          toast('插件配置已保存');
        } catch (error) {
          if (!current()) return;
          const uncertain =
            error.code === 'NETWORK_UNCERTAIN' || [408, 500, 502, 503, 504].includes(error.status);
          if (uncertain) node.dataset.uncertain = 'true';
          $('#ext-status', dialog).textContent = uncertain
            ? '结果未确认，已阻止重复提交。请关闭并刷新，核对原配置后再操作。'
            : error.message;
        } finally {
          delete node.dataset.submitting;
        }
      });
      if (current() && node.dataset.uncertain) submit.disabled = true;
    };
    return { dialog, node, current };
  }
  function configFields(schema, values = {}, prefix = 'config') {
    return (
      Object.entries(schema.properties || {})
        .map(([key, field], index) => {
          const name = prefix + '-' + index,
            value = values[key] ?? field.default,
            required = (schema.required || []).includes(key);
          const label = field.title || key;
          if (Array.isArray(field.enum))
            return select(
              name,
              label,
              (required ? '' : option('', '未设置')) +
                field.enum
                  .map((v, i) =>
                    option(String(i), String(v), JSON.stringify(v) === JSON.stringify(value)),
                  )
                  .join(''),
              required ? 'required' : '',
            );
          if (field.type === 'boolean')
            return select(
              name,
              label,
              (required ? '' : option('', '未设置', value === undefined)) +
                option('true', '是', value === true) +
                option('false', '否', value === false),
              required ? 'required' : '',
            );
          if (['string', 'integer', 'number'].includes(field.type)) {
            const numeric = field.type !== 'string';
            const limits = numeric
              ? `${field.minimum !== undefined ? ` min="${esc(field.minimum)}"` : ''}${field.maximum !== undefined ? ` max="${esc(field.maximum)}"` : ''}`
              : ` maxlength="${Math.min(field.maxLength ?? 4096, 65536)}"`;
            return `<label class="field">${esc(label)}<input name="${name}" type="${numeric ? 'number' : 'text'}" ${numeric ? `step="${field.type === 'integer' ? '1' : 'any'}"` : ''}${limits} ${required ? 'required' : ''} value="${esc(value ?? '')}"></label>`;
          }
          return `<label class="field">${esc(label)} (JSON)<textarea name="${name}" rows="3" ${required ? 'required' : ''}>${value === undefined ? '' : esc(JSON.stringify(value, null, 2))}</textarea></label>`;
        })
        .join('') || '<p class="form-note">无需额外配置。</p>'
    );
  }
  function configValue(node, schema, prefix = 'config') {
    const result = Object.create(null);
    Object.entries(schema.properties || {}).forEach(([key, field], index) => {
      const raw = node.elements[prefix + '-' + index].value;
      if (raw === '') return;
      if (Array.isArray(field.enum)) result[key] = field.enum[Number(raw)];
      else if (field.type === 'boolean') result[key] = raw === 'true';
      else if (['integer', 'number'].includes(field.type)) result[key] = Number(raw);
      else if (field.type === 'string') result[key] = raw;
      else result[key] = JSON.parse(raw);
    });
    return result;
  }
  async function configure(id) {
    const valid = intent(),
      data = await inventory();
    if (!valid()) return;
    const old = data.integrations.find((row) => row.id === id);
    if (id && !old) throw new Error('集成已变化，请刷新');
    const f = form(
      '配置外部插件',
      `${select('plugin', '插件契约', data.catalog.map((item) => option(item.manifest.id, item.manifest.title + ' · ' + item.manifest.version, item.manifest.id === old?.plugin_id)).join(''), old ? 'disabled' : '')}<label class="field">集成别名<input name="alias" pattern="[a-z][a-z0-9-]{0,19}" maxlength="20" required ${old ? 'readonly' : ''} value="${esc(old?.alias || '')}"></label><div id="ext-config-fields"></div><label class="check"><input name="reviewed" type="checkbox" required>我已审核契约、账号和工具映射；配置改变后须重新同意。</label>`,
      async (node) => {
        const item = data.catalog.find((item) => item.manifest.id === node.elements.plugin.value);
        const backends = Object.create(null);
        Object.entries(item.manifest.backend_slots).forEach(([name, backend], index) => {
          const tools = Object.create(null);
          backend.tool_slots.forEach((slot, slotIndex) => {
            tools[slot] = node.elements[`tool-${index}-${slotIndex}`].value;
          });
          backends[name] = { binding_id: node.elements['backend-' + index].value, tools };
        });
        await api(base + '/' + encodeURIComponent(node.elements.alias.value), {
          method: 'PUT',
          body: JSON.stringify({
            plugin_id: item.manifest.id,
            manifest_hash: item.manifest_hash,
            expected_version: old?.version || 0,
            config: configValue(node, item.manifest.space_config_schema),
            backends,
          }),
        });
      },
    );
    function draw() {
      const item = data.catalog.find((item) => item.manifest.id === f.node.elements.plugin.value);
      if (!item) return;
      const same = old?.plugin_id === item.manifest.id;
      $('#ext-config-fields', f.dialog).innerHTML =
        `<p class="mono gw-wrap">契约 ${esc(item.manifest_hash)}</p>${configFields(item.manifest.space_config_schema, same ? old.config : {})}${Object.entries(
          item.manifest.backend_slots,
        )
          .map(
            ([name], i) =>
              `<fieldset><legend>${esc(name)}</legend>${select(
                'backend-' + i,
                '已批准的 MCP binding',
                option('', '请选择账号 / binding') +
                  data.gateway.bindings
                    .filter((b) => b.enabled)
                    .map((b) =>
                      option(
                        b.id,
                        b.alias +
                          ' · ' +
                          (data.gateway.accounts.find((a) => a.id === b.account_id)?.label ||
                            b.account_id),
                        same && old.backends[name]?.binding_id === b.id,
                      ),
                    )
                    .join(''),
                'required',
              )}<div id="ext-tools-${i}"></div></fieldset>`,
          )
          .join('')}`;
      Object.entries(item.manifest.backend_slots).forEach(([name, backend], i) => {
        const renderTools = () => {
          const binding = data.gateway.bindings.find(
            (b) => b.id === f.node.elements['backend-' + i].value,
          );
          $('#ext-tools-' + i, f.dialog).innerHTML = backend.tool_slots
            .map((slot, n) =>
              select(
                `tool-${i}-${n}`,
                slot,
                option('', '请选择审核后的工具') +
                  (binding?.tools || [])
                    .map((t) =>
                      option(
                        t,
                        t,
                        same &&
                          old.backends[name]?.binding_id === binding.id &&
                          old.backends[name]?.tools[slot]?.name === t,
                      ),
                    )
                    .join(''),
                'required',
              ),
            )
            .join('');
        };
        f.node.elements['backend-' + i].onchange = renderTools;
        renderTools();
      });
      f.node.elements.reviewed.checked = false;
    }
    f.node.elements.plugin.onchange = draw;
    draw();
  }
  async function projects(id, selectedProject = '') {
    const valid = intent(),
      data = await inventory();
    const row = data.integrations.find((r) => r.id === id);
    if (!row) throw new Error('集成已变化，请刷新');
    const attachments = (await api(base + '/' + id + '/projects')).attachments;
    if (!valid()) return;
    const manifest = data.catalog.find((i) => i.manifest.id === row.plugin_id)?.manifest;
    if (!manifest) throw new Error('插件尚未安装');
    const f = form(
      '插件项目绑定',
      `<p>${esc(row.alias)} · 只配置外部资源 ID，不移动文件，也不授予权限。</p>${select('project', 'CodePier 项目', (S.projects || []).map((p) => option(p.id, p.alias, p.id === selectedProject)).join(''), 'required')}${select('mode', '操作', option('attach', '保存 / 重新绑定') + option('detach', '解除绑定（保留资源）'))}<div id="ext-project-config"></div>`,
      async (node) => {
        const project = node.elements.project.value,
          old = attachments.find((a) => a.project_id === project);
        const path = base + '/' + id + '/projects/' + encodeURIComponent(project);
        if (node.elements.mode.value === 'detach') {
          if (!old) throw new Error('此项目尚未绑定');
          await api(path + '?expected_version=' + old.version, { method: 'DELETE' });
        } else
          await api(path, {
            method: 'PUT',
            body: JSON.stringify({
              expected_version: old?.version || 0,
              config: configValue(node, manifest.project_config_schema, 'project-config'),
            }),
          });
      },
    );
    const draw = () => {
      const old = attachments.find((a) => a.project_id === f.node.elements.project.value);
      $('#ext-project-config', f.dialog).innerHTML =
        f.node.elements.mode.value === 'detach'
          ? '<p>解除绑定不会删除共享集成、后端账号或外部项目。</p>'
          : configFields(manifest.project_config_schema, old?.config || {}, 'project-config');
    };
    f.node.elements.project.onchange = draw;
    f.node.elements.mode.onchange = draw;
    draw();
  }
  async function role(id) {
    const valid = intent(),
      data = await inventory(),
      roles = (await api('/api/access-roles')).roles;
    if (!valid()) return;
    const row = data.integrations.find((r) => r.id === id),
      manifest = data.catalog.find((i) => i.manifest.id === row?.plugin_id)?.manifest;
    if (!manifest || !roles.length) throw new Error('请先配置集成并建立角色');
    const scopeOption = Object.values(manifest.actions).some((a) => a.scope === 'space')
      ? option('_space', '空间范围（不是全部项目）')
      : '';
    const f = form(
      '插件角色动作',
      `${select('role', '角色', roles.map((r) => option(r.id, r.label)).join(''), 'required')}${select('target', '动作作用范围', (S.projects || []).map((p) => option(p.id, p.alias)).join('') + scopeOption, 'required')}<div id="ext-role-actions"></div><p class="form-note">只修改当前集成与所选范围的动作。其他规则保持不变；项目动作仍需已有项目读取权，不会获得原始 MCP 工具权限。客户端须另行同意。</p>`,
      async (node) => {
        const current = roles.find((r) => r.id === node.elements.role.value),
          target = node.elements.target.value,
          scope = target === '_space' ? 'space' : 'project';
        const rules = (current.plugin_rules || []).flatMap((rule) => {
          if (rule.integration_id !== id || rule.scope !== scope) return [rule];
          if (scope === 'space') return [];
          const remaining = rule.projects.filter((p) => p !== target);
          return remaining.length ? [{ ...rule, projects: remaining }] : [];
        });
        const actions = $$('[name="action"]:checked', node).map((el) => el.value);
        if (actions.length)
          rules.push({
            integration_id: id,
            scope,
            projects: scope === 'space' ? [] : [target],
            actions,
          });
        await api('/api/access-roles/' + current.id, {
          method: 'PUT',
          body: JSON.stringify({
            label: current.label,
            enabled: current.enabled,
            project_rules: current.project_rules,
            device_rules: current.device_rules,
            connector_rules: current.connector_rules,
            vps_rules: current.vps_rules,
            plugin_rules: rules,
            expected_version: current.version,
          }),
        });
      },
    );
    const draw = () => {
      const current = roles.find((r) => r.id === f.node.elements.role.value),
        target = f.node.elements.target.value,
        scope = target === '_space' ? 'space' : 'project';
      const selected = (current.plugin_rules || [])
        .filter(
          (r) =>
            r.integration_id === id &&
            r.scope === scope &&
            (scope === 'space' || r.projects.includes(target)),
        )
        .flatMap((r) => r.actions);
      $('#ext-role-actions', f.dialog).innerHTML = Object.entries(manifest.actions)
        .filter(([, a]) => a.scope === scope)
        .map(
          ([name, a]) =>
            `<label class="check"><input name="action" type="checkbox" value="${esc(name)}" ${selected.includes(name) ? 'checked' : ''}>${esc(name)} · ${esc(a.effect)} · ${esc(a.title)}</label>`,
        )
        .join('');
    };
    f.node.elements.role.onchange = draw;
    f.node.elements.target.onchange = draw;
    draw();
  }
  async function toggle(id) {
    const valid = intent(),
      data = await inventory();
    if (!valid()) return;
    const row = data.integrations.find((r) => r.id === id);
    if (!row) throw new Error('集成已变化');
    form(
      row.enabled ? '停用插件集成' : '启用插件集成',
      `<p>${esc(row.alias)}。此操作不会终止已发出的外部任务；重新启用也不会恢复旧的插件同意。</p>`,
      () =>
        api(base + '/' + id, {
          method: 'PATCH',
          body: JSON.stringify({ expected_version: row.version, enabled: !row.enabled }),
        }),
    );
  }
  async function consent(grant) {
    const valid = intent(),
      path = base + '/grants/' + encodeURIComponent(grant) + '/consent',
      preview = await api(path);
    if (!valid()) return;
    form(
      '插件动作与目标同意',
      `${preview.routes.map((r) => `<article class="grant-row"><div><h3>${esc(r.integration_alias)} · ${esc(r.action)} · ${esc(r.effect)}</h3><p>CodePier 项目：${esc(r.project_id || '空间范围')}；后端账号：${esc(r.backend_account.label)}；工具：${esc(r.backend_tool)}</p>${json({ project: r.project_config, space: r.space_config })}${r.broader_direct_access.length ? '<p class="error-text">此连接另有同账号的原始 MCP 工具权限，可绕过插件范围限制。请在角色中移除不需要的直接权限。</p>' : ''}</div></article>`).join('') || '<p>没有可同意的动作。请检查项目绑定、角色规则、后端账号和外部 MCP 同意。</p>'}${select('mode', '操作', (preview.routes.length ? option('approve', '明确同意以上当前动作与目标') : '') + (preview.has_consent ? option('revoke', '撤回全部插件动作同意') : ''), 'required')}<label class="check"><input type="checkbox" required>我已阅读动作、外部目标与账号。插件或绑定改变后不会自动扩展本次同意。</label>`,
      (node) =>
        node.elements.mode.value === 'revoke'
          ? api(path, { method: 'DELETE' })
          : api(path, {
              method: 'POST',
              body: JSON.stringify({
                confirmed: true,
                expected_role_version: preview.role_version,
                review_hash: preview.review_hash,
              }),
            }),
    );
  }
  document.addEventListener('click', (event) => {
    const node = event.target.closest('[data-ext-action]');
    if (!node || node.disabled) return;
    const actions = { configure, projects, role, toggle, consent };
    const action = actions[node.dataset.extAction];
    if (action) action(node.dataset.extId).catch((error) => toast(error.message, true));
  });
  return { html, consent, configFields, configValue };
})();
