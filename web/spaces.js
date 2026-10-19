'use strict';
// Workspace IDs here are explicit page/resource addresses, never login state.
window.CodePierSpaces = (() => {
  const enc = encodeURIComponent;
  const field = (name, label, value = '', attrs = '') =>
    `<label class="field">${esc(label)}<input name="${name}" value="${esc(value ?? '')}" ${attrs}></label>`;
  const select = (name, label, choices, value = '', attrs = '') =>
    `<label class="field">${esc(label)}<select name="${name}" ${attrs}>${choices.map(([id, text]) => `<option value="${esc(id)}" ${String(id) === String(value) ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select></label>`;
  const check = (name, label, on = false) =>
    `<label class="check"><input type="checkbox" name="${name}" ${on ? 'checked' : ''}>${esc(label)}</label>`;
  const textArea = (name, label, value = '', attrs = '') =>
    `<label class="field">${esc(label)}<textarea name="${name}" rows="5" ${attrs}>${esc(value)}</textarea></label>`;
  const multi = (name, label, choices, selected = []) =>
    `<fieldset class="ws-options"><legend>${esc(label)}</legend>${choices.map(([id, text]) => `<label class="check"><input type="checkbox" name="${name}" value="${esc(id)}" ${selected.includes(id) ? 'checked' : ''}>${esc(text)}</label>`).join('') || '<p class="form-note">没有可用选项。</p>'}</fieldset>`;
  const values = (form, name) =>
    [...form.querySelectorAll(`input[name="${name}"]:checked`)].map((n) => n.value);
  const options = (rows = [], label = 'name') =>
    rows.map((r) => [r.id, r[label] || r.alias || r.label || r.id]);
  const pretty = (value) =>
    `<pre class="code-box ws-json">${esc(JSON.stringify(value, null, 2))}</pre>`;
  const panel = (title, body, actions = '') =>
    `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2><div class="actions">${actions}</div></div><div class="panel-body">${body}</div></section>`;
  const button = (action, title, id = '', attrs = '') =>
    `<button type="button" class="btn small" data-ws-action="${action}" data-id="${esc(id)}" ${attrs}>${esc(title)}</button>`;
  const row = (title, detail, actions = '') =>
    `<article class="grant-row"><div><h3>${esc(title)}</h3><div class="form-note ws-wrap">${detail}</div></div><div class="actions">${actions}</div></article>`;
  const root = (id) => '/api/spaces/' + enc(id);
  const change = (old, body, key) => ({
    ...body,
    expected_version: old?.version || 0,
    idempotency_key: key,
  });
  const save = (url, body, update = false) =>
    api(url, {
      method: update ? 'PUT' : 'POST',
      body: JSON.stringify(body),
      requestTimeout: 50000,
    });
  const route = () => {
    const [path] = location.hash.slice(1).split('?');
    const parts = path.split('/');
    return { id: parts[0] === 'spaces' ? parts[1] || '' : '', tab: parts[2] || 'projects' };
  };
  let snapshot = null;
  const tabs = [
    ['projects', 'Projects'],
    ['folders', 'Folders'],
    ['storage', 'Shared Storage'],
    ['devices', 'Devices'],
    ['vps', 'VPS'],
    ['shell', 'Shell'],
    ['profiles', 'Profiles'],
    ['connections', 'MCP Connections'],
    ['members', '成员与权限'],
    ['mcp', 'MCP Services'],
  ];
  const titles = Object.fromEntries(tabs);
  const manage = (space, capability) =>
    space.management?.includes(capability) || space.management?.includes('workspace');
  const newKey = () => 'workspace-' + uid();

  function viewIntent() {
    const login = S.session,
      page = location.hash,
      modalId = S.modalIntent || 0;
    return () => login === S.session && page === location.hash && modalId === (S.modalIntent || 0);
  }
  function form(title, body, submit, setup = null, done = null) {
    const login = S.session,
      page = location.hash;
    let key = newKey();
    const d = modal(
      title,
      `<form id="workspace-form">${body}<p data-ws-status role="status"></p></form>`,
      '<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="workspace-form">保存</button>',
      true,
    );
    const f = $('#workspace-form', d),
      status = $('[data-ws-status]', d),
      b = $('button[type="submit"]', d);
    const current = () => d.isConnected && login === S.session && page === location.hash;
    let submitted = null,
      attemptedValues = null;
    f.onsubmit = (event) => {
      event.preventDefault();
      if (!current() || f.dataset.saving || f.dataset.uncertain || !f.reportValidity()) return;
      const values = JSON.stringify([...new FormData(f).entries()]);
      if (attemptedValues !== null && attemptedValues !== values) {
        status.textContent =
          '原保存结果尚未核实；不能使用原保存键提交修改后的配置。请先核对原校验回执。';
        return;
      }
      attemptedValues = values;
      f.dataset.saving = 'true';
      void busy(b, async () => {
        try {
          status.textContent = '正在校验并保存…';
          const value = await submit(f, key, submitted);
          if (!current()) return;
          await closeModal(d);
          // Logout, another editor or explicit navigation may have happened
          // during history traversal. Never resurrect the old form's view.
          if (login !== S.session || page !== location.hash || $('.modal')) return;
          if (done) await done(value);
          else {
            await renderPage(false);
            toast('已保存');
          }
        } catch (error) {
          if (!current()) return;
          if (error.code === 'VALIDATION_PENDING') {
            // This validation is an operation receipt, not a second submission.
            submitted = { operation_id: error.operation_id };
            status.innerHTML = `目录校验尚未完成。保留表单不变，完成后按同一个保存键继续。${button('receipt', '查看校验', error.operation_id)}`;
            $('[data-ws-action="receipt"]', status).onclick = () =>
              operationDetail(error.operation_id);
          } else {
            status.textContent =
              error.message + (error.operation_id ? ' · ' + error.operation_id : '');
            if (
              error.code === 'NETWORK_UNCERTAIN' ||
              [408, 500, 502, 503, 504].includes(error.status)
            ) {
              f.dataset.uncertain = 'true';
              status.textContent += '。结果不确定：关闭并刷新列表核对，不自动重新创建。';
            } else {
              // Definite rejection is not an accepted mutation. The next
              // explicit save receives a fresh key; no background replay.
              attemptedValues = null;
              submitted = null;
              key = newKey();
            }
            if (error.operation_id) {
              // A command's terminal failure is still an original operation,
              // not a missing submission. Keep an explicit observation entry;
              // viewing it never resubmits the command or changes its target.
              const receipt = document.createElement('button');
              receipt.type = 'button';
              receipt.className = 'btn small';
              receipt.dataset.wsErrorReceipt = error.operation_id;
              receipt.textContent = '查看原操作结果';
              receipt.onclick = () => operationDetail(error.operation_id);
              status.append(' ', receipt);
            }
          }
        } finally {
          delete f.dataset.saving;
        }
      }).finally(() => {
        if (f.dataset.uncertain) b.disabled = true;
      });
    };
    setup?.(f, d);
    return d;
  }

  function projectCollection(projects, { editable = false, workspaceLabels = false } = {}) {
    if (!projects.length) return empty('进入工作空间，先添加 Folder 再建立 Project。');
    const cards = projects
      .map((p) => {
        const search = [
          p.alias,
          p.description,
          p.root,
          p.device_name,
          workspaceLabels ? p.workspace?.label : '',
        ]
          .filter(Boolean)
          .join(' ')
          .toLocaleLowerCase();
        return `<article class="panel workspace-project-row" data-cp-key="project:${esc(p.id)}" data-project-row data-project-search="${esc(search)}">
        <header class="workspace-project-head"><span class="project-index" aria-hidden="true">${icon('folder')}</span><div><span class="project-cover-label">PROJECT</span><h3>${esc(p.alias)}</h3>${p.description ? `<p>${esc(p.description)}</p>` : ''}</div>${onlineBadge(p.online)}</header>
        <div class="workspace-project-path"><span>工作空间目录</span><code title="${esc(p.root || '')}">${esc(p.root || '来源不可用')}</code></div>
        <dl class="workspace-project-facts"><div><dt>来源设备</dt><dd>${esc(p.device_name || '未解析')}</dd></div><div><dt>文件权限</dt><dd>${p.mode === 'write' ? '可编辑' : '只读'}</dd></div><div><dt>工作空间</dt><dd>${workspaceLabels ? esc(p.workspace?.label || '') : '当前页面的工作空间'}</dd></div></dl>
        <footer class="workspace-project-actions"><div class="actions"><button class="btn" data-project-detail="${esc(p.id)}">项目详情</button><button class="btn primary" data-action="open-project" data-id="${esc(p.id)}">工作台 ${icon('arrow')}</button></div>${editable ? button('project', '目录 / 设置', p.id, `aria-label="编辑 ${esc(p.alias)} 的工作空间目录"`) : ''}</footer>
      </article>`;
      })
      .join('');
    return (
      `<div class="project-search workspace-project-search"><label for="project-query">${icon('search')}<input id="project-query" type="search" aria-label="筛选项目" placeholder="搜索项目、设备或路径"></label><small id="project-count" role="status"></small></div>
      <section class="workspace-project-collection">${cards}<div class="empty panel" id="project-no-match" hidden><p>没有匹配的项目</p><button type="button" class="btn ghost small" data-ui="clear-project-query">清除筛选</button></div></section>` +
      uiHelp(
        '工作空间目录与权限',
        'Project 引用工作空间中的一个 Folder 及其根目录或子目录。可选执行设备来自该来源的映射；权限由成员和 Profile 决定，默认设备不可用时不会自动换机。',
      )
    );
  }

  async function html() {
    const address = route(),
      login = S.session,
      page = location.hash;
    if (!address.id) {
      const out = await api('/api/spaces');
      if (login !== S.session || page !== location.hash) return '';
      snapshot = { spaces: out.spaces };
      return `<div id="workspace-page">${heading('工作空间', 'WORKSPACES', '把资源放进工作空间，配置映射、成员和连接权限。', button('new-space', '新建工作空间'))}${panel('工作空间', out.spaces.map((x) => row(x.label, '资源和 MCP 连接独立绑定到这个工作空间。', `<a class="btn primary" href="#spaces/${enc(x.id)}">打开</a>`)).join('') || empty('先建立一个工作空间。'))}</div>`;
    }
    const space = await api(root(address.id));
    const allowedTabs = tabs.filter(
      ([id]) =>
        !['members', 'folders', 'storage', 'devices', 'shell', 'mcp'].includes(id) ||
        manage(space, id === 'members' ? 'members' : 'resources'),
    );
    if (!allowedTabs.some(([id]) => id === address.tab))
      throw new Error('这个工作空间没有提供该管理页面。');
    let data, content;
    if (address.tab === 'shell') {
      data = await api(root(space.id) + '/shell');
      content = panel(
        'Shell 继承',
        pretty(data.settings) +
          '<p>继承来源时，每个 Project 按自己的 Folder 来源解析默认设备；不会任选一台在线设备。</p>',
        manage(space, 'resources') ? button('shell', '编辑 Shell') : '',
      );
    } else if (address.tab === 'mcp') {
      content =
        (await CodePierGateway.html(space.id)) + (await CodePierExternalPlugins.html(space.id));
    } else {
      data = await api(root(space.id) + '/' + address.tab);
      const rows = data[address.tab] || [];
      const editable = manage(
        space,
        address.tab === 'profiles'
          ? 'profiles'
          : address.tab === 'members'
            ? 'members'
            : 'resources',
      );
      const add =
        address.tab === 'connections' || editable
          ? button('new-' + address.tab, '添加 ' + titles[address.tab]) +
            (address.tab === 'members' && editable ? button('invite', '建立邀请') : '')
          : '';
      if (address.tab === 'projects') content = projectCollection(rows, { editable });
      if (address.tab === 'folders')
        content = rows
          .map((x) =>
            row(
              x.name,
              `${esc(x.source_kind)} · ${esc(x.device_path || x.subpath)} · ${x.mode === 'write' ? '可写' : '只读'} · ${x.allow_tasks ? '允许命令' : '不允许命令'}<br>${(x.views || []).map((v) => `${esc(v.name)} → <code>${esc(v.root)}</code>`).join('<br>')}${x.source_error ? '<br>' + esc(x.source_error) : ''}`,
              button('folder', '编辑', x.id) +
                button('browse', '浏览', x.id) +
                button('remove-folder', '移除', x.id),
            ),
          )
          .join('');
      if (address.tab === 'storage')
        content = rows
          .map((x) =>
            row(
              x.name,
              `<code>${esc(x.abstract_base)}</code><br>${(x.mappings || []).map((m) => `${esc(m.device_name)} → <code>${esc(m.path)}</code>`).join('<br>')}<br>默认 Shell：${esc(x.default_device_id)} · 文件设备：${esc(x.files_device_id)}`,
              button('storage', '编辑映射与默认设备', x.id) +
                button('remove-storage', '移除', x.id),
            ),
          )
          .join('');
      if (address.tab === 'devices')
        content = rows
          .map((x) =>
            row(
              x.name,
              `${x.online ? '在线' : '离线'} · ${x.info?.platform ? esc(x.info.platform) : ''}`,
              button('device-detail', '设备详情', x.id),
            ),
          )
          .join('');
      if (address.tab === 'vps')
        content = rows
          .map((x) =>
            row(
              x.name,
              `${esc(x.host)} · ${esc({ top_level: '顶层 VPS', project_only: '仅 Project 类别', both: '顶层与 Project 类别' }[x.display_mode])}<br>${x.projects.map((p) => esc(p.alias)).join(' · ')}${x.runner_name ? '<br>Runner：' + esc(x.runner_name) + ' / ' + esc(x.connection_ref) : ''}`,
              (editable ? button('vps', '配置', x.id) + button('remove-vps', '移除', x.id) : '') +
                (x.can_execute ? button('vps-command', '运行命令', x.id) : ''),
            ),
          )
          .join('');
      if (address.tab === 'profiles')
        content = rows
          .map((x) =>
            row(
              x.label,
              `${x.enabled ? '启用' : '停用'} · v${x.version}<details><summary>资源与操作权限</summary>${pretty(x.policy)}</details>`,
              editable ? button('profile', '编辑权限', x.id) : '',
            ),
          )
          .join('');
      if (address.tab === 'connections')
        content = rows
          .map((x) =>
            row(
              x.name,
              `${esc(x.profile?.label || '')} · ${esc(x.status)} · ${x.consent_mode === 'live' ? '明确同意跟随 Profile' : '固定同意范围'} · 到期 ${esc(timeText(x.expires))}<details><summary>有效权限 / 连接身份</summary><code>${esc(x.id)}</code>${pretty(x.permissions)}</details>${x.unavailable_reason ? '<p>' + esc(x.unavailable_reason) + '</p>' : ''}`,
              x.status !== 'revoked'
                ? button('plugin-consent', '外部插件动作同意', x.id) +
                    button('revoke', '撤销', x.id)
                : '',
            ),
          )
          .join('');
      if (address.tab === 'members') {
        content = rows
          .map((x) =>
            row(
              x.display_name || x.username,
              `${esc(x.source)} · ${x.active && !x.blocked ? '有效' : '暂停'}<details><summary>直接成员权限</summary>${pretty(x.policy)}</details>`,
              (x.source === 'manual' ? button('member', '编辑直接权限', x.user_id) : '') +
                button(
                  'suspend-member',
                  x.blocked ? '恢复成员' : '暂停成员',
                  x.user_id,
                  `data-blocked="${x.blocked ? '1' : '0'}"`,
                ),
            ),
          )
          .join('');
        if (editable) {
          const invited = await api(root(space.id) + '/invites');
          data.invitations = invited.invitations;
          content +=
            '<h3>工作空间邀请</h3>' +
            invited.invitations
              .map((invite) =>
                row(
                  invite.used_by ? '已使用的邀请' : '待接受的邀请',
                  `到期 ${esc(timeText(invite.expires))}<details><summary>邀请授予的直接权限</summary>${pretty(invite.policy)}</details>`,
                  invite.used_by
                    ? ''
                    : button(
                        'revoke-invite',
                        '撤销邀请',
                        invite.id,
                        `data-ws-invite="${esc(invite.id)}"`,
                      ),
                ),
              )
              .join('');
        }
      }
      content = panel(titles[address.tab], content || empty('尚无项目。'), add);
    }
    if (login !== S.session || page !== location.hash) return '';
    snapshot = { space, address, data };
    const pending = new URLSearchParams(location.search).get('authorize');
    return `<div id="workspace-page">${heading(space.label, 'WORKSPACE', '资源先进入工作空间；Project 只使用其 Folder 已提供的设备。', `<a class="btn" href="#spaces">全部工作空间</a>${manage(space, 'workspace') ? button('settings', '工作空间设置') : ''}`)}<nav class="product-tabs" aria-label="工作空间设置">${allowedTabs.map(([id, label]) => `<a class="btn ${address.tab === id ? 'primary' : 'ghost'}" href="#spaces/${enc(space.id)}/${id}" ${address.tab === id ? 'aria-current="page"' : ''}>${esc(label)}</a>`).join('')}</nav>${pending && address.tab === 'profiles' ? notice(`建立 Profile 后仍需返回确认授权。${button('return-consent', '返回 MCP 授权', pending)}`) : ''}${content}</div>`;
  }

  async function chooseSpace(title, proceed) {
    const out = await api('/api/spaces');
    if (!out.spaces.length) throw new Error('先建立工作空间，然后分配资源。');
    const d = modal(
      title,
      select(
        'space',
        '工作空间',
        [['', '请选择工作空间'], ...options(out.spaces, 'label')],
        '',
        'required',
      ),
      '<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" id="workspace-next">继续</button>',
    );
    $('#workspace-next', d).onclick = () =>
      busy($('#workspace-next', d), async () => {
        const id = $('select', d).value;
        if (!id) throw new Error('请选择工作空间。');
        await closeModal(d);
        await proceed(id);
      });
  }
  async function newSpace() {
    form(
      '新建工作空间',
      field('label', '名称', '', 'required maxlength="100"'),
      (f, key) => post('/api/spaces', { label: f.elements.label.value, idempotency_key: key }),
      null,
      async (x) => {
        await CodePierIdentity.refresh();
        await navigate('spaces/' + enc(x.id));
      },
    );
  }
  async function settings(space) {
    form(
      '工作空间设置',
      field('label', '名称', space.label, 'required maxlength="100"') +
        check('active', '启用工作空间', !!space.active),
      (f, key) =>
        save(
          root(space.id),
          change(space, { label: f.elements.label.value, active: f.elements.active.checked }, key),
          true,
        ),
    );
  }
  async function bindDevice(space) {
    const inventory = (await api('/api/devices')).devices;
    form(
      '将设备放进工作空间',
      select('device', '设备', options(inventory), '', 'required'),
      (f, key) =>
        save(
          root(space.id) + '/devices/' + enc(f.elements.device.value),
          change(null, { enabled: true }, key),
          true,
        ),
    );
  }
  async function folderEditor(space, old = null) {
    const [devices, storage] = await Promise.all([
      api(root(space.id) + '/devices'),
      api(root(space.id) + '/storage'),
    ]);
    const d = form(
      old ? '编辑 Folder' : '添加 Folder',
      field('name', '名称', old?.name, 'required maxlength="100"') +
        select(
          'source_kind',
          '来源',
          [
            ['device', '设备目录'],
            ['storage', '工作空间共享存储'],
          ],
          old?.source_kind || 'device',
        ) +
        '<div data-source-device>' +
        select('device_id', '工作空间设备', options(devices.devices), old?.device_id) +
        field(
          'device_path',
          '设备绝对路径',
          old?.device_path,
          'maxlength="4096" placeholder="/srv/projects"',
        ) +
        '</div>' +
        '<div data-source-storage>' +
        select(
          'storage_binding_id',
          '工作空间存储',
          options(storage.storage),
          old?.storage_binding_id,
        ) +
        field('subpath', '存储中的子目录', old?.subpath || '.', 'maxlength="4096"') +
        '</div>' +
        select(
          'mode',
          '文件访问',
          [
            ['read', '只读'],
            ['write', '可写'],
          ],
          old?.mode || 'read',
        ) +
        check('allow_tasks', '允许命令执行', !!old?.allow_tasks) +
        check('enabled', '启用', old ? !!old.enabled : true) +
        notice(
          '绑定不会创建或挂载目录。路径必须由设备验证；一个工作空间可以添加同一设备的多个目录。',
        ),
      (f, key) => {
        const v = f.elements,
          kind = v.source_kind.value;
        const body = {
          name: v.name.value,
          source_kind: kind,
          mode: v.mode.value,
          allow_tasks: v.allow_tasks.checked,
          enabled: v.enabled.checked,
          ...(kind === 'device'
            ? { device_id: v.device_id.value, device_path: v.device_path.value, subpath: '.' }
            : { storage_binding_id: v.storage_binding_id.value, subpath: v.subpath.value }),
        };
        return save(
          root(space.id) + '/folders' + (old ? '/' + enc(old.id) : ''),
          change(old, body, key),
          !!old,
        );
      },
      (f, d) => {
        const update = () => {
          const direct = f.elements.source_kind.value === 'device';
          $('[data-source-device]', d).hidden = !direct;
          $('[data-source-storage]', d).hidden = direct;
          f.elements.device_path.required = direct;
          f.elements.device_id.required = direct;
          f.elements.storage_binding_id.required = !direct;
          f.elements.subpath.required = !direct;
          if (f.elements.mode.value !== 'write') {
            f.elements.allow_tasks.checked = false;
            f.elements.allow_tasks.disabled = true;
          } else f.elements.allow_tasks.disabled = false;
        };
        f.elements.source_kind.onchange = update;
        f.elements.mode.onchange = update;
        update();
      },
    );
    return d;
  }
  async function browse(space, folder, path = '.') {
    const data = await settled(
      api(
        root(space.id) + '/folders/' + enc(folder.id) + '/browse?' + new URLSearchParams({ path }),
      ),
    );
    modal(
      'Folder · ' + folder.name,
      pretty(data) + notice('Project 可绑定根目录 “.” 或其中一个相对子目录。'),
    );
  }
  async function projectEditor(id = null, spaceId = '') {
    const current = viewIntent();
    if (!id && !spaceId)
      return chooseSpace('Project 所属工作空间', (chosen) => projectEditor(null, chosen));
    let old = null;
    if (id) {
      await loadBasics();
      if (!current()) return;
      old = S.projects.find((p) => p.id === id);
      if (!old) throw new Error('Project 不存在或不可访问。');
      spaceId = old.workspace?.id;
    }
    if (!spaceId) throw new Error('无法定位 Project 的工作空间；请从工作空间打开该项目。');
    const folders = (await api(root(spaceId) + '/folders')).folders.filter((x) => x.enabled);
    if (!current()) return;
    if (!folders.length) throw new Error('先在这个工作空间添加 Folder，再建立 Project。');
    form(
      old ? '编辑 Project' : '新建 Project',
      field('alias', '项目名称', old?.alias, 'required maxlength="100"') +
        field('description', '说明', old?.description, 'maxlength="2000"') +
        select('folder_id', '工作空间 Folder', options(folders), old?.folder_id, 'required') +
        field(
          'subpath',
          '根目录或相对子目录',
          old?.subpath || '.',
          'required maxlength="4096" aria-describedby="project-subpath-help"',
        ) +
        '<p class="form-note" id="project-subpath-help">使用工作空间 Folder 的相对子目录；“.” 使用 Folder 根目录，不能越出范围。</p>' +
        '<div data-folder-preview class="form-note"></div>' +
        notice('每个 Project 只引用一个 Folder。使用 “.” 绑定根目录；不能越出 Folder 范围。'),
      (f, key) =>
        save(
          old ? '/api/projects/' + enc(id) : root(spaceId) + '/projects',
          change(
            old,
            {
              alias: f.elements.alias.value,
              description: f.elements.description.value,
              folder_id: f.elements.folder_id.value,
              subpath: f.elements.subpath.value,
            },
            key,
          ),
          !!old,
        ),
      (f, d) => {
        const preview = () => {
          const x = folders.find((x) => x.id === f.elements.folder_id.value);
          $('[data-folder-preview]', d).innerHTML = (x?.views || [])
            .map((v) => `${esc(v.name)} → <code>${esc(v.logical_root || v.root)}</code>`)
            .join('<br>');
        };
        f.elements.folder_id.onchange = preview;
        preview();
      },
    );
  }
  async function projectDetail(id) {
    const current = viewIntent();
    await loadBasics();
    if (!current()) return;
    const old = S.projects.find((x) => x.id === id);
    if (!old) throw new Error('Project 不可访问。');
    const result = await api('/api/projects/' + enc(id) + '/vps');
    if (!current()) return;
    const d = modal(
      'Project · ' + old.alias,
      `<p>${esc(old.description)}</p><dl class="kv"><dt>工作目录</dt><dd><code>${esc(old.root)}</code></dd><dt>工作空间</dt><dd>${esc(old.workspace?.label || '')}</dd></dl>` +
        panel(
          'VPS',
          result.vps
            .map((v) =>
              row(
                v.name,
                esc(v.host),
                v.can_execute ? button('project-vps-command', '运行命令', v.id) : '',
              ),
            )
            .join('') || empty('没有在这个 Project 类别显示的 VPS。'),
        ),
      `<button class="btn" data-ws-project-folder>Folder</button><button class="btn" data-ws-project-shell>Shell</button><button class="btn" data-ws-project-artifacts>产物 / 下载</button><button class="btn primary" data-ws-project-tools>开发工具</button>`,
      true,
    );
    $('[data-ws-project-folder]', d).onclick = async () => {
      await closeModal(d);
      await projectEditor(id);
    };
    $('[data-ws-project-shell]', d).onclick = async () => {
      await closeModal(d);
      await shellEditor(old.workspace?.id, id);
    };
    $('[data-ws-project-artifacts]', d).onclick = async () => {
      await closeModal(d);
      await navigate('project/' + enc(id) + '/artifacts');
    };
    $('[data-ws-project-tools]', d).onclick = async () => {
      await closeModal(d);
      await navigate('project/' + enc(id) + '/tools');
    };
    $$('[data-ws-action="project-vps-command"]', d).forEach(
      (b) =>
        (b.onclick = () => {
          closeModal(d);
          void commandVPS(
            result.vps.find((x) => x.id === b.dataset.id),
            id,
          );
        }),
    );
  }
  async function shellEditor(spaceId, projectId = '') {
    const url = projectId ? '/api/projects/' + enc(projectId) + '/shell' : root(spaceId) + '/shell';
    const state = await api(url);
    const devices = projectId
      ? state.devices
      : (await api(root(spaceId) + '/devices')).devices.filter(
          (x) => x.info?.shell_enabled !== false,
        );
    const old = state.settings;
    form(
      projectId ? 'Project Shell' : '工作空间 Shell',
      select(
        'mode',
        '执行设置',
        [
          ['inherit', projectId ? '继承工作空间' : '继承 Folder 来源'],
          ['custom', '自定义'],
          ['disabled', '停用'],
        ],
        old.mode,
      ) +
        '<div data-custom-shell>' +
        select(
          'default_device_id',
          '默认执行设备',
          [['', '请选择'], ...options(devices)],
          old.default_device_id || '',
        ) +
        multi(
          'allowed_device_ids',
          '允许的设备（不选＝该来源提供的全部可用选项）',
          options(devices),
          old.allowed_device_ids,
        ) +
        '</div>' +
        (state.resolved
          ? `<details><summary>当前解析结果</summary>${pretty(state.resolved)}</details>`
          : '') +
        (state.resolved_error ? notice(esc(state.resolved_error.message)) : '') +
        notice('自定义只选择工作空间 Folder 提供的设备。默认设备不可用时失败，不自动切换。'),
      (f, key) => {
        const custom = f.elements.mode.value === 'custom';
        return save(
          url,
          change(
            state,
            {
              settings: {
                mode: f.elements.mode.value,
                ...(custom
                  ? {
                      default_device_id: f.elements.default_device_id.value,
                      allowed_device_ids: values(f, 'allowed_device_ids'),
                    }
                  : {}),
              },
            },
            key,
          ),
          true,
        );
      },
      (f, d) => {
        const update = () => {
          const on = f.elements.mode.value === 'custom';
          $('[data-custom-shell]', d).hidden = !on;
          f.elements.default_device_id.required = on;
        };
        f.elements.mode.onchange = update;
        update();
      },
    );
  }

  async function poolEditor(old = null) {
    form(
      old ? '编辑 Shared Storage' : '新增 Shared Storage',
      field('name', '名称', old?.name, 'required maxlength="100"') +
        field(
          'abstract_base',
          '逻辑根路径',
          old?.abstract_base || '/storage/',
          'required maxlength="4096"',
        ) +
        check('enabled', '启用', old ? !!old.enabled : true) +
        notice('逻辑路径不是设备上的真实挂载。添加设备映射后，仍需在工作空间纳入这些映射。'),
      (f, key) =>
        save(
          '/api/storage-pools' + (old ? '/' + enc(old.id) : ''),
          change(
            old,
            {
              name: f.elements.name.value,
              abstract_base: f.elements.abstract_base.value,
              enabled: f.elements.enabled.checked,
            },
            key,
          ),
          !!old,
        ),
    );
  }
  async function mappings(pool) {
    const [data, inventory] = await Promise.all([
      api('/api/storage-pools/' + enc(pool.id) + '/mappings'),
      api('/api/devices'),
    ]);
    const d = modal(
      '存储映射 · ' + pool.name,
      data.mappings
        .map((x) =>
          row(
            x.device_name,
            `<code>${esc(x.path)}</code> · ${x.enabled ? '启用' : '停用'}`,
            button('mapping-edit', '编辑', x.id) + button('mapping-remove', '移除', x.id),
          ),
        )
        .join('') || empty('先在设备上挂载相同的存储，再添加路径映射。'),
      '<button class="btn ghost" data-action="close-modal">关闭</button><button class="btn primary" data-new-mapping>添加映射</button>',
      true,
    );
    const editor = (old = null) => {
      closeModal(d);
      form(
        '设备路径映射',
        select(
          'device_id',
          '设备',
          options(inventory.devices),
          old?.device_id,
          old ? 'disabled' : 'required',
        ) +
          field('path', '设备上的真实绝对路径', old?.path, 'required maxlength="4096"') +
          check('enabled', '启用', old ? !!old.enabled : true) +
          check(
            'confirmed_same_storage',
            '我已确认各设备映射的是同一份共享数据，不是独立 Git clone',
          ) +
          notice('系统将通过这台设备校验目录。不会挂载文件系统或同步内容。'),
        (f, key) =>
          save(
            '/api/storage-pools/' + enc(pool.id) + '/mappings' + (old ? '/' + enc(old.id) : ''),
            change(
              old,
              {
                device_id: f.elements.device_id.value,
                path: f.elements.path.value,
                enabled: f.elements.enabled.checked,
                confirmed_same_storage: f.elements.confirmed_same_storage.checked,
              },
              key,
            ),
            !!old,
          ),
        (f) => {
          f.elements.confirmed_same_storage.required = true;
        },
        () => mappings(pool),
      );
    };
    $('[data-new-mapping]', d).onclick = () => editor();
    $$('[data-ws-action="mapping-edit"]', d).forEach(
      (b) => (b.onclick = () => editor(data.mappings.find((x) => x.id === b.dataset.id))),
    );
    $$('[data-ws-action="mapping-remove"]', d).forEach(
      (b) =>
        (b.onclick = () =>
          busy(b, async () => {
            const x = data.mappings.find((x) => x.id === b.dataset.id);
            if (!confirm('仅移除映射登记？实物文件不会被删除。')) return;
            await api(
              '/api/storage-pools/' +
                enc(pool.id) +
                '/mappings/' +
                enc(x.id) +
                '?expected_version=' +
                x.version,
              { method: 'DELETE' },
            );
            closeModal(d);
            await mappings(pool);
          })),
    );
  }
  async function storageEditor(space, old = null) {
    const [pools, devices] = await Promise.all([
      api('/api/storage-pools'),
      api(root(space.id) + '/devices'),
    ]);
    let mapRows = [],
      load = 0;
    const d = form(
      '工作空间共享存储',
      field('name', '名称', old?.name, 'required maxlength="100"') +
        select(
          'pool_id',
          '存储资源',
          [['', '请选择'], ...options(pools.pools)],
          old?.pool_id,
          old ? 'disabled' : 'required',
        ) +
        '<div data-storage-mappings></div><div data-storage-devices></div>' +
        check('enabled', '启用', old ? !!old.enabled : true) +
        notice(
          '只纳入这个工作空间的设备映射。默认 Shell 设备必选；允许清单不选表示全部符合条件的映射设备。',
        ),
      (f, key) =>
        save(
          root(space.id) + '/storage' + (old ? '/' + enc(old.id) : ''),
          change(
            old,
            {
              pool_id: f.elements.pool_id.value,
              name: f.elements.name.value,
              mapping_ids: values(f, 'mapping_ids'),
              default_device_id: f.elements.default_device_id?.value || '',
              files_device_id: f.elements.files_device_id?.value || '',
              allowed_device_ids: values(f, 'allowed_device_ids'),
              enabled: f.elements.enabled.checked,
            },
            key,
          ),
          !!old,
        ),
    );
    const f = $('form', d);
    const updateDevices = () => {
      const selected = mapRows.filter((x) => values(f, 'mapping_ids').includes(x.id));
      const executable = selected.filter(
        (x) =>
          x.allow_tasks &&
          x.writable &&
          devices.devices.find((d) => d.id === x.device_id)?.info?.shell_enabled !== false,
      );
      const previousDefault = f.elements.default_device_id?.value || old?.default_device_id || '';
      const previousFiles = f.elements.files_device_id?.value || old?.files_device_id || '';
      const previousAllowed = f.querySelector('[name="allowed_device_ids"]')
        ? values(f, 'allowed_device_ids')
        : old?.allowed_device_ids || [];
      $('[data-storage-devices]', d).innerHTML =
        select(
          'files_device_id',
          '文件操作设备',
          [['', '请选择'], ...selected.map((x) => [x.device_id, x.device_name])],
          previousFiles,
          'required',
        ) +
        select(
          'default_device_id',
          '默认 Shell 设备',
          [['', '请选择'], ...executable.map((x) => [x.device_id, x.device_name])],
          previousDefault,
          'required',
        ) +
        multi(
          'allowed_device_ids',
          'Shell 允许设备（不选即全部已纳入的可执行映射）',
          executable.map((x) => [x.device_id, x.device_name]),
          previousAllowed,
        );
    };
    const loadMappings = async () => {
      const ticket = ++load,
        id = f.elements.pool_id.value;
      $('[data-storage-mappings]', d).replaceChildren();
      $('[data-storage-devices]', d).replaceChildren();
      if (!id) return;
      const out = await api('/api/storage-pools/' + enc(id) + '/mappings');
      if (!d.isConnected || ticket !== load) return;
      mapRows = out.mappings.filter(
        (x) => x.enabled && devices.devices.some((d) => d.id === x.device_id),
      );
      $('[data-storage-mappings]', d).innerHTML = multi(
        'mapping_ids',
        '纳入工作空间的设备路径',
        mapRows.map((x) => [x.id, x.device_name + ' → ' + x.path]),
        old?.mapping_ids || old?.mappings?.map((x) => x.id) || [],
      );
      $$('input[name="mapping_ids"]', d).forEach((n) => (n.onchange = updateDevices));
      updateDevices();
    };
    f.elements.pool_id.onchange = () => void loadMappings().catch((e) => toast(e.message, true));
    await loadMappings();
  }

  function policyEditorHTML(policy = {}, member = false) {
    // Explicit resource/action pairs remain editable without invented roles.
    return (
      (member
        ? multi(
            'manage',
            '直接管理权限',
            ['workspace', 'members', 'profiles', 'resources', 'connections', 'audit'].map((x) => [
              x,
              x,
            ]),
            policy.manage || [],
          )
        : '') +
      check(
        'raw_policy',
        '直接使用完整 JSON 策略（保留全部/未来资源选择器）',
        [...(policy.project_rules || []), ...(policy.vps_rules || [])].some(
          (r) => r.all_projects || r.all_vps || r.excluded_projects?.length,
        ),
      ) +
      '<div data-policy-pairs></div><details aria-label="高级策略配置"><summary>高级策略（外部 MCP / 插件 / 精确选择器）</summary>' +
      textArea(
        'policy_extra',
        '完整策略 JSON',
        JSON.stringify(policy, null, 2),
        'spellcheck="false"',
      ) +
      '<p class="form-note">保存时，下方可视化项目/VPS选择会覆盖 JSON 中对应的规则；外部工具、Folder 委派等其他规则保留。</p></details>'
    );
  }
  async function bindPolicy(f, space, policy = {}) {
    const [projects, vps] = await Promise.all([
      api(root(space.id) + '/projects'),
      api(root(space.id) + '/vps'),
    ]);
    const actionsFor = (rules, id, key) =>
      new Set(
        (rules || [])
          .filter(
            (r) =>
              !(r.excluded_projects || []).includes(id) &&
              (r['all_' + key] || (r[key] || []).includes(id)),
          )
          .flatMap((r) => r.actions),
      );
    const pairs = (kind, rows, actions) =>
      rows
        .map((x) => {
          const allowed = actionsFor(
            policy[kind === 'projects' ? 'project_rules' : 'vps_rules'],
            x.id,
            kind,
          );
          return `<fieldset class="ws-options"><legend>${esc(x.alias || x.name)}</legend>${actions.map((a) => `<label class="check"><input type="checkbox" data-policy-kind="${kind}" data-resource="${esc(x.id)}" value="${a}" ${allowed.has(a) ? 'checked' : ''}>${a}</label>`).join('')}</fieldset>`;
        })
        .join('');
    $('[data-policy-pairs]', f).innerHTML =
      '<h3>Project 权限</h3>' +
      pairs('projects', projects.projects, ['read', 'write', 'execute', 'computer']) +
      '<h3>VPS 权限</h3>' +
      pairs('vps', vps.vps, ['read', 'execute']);
    // Selecting a non-read action includes the required read permission. Clearing
    // read clears its dependent actions; unrelated resources never inherit them.
    $$('[data-policy-kind]', f).forEach(
      (n) =>
        (n.onchange = () => {
          const group = [...n.closest('fieldset').querySelectorAll('input')];
          if (n.checked && n.value !== 'read') group.find((x) => x.value === 'read').checked = true;
          if (!n.checked && n.value === 'read') group.forEach((x) => (x.checked = false));
        }),
    );
  }
  function readPolicy(f, member = false) {
    const policy = JSON.parse(f.elements.policy_extra.value || '{}');
    if (f.elements.raw_policy.checked) {
      if (!member && policy.manage?.length) throw new Error('Profile 不包含管理权限。');
      return policy;
    }
    for (const [kind, key] of [
      ['projects', 'project_rules'],
      ['vps', 'vps_rules'],
    ]) {
      const groups = new Map();
      $$(`input[data-policy-kind="${kind}"]:checked`, f).forEach((n) => {
        if (!groups.has(n.dataset.resource)) groups.set(n.dataset.resource, []);
        groups.get(n.dataset.resource).push(n.value);
      });
      policy[key] = [...groups].map(([id, actions]) => ({ [kind]: [id], actions }));
    }
    policy.manage = member ? values(f, 'manage') : [];
    return policy;
  }
  async function profileEditor(space, old = null) {
    const d = form(
      old ? '编辑 Profile' : '建立 Profile',
      field('label', 'Profile 名称', old?.label, 'required maxlength="80"') +
        check('enabled', '启用 Profile', old ? !!old.enabled : true) +
        policyEditorHTML(old?.policy || {}),
      (f, key) =>
        save(
          root(space.id) + '/profiles' + (old ? '/' + enc(old.id) : ''),
          {
            label: f.elements.label.value,
            enabled: f.elements.enabled.checked,
            policy: readPolicy(f),
            idempotency_key: key,
            ...(old ? { expected_version: old.version } : {}),
          },
          !!old,
        ),
      null,
      async (value) => {
        const request = new URLSearchParams(location.search).get('authorize');
        if (request && !old) await consent(request, value.id);
        else {
          await renderPage(false);
          toast('Profile 已保存');
        }
      },
    );
    const f = $('form', d),
      submit = $('button[type="submit"]', d);
    submit.disabled = true;
    try {
      await bindPolicy(f, space, old?.policy);
      if (d.isConnected) submit.disabled = false;
    } catch (e) {
      $('[data-ws-status]', d).textContent = e.message;
    }
  }
  async function memberEditor(space, old = null) {
    const d = form(
      '直接成员权限',
      field('user_id', '用户 ID', old?.user_id, old ? 'disabled' : 'required maxlength="100"') +
        check('active', '启用人工成员资格', old ? !!old.active : true) +
        policyEditorHTML(old?.policy || {}, true) +
        notice('没有 Role 分配。外部群组来源独立保留；暂停成员会覆盖全部来源。'),
      (f, key) =>
        save(
          root(space.id) + '/members/' + enc(f.elements.user_id.value),
          change(old, { active: f.elements.active.checked, policy: readPolicy(f, true) }, key),
          true,
        ),
    );
    const submit = $('button[type="submit"]', d);
    submit.disabled = true;
    try {
      await bindPolicy($('form', d), space, old?.policy);
      if (d.isConnected) submit.disabled = false;
    } catch (e) {
      $('[data-ws-status]', d).textContent = e.message;
    }
  }
  async function invitationEditor(space) {
    const d = form(
      '建立工作空间邀请',
      field('days', '有效天数', 7, 'type="number" min="1" max="30" required') +
        policyEditorHTML({}, true) +
        notice('邀请直接授予所选工作空间权限，不分配 Role；默认不增加资源权限。'),
      (f) =>
        post(root(space.id) + '/invites', {
          days: Number(f.elements.days.value),
          policy: readPolicy(f, true),
        }),
      null,
      async (result) => {
        const saved = modal(
          '保存工作空间邀请',
          notice('邀请只显示一次，不写入浏览器存储。接受者仍需使用自己的账号登录。') +
            `<div class="code-box secret">${esc(result.invitation)}</div>`,
          '<button class="btn ghost" data-action="close-modal">完成</button><button class="btn primary" data-copy-invitation>复制邀请</button>',
        );
        $('[data-copy-invitation]', saved).onclick = () => copy(result.invitation);
      },
    );
    const submit = $('button[type="submit"]', d);
    submit.disabled = true;
    try {
      await bindPolicy($('form', d), space, {});
      if (d.isConnected) submit.disabled = false;
    } catch (e) {
      $('[data-ws-status]', d).textContent = e.message;
    }
    return d;
  }

  async function newConnection(spaceId = '') {
    if (!spaceId) return chooseSpace('建立 MCP 连接：选择工作空间', newConnection);
    const out = await api(root(spaceId) + '/profiles');
    const profiles = out.profiles.filter((x) => x.enabled);
    const d = form(
      '建立 MCP 连接',
      field('name', '连接名称', '', 'required maxlength="80"') +
        `<p class="form-note">工作空间：${esc(S.identity?.spaces.find((x) => x.id === spaceId)?.label || spaceId)}</p>` +
        select(
          'profile_id',
          '工作空间内的 Profile',
          [['', '请选择 Profile'], ...options(profiles, 'label')],
          '',
          'required',
        ) +
        `<a class="btn ghost" data-create-profile href="#spaces/${enc(spaceId)}/profiles">建立 Profile</a><div data-profile-summary></div>` +
        select(
          'consent_mode',
          '授权方式',
          [
            ['fixed', '固定本次同意范围'],
            ['live', '明确同意跟随 Profile 变化'],
          ],
          'fixed',
        ) +
        check('confirm_live_profile', '我同意之后的 Profile 资源与操作权限变更') +
        check('confirm_external_mcp', '我同意使用 Profile 中明确配置的外部 MCP 工具') +
        field('days', '有效天数', 30, 'type="number" min="1" max="365" required'),
      (f, key) => {
        const profile = profiles.find((x) => x.id === f.elements.profile_id.value);
        if (!profile) throw new Error('请选择该工作空间内的 Profile。');
        return post(root(spaceId) + '/connections', {
          name: f.elements.name.value,
          profile_id: profile.id,
          profile_version: profile.version,
          consent_mode: f.elements.consent_mode.value,
          confirm_live_profile: f.elements.confirm_live_profile.checked,
          confirm_external_mcp: f.elements.confirm_external_mcp.checked,
          days: Number(f.elements.days.value),
          idempotency_key: key,
        });
      },
      (f, d) => {
        f.elements.profile_id.onchange = () => {
          $('[data-profile-summary]', d).innerHTML = pretty(
            profiles.find((x) => x.id === f.elements.profile_id.value)?.policy || {},
          );
        };
        $('[data-create-profile]', d).onclick = () => closeModal(d);
      },
      async (result) => {
        if (!result.token) {
          toast('这是原连接的回执；凭据只在首次响应显示。');
          await renderPage(false);
          return;
        }
        const saved = modal(
          '保存连接凭据',
          notice('只显示一次，不写入浏览器存储。关闭前保存到客户端。') +
            `<div class="code-box secret">${esc(result.token)}</div>`,
          '<button class="btn ghost" data-action="close-modal">完成</button><button class="btn primary" data-copy-credential>复制凭据</button>',
        );
        $('[data-copy-credential]', saved).onclick = () => copy(result.token);
      },
    );
    return d;
  }

  async function consent(id, preferredProfile = '') {
    const request = await api('/api/oauth/requests/' + enc(id));
    let version = request.selection_version,
      selected = '',
      profiles = [],
      choiceSerial = 0,
      consentLocked = false;
    const d = modal(
      '确认 MCP 应用授权',
      `<p><strong>${esc(request.client_name)}</strong> 请求连接一个工作空间。</p><dl class="kv"><dt>客户端</dt><dd>${esc(request.client_id)}</dd><dt>回调</dt><dd class="ws-wrap">${esc(request.redirect_uri)}</dd><dt>资源</dt><dd class="ws-wrap">${esc(request.resource)}</dd></dl>` +
        select(
          'consent_space',
          '1. 选择工作空间',
          [['', '请选择工作空间'], ...options(request.spaces, 'label')],
          '',
        ) +
        select(
          'consent_profile',
          '2. 选择这个工作空间的 Profile',
          [['', '先选择工作空间']],
          '',
          'disabled',
        ) +
        '<a class="btn ghost" data-consent-create hidden>建立 Profile</a><div data-consent-policy></div>' +
        select(
          'consent_mode',
          '授权方式',
          [
            ['fixed', '固定本次同意范围'],
            ['live', '跟随 Profile 变化'],
          ],
          'fixed',
        ) +
        check('confirm_live_profile', '我明确同意未来 Profile 的资源与操作权限变更') +
        check('confirm_external_mcp', '我同意使用 Profile 中明确配置的外部 MCP 工具') +
        '<p data-consent-status role="status"></p>',
      '<button class="btn ghost" data-consent-deny>拒绝</button><button class="btn primary" data-consent-allow disabled>确认授权</button>',
      true,
      false,
    );
    const spaces = $('[name="consent_space"]', d),
      choice = $('[name="consent_profile"]', d),
      allow = $('[data-consent-allow]', d),
      status = $('[data-consent-status]', d),
      create = $('[data-consent-create]', d);
    const updatePolicy = () => {
      const p = profiles.find((x) => x.id === choice.value);
      $('[data-consent-policy]', d).innerHTML = p
        ? `<h3>${esc(p.label)} · v${p.version}</h3>${pretty(p.policy)}`
        : '';
      allow.disabled = consentLocked || !p;
    };
    spaces.onchange = async () => {
      const serial = ++choiceSerial,
        target = spaces.value;
      selected = '';
      profiles = [];
      choice.replaceChildren();
      choice.disabled = true;
      allow.disabled = true;
      create.hidden = true;
      $('[data-consent-policy]', d).replaceChildren();
      if (!target) return;
      spaces.disabled = true;
      status.textContent = '正在加载所选工作空间的 Profiles…';
      try {
        const out = await post('/api/oauth/requests/' + enc(id) + '/select-space', {
          space_id: target,
          selection_version: version,
        });
        if (!d.isConnected || serial !== choiceSerial) return;
        version = out.selection_version;
        selected = out.selected_space_id;
        profiles = out.profiles;
        choice.innerHTML =
          '<option value="">请选择 Profile</option>' +
          profiles.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
        choice.disabled = !profiles.length;
        if (profiles.some((p) => p.id === preferredProfile)) choice.value = preferredProfile;
        create.href = '#spaces/' + enc(selected) + '/profiles';
        create.hidden = false;
        status.textContent = profiles.length
          ? '请核对权限，再确认授权。'
          : '没有 Profile。建立后返回这里确认。';
        updatePolicy();
      } catch (error) {
        if (d.isConnected) status.textContent = error.message;
      } finally {
        if (d.isConnected) spaces.disabled = false;
      }
    };
    choice.onchange = updatePolicy;
    create.onclick = () => {
      closeModal(d);
    };
    const decide = async (yes) => {
      const profile = profiles.find((p) => p.id === choice.value);
      if (yes && (!selected || !profile)) throw new Error('先选择工作空间，再选择 Profile。');
      try {
        spaces.disabled = choice.disabled = allow.disabled = true;
        const response = await post('/api/oauth/requests/' + enc(id) + '/decide', {
          allow: yes,
          selection_version: version,
          ...(yes
            ? {
                space_id: selected,
                profile_id: profile.id,
                profile_version: profile.version,
                consent_mode: $('[name="consent_mode"]', d).value,
                confirm_live_profile: $('[name="confirm_live_profile"]', d).checked,
                confirm_external_mcp: $('[name="confirm_external_mcp"]', d).checked,
              }
            : {}),
        });
        location.assign(response.redirect);
      } catch (error) {
        status.textContent = error.message;
        // Stale Profile/selection must be re-read, not silently resubmitted.
        if (
          [
            'VERSION_CHANGED',
            'PROFILE_CHANGED',
            'CONSENT_SELECTION_CHANGED',
            'NETWORK_UNCERTAIN',
          ].includes(error.code)
        ) {
          consentLocked = true;
          status.innerHTML =
            esc(error.message) + ' <button class="btn" data-reload-consent>重新读取并确认</button>';
          $('[data-reload-consent]', d).onclick = () => {
            closeModal(d);
            void consent(id);
          };
        } else {
          spaces.disabled = false;
          choice.disabled = !profiles.length;
          updatePolicy();
        }
      }
    };
    $('[data-consent-deny]', d).onclick = () =>
      busy($('[data-consent-deny]', d), () => decide(false));
    allow.onclick = () =>
      busy(allow, () => decide(true)).finally(() => {
        if (d.isConnected) updatePolicy();
      });
    if (request.selected_space_id) {
      spaces.value = request.selected_space_id;
      await spaces.onchange();
    }
  }

  async function vpsInventoryEditor(old = null) {
    form(
      old ? '编辑 VPS 资源' : '新增 VPS 资源',
      field('name', '名称', old?.name, 'required maxlength="100"') +
        field('host', '地址（仅显示资料）', old?.host, 'maxlength="253"') +
        field('provider', '提供商', old?.provider, 'maxlength="120"') +
        field('region', '区域', old?.region, 'maxlength="120"') +
        field('system', '系统', old?.system, 'maxlength="160"') +
        textArea('notes', '说明', old?.notes || '', 'maxlength="2000"') +
        check('enabled', '启用', old ? !!old.enabled : true) +
        notice(
          'SSH 凭据保留在 Runner。将整台 VPS 分配到工作空间后，选择 Runner 与 SSH connection reference。',
        ),
      (f, key) =>
        save(
          '/api/vps' + (old ? '/' + enc(old.id) : ''),
          change(
            old,
            Object.fromEntries(
              ['name', 'host', 'provider', 'region', 'system', 'notes']
                .map((k) => [k, f.elements[k].value])
                .concat([['enabled', f.elements.enabled.checked]]),
            ),
            key,
          ),
          !!old,
        ),
    );
  }
  async function vpsEditor(space, old = null) {
    const [inventory, devices, projects] = await Promise.all([
      api('/api/vps'),
      api(root(space.id) + '/devices'),
      api(root(space.id) + '/projects'),
    ]);
    form(
      '工作空间 VPS',
      field('name', '显示名称', old?.name, 'required maxlength="100"') +
        select(
          'resource_id',
          'VPS 资源',
          options(inventory.vps),
          old?.resource_id,
          old ? 'disabled' : 'required',
        ) +
        select(
          'runner_device_id',
          '发起 SSH 的工作空间 Runner',
          options(devices.devices),
          old?.runner_device_id,
          'required',
        ) +
        field(
          'connection_ref',
          'Runner 本机 SSH target reference',
          old?.connection_ref,
          'required pattern="[A-Za-z0-9_-]{1,100}"',
        ) +
        select(
          'display_mode',
          'VPS 显示位置',
          [
            ['top_level', '1 · 仅顶层 VPS'],
            ['project_only', '2 · 仅 Project scope 的 VPS 类别'],
            ['both', '1 & 2 · 同时显示'],
          ],
          old?.display_mode || 'top_level',
        ) +
        '<div data-vps-projects>' +
        multi(
          'projects',
          '在哪些 Project 的 VPS 类别显示',
          options(projects.projects, 'alias'),
          old?.projects?.map((x) => (typeof x === 'string' ? x : x.id)) || [],
        ) +
        '</div>' +
        check('enabled', '启用', old ? !!old.enabled : true) +
        notice(
          '这是同一台完整 VPS 的显示分类，不是 Project 目录沙箱。不需要本机 Project folder 才能 SSH。',
        ),
      (f, key) =>
        save(
          root(space.id) + '/vps' + (old ? '/' + enc(old.id) : ''),
          change(
            old,
            {
              name: f.elements.name.value,
              resource_id: f.elements.resource_id.value,
              runner_device_id: f.elements.runner_device_id.value,
              connection_ref: f.elements.connection_ref.value,
              display_mode: f.elements.display_mode.value,
              projects: f.elements.display_mode.value === 'top_level' ? [] : values(f, 'projects'),
              enabled: f.elements.enabled.checked,
            },
            key,
          ),
          !!old,
        ),
      (f, d) => {
        const update = () => {
          $('[data-vps-projects]', d).hidden = f.elements.display_mode.value === 'top_level';
        };
        f.elements.display_mode.onchange = update;
        update();
      },
    );
  }
  async function commandVPS(vps, projectId = '') {
    form(
      'SSH 命令 · ' + vps.name,
      textArea('command', '远端 Shell 命令', '', 'required maxlength="65536"') +
        field('cwd', '远端工作目录', '.', 'required') +
        notice('命令在完整 VPS 上执行，使用远端 SSH 账号权限；不会套用本机 Project 路径。'),
      async (f, key) =>
        settled(
          tool('exec', {
            target: vps.target || 'vps:' + vps.id,
            command: f.elements.command.value,
            cwd: f.elements.cwd.value,
            ...(projectId ? { project: projectId } : {}),
            idempotency_key: key,
          }),
        ),
      null,
      (result) => modal('SSH 结果', pretty(result)),
    );
  }

  async function inventoryHTML(kind) {
    const data = await api(kind === 'storage' ? '/api/storage-pools' : '/api/vps'),
      rows = data[kind === 'storage' ? 'pools' : 'vps'];
    snapshot = { inventory: rows, kind };
    return `<div id="workspace-inventory">${panel(
      kind === 'storage' ? 'Shared Storage · 共享存储' : 'VPS 资源库',
      rows
        .map((x) =>
          row(
            x.name,
            kind === 'storage' ? `<code>${esc(x.abstract_base)}</code>` : esc(x.host),
            button('inventory-edit', '编辑', x.id) +
              (kind === 'storage' ? button('mappings', '设备路径映射', x.id) : '') +
              button('inventory-remove', '移除', x.id),
          ),
        )
        .join('') || empty('尚未登记资源。'),
      button('inventory-new', '新增资源'),
    )}
      ${notice('这里只登记资源。使用权限、工作空间映射、默认执行设备与 VPS 显示位置都在具体工作空间配置。')}</div>`;
  }
  async function remove(url, old, message = '移除这个工作空间绑定？不会删除实物文件或远端资源。') {
    if (!confirm(message)) return;
    await api(url + '?expected_version=' + old.version, { method: 'DELETE' });
    await renderPage(false);
  }
  function bind() {
    const captured = snapshot;
    if (!captured) return;
    const space = captured.space,
      rows = captured.data?.[captured.address?.tab] || [];
    const find = (id) =>
      rows.find((x) => x.id === id || (x.user_id === id && x.source === 'manual'));
    $$('[data-ws-action]', $('#page')).forEach(
      (b) =>
        (b.onclick = () =>
          busy(b, async () => {
            const id = b.dataset.id,
              old = find(id);
            switch (b.dataset.wsAction) {
              case 'new-space':
                return newSpace();
              case 'settings':
                return settings(space);
              case 'new-devices':
                return bindDevice(space);
              case 'device-detail':
                return deviceDetail(id);
              case 'new-folders':
                return folderEditor(space);
              case 'folder':
                return folderEditor(space, old);
              case 'browse':
                return browse(space, old);
              case 'new-projects':
                return projectEditor(null, space.id);
              case 'project':
                return projectEditor(id);
              case 'new-storage':
                return storageEditor(space);
              case 'storage':
                return storageEditor(space, old);
              case 'shell':
                return shellEditor(space.id);
              case 'new-profiles':
                return profileEditor(space);
              case 'profile':
                return profileEditor(space, old);
              case 'invite':
                return invitationEditor(space);
              case 'revoke-invite':
                if (confirm('撤销这个尚未使用的工作空间邀请？')) {
                  await api(root(space.id) + '/invites/' + enc(id), { method: 'DELETE' });
                  await renderPage(false);
                }
                return;
              case 'new-members':
                return memberEditor(space);
              case 'member':
                return memberEditor(space, old);
              case 'suspend-member':
                await save(
                  root(space.id) + '/members/' + enc(id) + '/suspension',
                  { blocked: b.dataset.blocked !== '1' },
                  true,
                );
                return renderPage(false);
              case 'new-connections':
                return newConnection(space.id);
              case 'plugin-consent':
                return CodePierExternalPlugins.consent(id, space.id);
              case 'revoke':
                if (confirm('撤销这条 MCP 连接？已经执行的外部副作用不会回滚。')) {
                  await post(root(space.id) + '/connections/' + enc(id) + '/revoke');
                  await renderPage(false);
                }
                return;
              case 'return-consent':
                return consent(id);
              case 'new-vps':
                return vpsEditor(space);
              case 'vps':
                return vpsEditor(space, old);
              case 'vps-command':
                return commandVPS(old);
              case 'remove-folder':
                return remove(root(space.id) + '/folders/' + enc(id), old);
              case 'remove-storage':
                return remove(root(space.id) + '/storage/' + enc(id), old);
              case 'remove-vps':
                return remove(root(space.id) + '/vps/' + enc(id), old);
              case 'inventory-new':
                return captured.kind === 'storage' ? poolEditor() : vpsInventoryEditor();
              case 'inventory-edit':
                return captured.kind === 'storage'
                  ? poolEditor(captured.inventory.find((x) => x.id === id))
                  : vpsInventoryEditor(captured.inventory.find((x) => x.id === id));
              case 'mappings':
                return mappings(captured.inventory.find((x) => x.id === id));
              case 'inventory-remove':
                return remove(
                  (captured.kind === 'storage' ? '/api/storage-pools/' : '/api/vps/') + enc(id),
                  captured.inventory.find((x) => x.id === id),
                  '移除资源登记？不会删除实物数据或远端服务器。',
                );
            }
          })),
    );
    $$('[data-project-detail]', $('#page')).forEach(
      (b) => (b.onclick = () => busy(b, () => projectDetail(b.dataset.projectDetail))),
    );
    if (captured.address?.tab === 'mcp') {
      CodePierGateway.bind();
      CodePierExternalPlugins.bind?.();
    }
  }
  return {
    html,
    bind,
    route,
    newSpace,
    projectEditor,
    projectDetail,
    projectCollection,
    newConnection,
    consent,
    inventoryHTML,
    form,
    profileEditor,
  };
})();
