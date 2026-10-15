'use strict';
window.CodePierProjectRouting = (() => {
  let ticket = 0;
  const root = '/api/project-routing';
  const option = (value, text, selected = false) =>
    `<option value="${esc(value)}" ${selected ? 'selected' : ''}>${esc(text)}</option>`;
  const select = (name, text, choices, extra = '') =>
    `<label class="field">${esc(text)}<select name="${name}" ${extra}>${choices}</select></label>`;
  const input = (name, text, value = '', extra = '') =>
    `<label class="field">${esc(text)}<input name="${name}" value="${esc(value)}" ${extra}></label>`;
  const area = (name, text, value) =>
    `<label class="field">${esc(text)}<textarea name="${name}" rows="8">${esc(JSON.stringify(value, null, 2))}</textarea></label>`;
  const button = (action, id, text) =>
    `<button class="btn small" data-routing="${action}" data-routing-id="${esc(id)}">${esc(text)}</button>`;
  const panel = (title, body) =>
    `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2></div><div class="panel-body">${body}</div></section>`;
  function intent() {
    const current = ++ticket,
      session = S.session,
      space = S.space_id,
      page = S.page,
      modalId = S.modalIntent;
    return () =>
      current === ticket &&
      session === S.session &&
      space === S.space_id &&
      page === S.page &&
      modalId === S.modalIntent;
  }
  function form(title, body, save) {
    const session = S.session,
      space = S.space_id,
      page = S.page;
    const dialog = modal(
      title,
      `<form id="routing-form">${body}<p id="routing-status" role="status"></p></form>`,
      '<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" form="routing-form" type="submit">保存</button>',
      true,
    );
    const node = $('#routing-form', dialog);
    const current = () =>
      dialog.isConnected && session === S.session && space === S.space_id && page === S.page;
    node.onsubmit = async (event) => {
      event.preventDefault();
      if (!current() || node.dataset.busy || node.dataset.uncertain || !node.reportValidity())
        return;
      node.dataset.busy = '1';
      await busy($('button[type="submit"]', dialog), async () => {
        try {
          if (!current()) return;
          await save(node);
          if (!current()) return;
          await renderPage(false);
          if (!current()) return;
          closeModal(dialog);
          toast('路由配置已保存');
        } catch (error) {
          if (!current()) return;
          const uncertain =
            error.code === 'NETWORK_UNCERTAIN' || [408, 500, 502, 503, 504].includes(error.status);
          if (uncertain) node.dataset.uncertain = '1';
          $('#routing-status', dialog).textContent = uncertain
            ? '结果未确认，停止重复提交。请刷新后核对原配置。'
            : error.message;
        } finally {
          delete node.dataset.busy;
        }
      });
      if (current() && node.dataset.uncertain) $('button[type="submit"]', dialog).disabled = true;
    };
    return { dialog, node, current };
  }
  const send = (path, body, method = 'PUT') => api(path, { method, body: JSON.stringify(body) });
  const inventory = () => api(root);
  async function html() {
    let data;
    try {
      data = await inventory();
    } catch (error) {
      if (error.status === 403) return '';
      throw error;
    }
    return `<div id="project-routing-panel">${panel(
      '共享 NFS · 空间资源',
      `<p>登记同一份 NFS 存储和设备挂载路径。项目选择子文件夹，不再固定属于某台设备。不会创建挂载或同步副本。</p><div class="actions">${button('source', '', '添加共享源')}${button('route', '', '添加动作路线')}${button('defaults', '', '空间默认路线')}${button('execution', '', '空间测试 / Hooks')}</div>${
        data.sources
          .map(
            (s) =>
              `<article class="grant-row"><div><strong>${esc(s.name)}</strong> · ${s.enabled ? '启用' : '停用'}<p class="mono gw-wrap">${esc(s.id)}</p>${data.mounts
                .filter((m) => m.source_root_id === s.id)
                .map(
                  (m) =>
                    `<p>${esc(data.devices.find((d) => d.id === m.device_id)?.name || m.device_id)} → <code>${esc(m.path)}</code> · ${esc(m.mode)} · ${m.allow_tasks ? '允许任务' : '仅文件'} · ${m.enabled ? '启用' : '停用'}</p>`,
                )
                .join(
                  '',
                )}</div><div class="actions">${button('source', s.id, '共享源设置')}${button('mount', s.id, '设备挂载')}${button('batch', s.id, '选择子文件夹 / 批量建项目')}</div></article>`,
          )
          .join('') || '<p>尚未登记共享源。</p>'
      }`,
    )}${panel('按动作选择设备', `<p>默认路线只提供配置，不授予权限。旧项目保持原设备映射；接入共享源后才使用新路线。</p>${data.routes.map((r) => `<article class="grant-row"><div><strong>${esc(r.name)}</strong> · ${esc(r.family)} → ${esc(data.devices.find((d) => d.id === r.device_id)?.name || r.device_id)} · ${esc(r.settings.provider)} · ${r.enabled ? '启用' : '停用'}</div><div class="actions">${button('route', r.id, '配置')}${button('role', r.id, '角色授权')}</div></article>`).join('')}`)}${panel(
      '逻辑项目与动作覆盖',
      (S.projects || [])
        .map(
          (p) =>
            `<article class="grant-row"><div><strong>${esc(p.alias)}</strong> · ${p.routing_managed ? `共享源 ${esc(p.source_root_id || '未绑定')} / ${esc(p.subpath)}` : '原设备映射'}${(
              p.execution_routes || []
            )
              .filter((r) => r.configured || r.authorized)
              .map(
                (r) =>
                  `<p>${esc(r.action)} → ${esc(r.device_name || r.error_code || '未配置')} · ${esc(r.provider || '')} · ${r.online ? '在线' : '离线 / 不可用'}</p>`,
              )
              .join(
                '',
              )}</div><div class="actions">${button('binding', p.id, p.routing_managed ? '源绑定 / 解除绑定' : '接入共享源')}${p.routing_managed ? button('override', p.id, '项目路线') + button('execution', p.id, '测试 / Hooks') + button('metadata', p.id, '项目设置') : ''}</div></article>`,
        )
        .join(''),
    )}</div>`;
  }
  async function source(id) {
    const valid = intent(),
      data = await inventory();
    if (!valid()) return;
    const old = data.sources.find((s) => s.id === id);
    form(
      '共享源设置',
      input(
        'name',
        '空间内共享源名称',
        old?.name || '',
        'required pattern="[a-z][a-z0-9_.-]{0,63}" ' + (old ? 'readonly' : ''),
      ) +
        select(
          'enabled',
          '状态',
          option('true', '启用', old?.enabled !== 0) + option('false', '停用', old?.enabled === 0),
        ),
      (node) =>
        send('/api/source-roots/' + encodeURIComponent(node.elements.name.value), {
          expected_version: old?.version || 0,
          enabled: node.elements.enabled.value === 'true',
        }),
    );
  }
  async function mount(id) {
    const valid = intent(),
      data = await inventory();
    if (!valid()) return;
    const f = form(
      '共享源的设备挂载',
      select('device', '设备', data.devices.map((d) => option(d.id, d.name)).join(''), 'required') +
        select(
          'operation',
          '操作',
          option('save', '验证并保存') + option('disable', '停用此挂载'),
        ) +
        input('path', '此设备上的绝对挂载路径', '', 'required') +
        select('mode', '文件权限', option('read', '只读') + option('write', '可写')) +
        '<label class="check"><input type="checkbox" name="tasks">允许任务（仍需本机授权；内置设备不支持）</label><label class="check"><input type="checkbox" name="same" required>确认各设备挂载的是同一份 NFS 存储。</label><p>这里只登记已有挂载。目录验证由选定设备执行，不会启用其 Shell 或修改本机授权。</p>',
      async (node) => {
        const old = data.mounts.find(
          (m) => m.source_root_id === id && m.device_id === node.elements.device.value,
        );
        const path = '/api/source-roots/' + id + '/mounts/' + node.elements.device.value;
        if (node.elements.operation.value === 'disable') {
          if (!old) throw new Error('此挂载尚未登记');
          await api(path + '?expected_version=' + old.version, { method: 'DELETE' });
        } else
          await send(path, {
            path: node.elements.path.value,
            mode: node.elements.mode.value,
            allow_tasks: node.elements.tasks.checked,
            confirmed_same_storage: node.elements.same.checked,
            expected_version: old?.version || 0,
          });
      },
    );
    const draw = () => {
      const old = data.mounts.find(
        (m) => m.source_root_id === id && m.device_id === f.node.elements.device.value,
      );
      f.node.elements.path.value = old?.path || '';
      f.node.elements.mode.value = old?.mode || 'read';
      f.node.elements.tasks.checked = !!old?.allow_tasks;
    };
    f.node.elements.device.onchange = draw;
    draw();
  }
  async function route(id) {
    const valid = intent(),
      data = await inventory();
    if (!valid()) return;
    const old = data.routes.find((r) => r.id === id);
    const f = form(
      '动作执行路线',
      input('name', '路线名称', old?.name || '', 'required ' + (old ? 'readonly' : '')) +
        select(
          'family',
          '动作类型',
          data.actions.map((a) => option(a, a, a === old?.family)).join(''),
          old ? 'disabled' : '',
        ) +
        select(
          'device',
          '执行设备',
          data.devices.map((d) => option(d.id, d.name, d.id === old?.device_id)).join(''),
          'required',
        ) +
        select(
          'provider',
          '执行适配器',
          option('device', '设备原生执行', old?.settings.provider !== 'rch') +
            option('rch', 'RCH（仅 test）', old?.settings.provider === 'rch'),
        ) +
        select(
          'enabled',
          '状态',
          option('true', '启用', old?.enabled !== 0) + option('false', '停用', old?.enabled === 0),
        ) +
        area(
          'rch',
          'RCH 参数（严格远端，无本地回退）',
          old?.settings.rch || {
            mode: 'job',
            require_remote: true,
            worker: '',
            profile: '',
            required_tools: [],
            result_dirs: [],
          },
        ) +
        area('hooks', '本路线 Hooks', old?.settings.hooks || []),
      (node) =>
        send('/api/action-routes/' + encodeURIComponent(node.elements.name.value), {
          expected_version: old?.version || 0,
          family: node.elements.family.value,
          device_id: node.elements.device.value,
          enabled: node.elements.enabled.value === 'true',
          settings: {
            provider: node.elements.provider.value,
            rch: JSON.parse(node.elements.rch.value),
            hooks: JSON.parse(node.elements.hooks.value),
          },
        }),
    );
    const draw = () => {
      const isTest = f.node.elements.family.value === 'test';
      f.node.elements.provider.disabled = !isTest;
      if (!isTest) f.node.elements.provider.value = 'device';
    };
    f.node.elements.family.onchange = draw;
    draw();
  }
  async function defaults() {
    const valid = intent(),
      data = await inventory();
    if (!valid()) return;
    const f = form(
      '空间默认动作路线',
      select('family', '动作', data.actions.map((a) => option(a, a)).join('')) +
        '<div id="routing-default-choice"></div>',
      (node) => {
        const old = data.defaults.find((r) => r.family === node.elements.family.value);
        return send(root + '/defaults/' + node.elements.family.value, {
          route_id: node.elements.route.value,
          expected_version: old?.version || 0,
        });
      },
    );
    const draw = () => {
      const family = f.node.elements.family.value,
        old = data.defaults.find((d) => d.family === family);
      $('#routing-default-choice', f.dialog).innerHTML = select(
        'route',
        '默认路线',
        data.routes
          .filter((r) => r.family === family && r.enabled)
          .map((r) => option(r.id, r.name, r.id === old?.route_id))
          .join(''),
        'required',
      );
    };
    f.node.elements.family.onchange = draw;
    draw();
  }
  async function batch(id) {
    const valid = intent(),
      data = await inventory();
    if (!valid()) return;
    const mounts = data.mounts.filter((m) => m.source_root_id === id && m.enabled);
    if (!mounts.length) throw new Error('先登记至少一个设备挂载');
    const requestKey = crypto.randomUUID();
    const f = form(
      '选择 NFS 子文件夹并批量建立项目',
      select(
        'device',
        '浏览 / 验证设备',
        mounts
          .map((m) =>
            option(
              m.device_id,
              data.devices.find((d) => d.id === m.device_id)?.name || m.device_id,
            ),
          )
          .join(''),
        'required',
      ) +
        input('path', '浏览相对子目录', '.', 'required') +
        '<button type="button" class="btn" id="routing-browse">浏览子目录</button><div id="routing-folders"></div>' +
        select('mode', '项目文件模式', option('write', '可写') + option('read', '只读')) +
        '<label class="check"><input type="checkbox" name="tasks">项目允许执行任务（执行设备另外授权）</label>' +
        area('projects', '待建立项目（别名 + 相对子路径；可直接编辑 JSON）', []),
      (node) =>
        send(
          root + '/projects',
          {
            source_root_id: id,
            validation_device_id: node.elements.device.value,
            projects: JSON.parse(node.elements.projects.value).map((p) => ({
              ...p,
              mode: node.elements.mode.value,
              allow_tasks: node.elements.tasks.checked,
            })),
            idempotency_key: requestKey,
          },
          'POST',
        ),
    );
    let browseTicket = 0;
    $('#routing-browse', f.dialog).onclick = async () => {
      const mine = ++browseTicket,
        device = f.node.elements.device.value,
        path = f.node.elements.path.value;
      try {
        const result = await api(
          '/api/source-roots/' + id + '/browse?' + new URLSearchParams({ device_id: device, path }),
        );
        if (
          !f.current() ||
          mine !== browseTicket ||
          device !== f.node.elements.device.value ||
          path !== f.node.elements.path.value
        )
          return;
        if (result.pending) {
          $('#routing-status', f.dialog).textContent =
            '目录读取尚在执行：' + result.operation_id + '。完成后重新浏览。';
          return;
        }
        const folders = (result.entries || []).filter(
          (e) => e.type === 'directory' || e.type === 'dir',
        );
        $('#routing-folders', f.dialog).innerHTML =
          folders
            .map(
              (e, i) =>
                `<label class="check"><input type="checkbox" data-routing-folder="${i}">${esc(e.path)}</label>`,
            )
            .join('') +
          (result.next_offset !== null && result.next_offset !== undefined
            ? '<p>结果已分页；请缩小子目录或直接填写项目列表。</p>'
            : '');
        $$('[data-routing-folder]', f.dialog).forEach(
          (el) =>
            (el.onchange = () => {
              f.node.elements.projects.value = JSON.stringify(
                $$('[data-routing-folder]:checked', f.dialog).map((box) => {
                  const entry = folders[Number(box.dataset.routingFolder)];
                  return { alias: entry.path.split('/').pop(), subpath: entry.path };
                }),
                null,
                2,
              );
            }),
        );
      } catch (error) {
        if (f.current()) $('#routing-status', f.dialog).textContent = error.message;
      }
    };
  }
  async function binding(id) {
    const valid = intent(),
      data = await inventory(),
      detail = await api('/api/projects/' + id + '/routing');
    if (!valid()) return;
    const p = detail.project;
    const f = form(
      '项目源绑定',
      select(
        'source',
        '共享源',
        option('', '解除绑定（保留项目与资源）', !p.source_root_id) +
          data.sources
            .filter((s) => s.enabled)
            .map((s) => option(s.id, s.name, s.id === p.source_root_id))
            .join(''),
      ) +
        input('subpath', '相对子文件夹', p.subpath || '.', 'required') +
        '<div id="routing-binding-device"></div><p>接入共享源后，项目不再以原设备作为隐含回退。请先配置空间默认路线和角色路线权限。解除绑定不删除 NFS 文件。</p>',
      (node) =>
        send('/api/projects/' + id + '/source', {
          source_root_id: node.elements.source.value || null,
          subpath: node.elements.subpath.value,
          validation_device_id: node.elements.device?.value || '',
          expected_version: p.routing_version || 1,
        }),
    );
    const draw = () => {
      $('#routing-binding-device', f.dialog).innerHTML = f.node.elements.source.value
        ? select(
            'device',
            '验证设备',
            data.mounts
              .filter((m) => m.source_root_id === f.node.elements.source.value && m.enabled)
              .map((m) =>
                option(
                  m.device_id,
                  data.devices.find((d) => d.id === m.device_id)?.name || m.device_id,
                ),
              )
              .join(''),
            'required',
          )
        : '';
    };
    f.node.elements.source.onchange = draw;
    draw();
  }
  async function override(id) {
    const valid = intent(),
      data = await inventory(),
      detail = await api('/api/projects/' + id + '/routing');
    if (!valid()) return;
    const f = form(
      '项目动作路线',
      select('family', '动作', data.actions.map((a) => option(a, a)).join('')) +
        '<div id="routing-override"></div>',
      (node) => {
        const chosen = node.elements.route.value;
        return send('/api/projects/' + id + '/routing/' + node.elements.family.value, {
          mode: ['inherit', 'disabled'].includes(chosen) ? chosen : 'route',
          route_id: ['inherit', 'disabled'].includes(chosen) ? null : chosen,
          expected_version: detail.project.routing_version,
        });
      },
    );
    const draw = () => {
      const family = f.node.elements.family.value,
        old = detail.overrides.find((o) => o.family === family);
      $('#routing-override', f.dialog).innerHTML = select(
        'route',
        '路线',
        option('inherit', '继承空间默认', !old) +
          option('disabled', '禁用此项目动作', old && old.route_id === null) +
          data.routes
            .filter((r) => r.family === family && r.enabled)
            .map((r) => option(r.id, r.name, r.id === old?.route_id))
            .join(''),
      );
    };
    f.node.elements.family.onchange = draw;
    draw();
  }
  async function execution(id) {
    const valid = intent(),
      data = id ? await api('/api/projects/' + id + '/routing') : await inventory();
    if (!valid()) return;
    form(
      id ? '项目测试命令与 Hooks' : '空间测试命令与强制 Hooks',
      area('settings', '声明式执行配置', data.execution.settings) +
        '<p>tests 使用 name / argv / cwd / timeout_seconds。Hooks 使用 name / actions / handler。空间 deny 或 require_rch 不能由项目覆盖解除。不可执行任意 hook 脚本。</p>',
      (node) =>
        send(id ? '/api/projects/' + id + '/execution' : root + '/execution', {
          expected_version: data.execution.version,
          settings: JSON.parse(node.elements.settings.value),
        }),
    );
  }
  async function metadata(id) {
    const valid = intent(),
      detail = await api('/api/projects/' + id + '/routing');
    if (!valid()) return;
    const p = detail.project;
    form(
      '逻辑项目设置',
      input('alias', '别名', p.alias, 'required') +
        input('description', '说明', p.description || '') +
        select(
          'mode',
          '文件权限',
          option('read', '只读', p.mode === 'read') + option('write', '可写', p.mode === 'write'),
        ) +
        `<label class="check"><input type="checkbox" name="tasks" ${p.allow_tasks ? 'checked' : ''}>允许执行任务</label>`,
      (node) =>
        send('/api/projects/' + id + '/metadata', {
          alias: node.elements.alias.value,
          description: node.elements.description.value,
          mode: node.elements.mode.value,
          allow_tasks: node.elements.tasks.checked,
          expected_version: p.routing_version,
        }),
    );
  }
  async function role(id) {
    const valid = intent(),
      roles = (await api('/api/access-roles')).roles;
    if (!valid()) return;
    if (!roles.length) throw new Error('请先建立角色');
    const f = form(
      '路线使用授权',
      select('role', '角色', roles.map((r) => option(r.id, r.label)).join('')) +
        `<label class="check"><input type="checkbox" name="all">明确允许此路线用于全部现有及未来项目（仍须项目操作权限）</label><div id="routing-role-projects"></div><p>这是具体路线权限，不会新增项目读写权，也不会绕过设备本机授权。固定连接保持创建时的路线和项目上限。</p>`,
      (node) => {
        const old = roles.find((r) => r.id === node.elements.role.value);
        const selected = $$('[name="project"]:checked', node).map((el) => el.value);
        const rules = (old.route_rules || []).filter((r) => r.route_id !== id);
        if (selected.length || node.elements.all.checked)
          rules.push({ route_id: id, projects: selected, all_projects: node.elements.all.checked });
        return send('/api/access-roles/' + old.id, {
          label: old.label,
          enabled: old.enabled,
          project_rules: old.project_rules,
          device_rules: old.device_rules,
          connector_rules: old.connector_rules,
          vps_rules: old.vps_rules,
          plugin_rules: old.plugin_rules,
          route_rules: rules,
          expected_version: old.version,
        });
      },
    );
    const draw = () => {
      const selected = (
        roles.find((r) => r.id === f.node.elements.role.value).route_rules || []
      ).filter((r) => r.route_id === id);
      f.node.elements.all.checked = selected.some((r) => r.all_projects);
      $('#routing-role-projects', f.dialog).innerHTML = (S.projects || [])
        .map(
          (p) =>
            `<label class="check"><input type="checkbox" name="project" value="${esc(p.id)}" ${selected.some((r) => r.projects.includes(p.id)) ? 'checked' : ''}>${esc(p.alias)}</label>`,
        )
        .join('');
    };
    f.node.elements.role.onchange = draw;
    draw();
  }
  document.addEventListener('click', (event) => {
    const node = event.target.closest('[data-routing]');
    if (!node || node.disabled) return;
    const action = {
      source,
      mount,
      route,
      defaults,
      batch,
      binding,
      override,
      execution,
      metadata,
      role,
    }[node.dataset.routing];
    if (action) action(node.dataset.routingId).catch((error) => toast(error.message, true));
  });
  return { html };
})();
