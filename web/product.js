'use strict';
// Resource setup, credential consent and correlation data share navigation, not authority.
window.CodePierProduct = (() => {
  const activeTab = () =>
    S.page === 'resources'
      ? S.resourceTab || 'projects'
      : S.page === 'access'
        ? S.accessTab || 'roles'
        : S.conversationTab || 'index';
  function intent() {
    const login = S.session,
      space = S.space_id,
      page = S.page,
      tab = activeTab(),
      modalIntent = S.modalIntent || 0;
    const ticket = (S.productIntent = (S.productIntent || 0) + 1);
    return () =>
      login === S.session &&
      space === S.space_id &&
      page === S.page &&
      tab === activeTab() &&
      modalIntent === (S.modalIntent || 0) &&
      ticket === S.productIntent;
  }
  const tabs = (area, selected, items) =>
    `<nav class="product-tabs" aria-label="${area}">${items.map(([id, label]) => `<button class="btn ${id === selected ? 'primary' : 'ghost'}" data-product-tab="${id}" data-product-area="${area}" aria-pressed="${id === selected}">${label}</button>`).join('')}</nav>`;
  const section = (title, body) =>
    `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2></div><div class="panel-body">${body}</div></section>`;
  const detailButton = (type, id) =>
    `<button class="btn small" data-resource-detail="${esc(id)}" data-resource-type="${type}">资源详情</button>`;
  const withoutHeading = (html) => {
    const t = document.createElement('template');
    t.innerHTML = html;
    for (const header of t.content.querySelectorAll('.page-head')) {
      const actions = header.querySelector('.actions');
      header.replaceWith(...(actions ? [actions] : []));
    }
    return t.innerHTML;
  };
  async function resources(seq) {
    await loadBasics();
    const tab = S.resourceTab || 'projects';
    let body;
    if (tab === 'devices') body = devicesHTML();
    else if (tab === 'vps') body = await vpsHTML(seq);
    else if (tab === 'mcp') {
      body = await CodePierGateway.html();
      const t = document.createElement('template');
      t.innerHTML = body;
      // Delegation is managed with the connection in Access.
      for (const p of t.content.querySelectorAll('.panel'))
        if (p.querySelector('h2')?.textContent === '我的连接委派') p.remove();
      body = t.innerHTML;
    } else body = projectsHTML();
    return `<div id="resources-page">${heading('资源', 'RESOURCES', '先建立资源，再配置角色规则，最后连接客户端。')}${tabs(
      'resources',
      tab,
      [
        ['projects', 'Projects · 项目'],
        ['devices', 'Devices / Agents · 执行节点'],
        ['mcp', 'MCP Services · 服务'],
        ['vps', 'VPS · SSH 连接'],
      ],
    )}<p class="form-note">资源关联用于说明用途；每项资源的使用权限仍需明确授权。</p><div id="resource-content">${withoutHeading(body)}</div></div>`;
  }
  function permissionRows(c) {
    const p = c.permissions;
    return `<div class="connection-permissions">${p.projects.map((r) => `<p><strong>Project ${esc(r.name)}</strong> → ${esc(r.actions.join(' / ') || '无可用操作')}${r.actions.join() !== r.policy_actions.join() ? `<small> · 角色规则 ${esc(r.policy_actions.join(' / '))}；项目配置进一步限制</small>` : ''}</p>`).join('')}${p.vps.map((r) => `<p><strong>VPS ${esc(r.name)}</strong> → ${esc(r.actions.join(' / '))}${r.policy_actions.includes('execute') && !r.actions.includes('execute') ? '<small> · 执行路线或项目执行权未满足</small>' : ''}<small> · ${esc(r.execution_route.alias || '未配置可用执行路线')} · SSH 账号权限</small></p>`).join('')}${p.mcp.map((r) => `<p><strong>MCP ${esc(r.name)}</strong> → ${esc(r.tools.join(' / '))}<small> · 帐户 ${esc(r.account_id)}</small></p>`).join('')}${!p.projects.length && !p.vps.length && !p.mcp.length ? '<p class="muted">当前没有有效资源权限。</p>' : ''}</div>`;
  }
  async function access() {
    await loadBasics();
    const tab = S.accessTab || 'roles';
    let body;
    if (tab === 'roles') {
      const [vps, gw] = await Promise.all([vpsInventory(), api('/api/mcp-gateway')]);
      S.vps = vps;
      S.roleBindings = gw.bindings;
      S.roleAccounts = gw.accounts;
      body = await CodePierRoles.html();
    } else if (tab === 'members') {
      body = await CodePierIdentity.html('members');
    } else if (tab === 'advanced') {
      S.settings = await api('/api/settings');
      S.grants = (await api('/api/grants')).grants;
      body = `<p class="form-note">稳定身份、原固定 grant 上限及旧接入配置保留在这里。身份相同不共享私有历史。</p>${await CodePierProfiles.html()}<details><summary>原固定授权与技术接入</summary><button class="btn" data-legacy-grant>创建原固定范围凭据</button>${connectHTML()}</details>`;
    } else {
      const [settings, records, gateway] = await Promise.all([
        api('/api/settings'),
        api('/api/client-connections'),
        api('/api/mcp-gateway'),
      ]);
      S.settings = settings;
      const names = {
        active: '有效',
        disabled: '已停用',
        revoked: '已撤销',
        expired: '已过期',
        pending: '待连接',
      };
      body =
        section(
          '连接客户端',
          `<p>ChatGPT Web 使用 OAuth 连接角色地址；支持 Bearer 的客户端可在这里创建限定连接。</p><div class="endpoint"><code>${esc(settings.role_mcp_url)}</code><button class="btn small" data-action="copy-role-endpoint">复制角色 OAuth 地址</button></div><p class="form-note">OAuth 在同一确认窗口选择角色与稳定身份，须明确同意动态政策。旧固定授权继续保留首次同意上限。</p><button class="btn primary" id="connection-create">创建客户端连接</button>`,
        ) +
        section(
          '我的 Client Connections',
          records.connections
            .map(
              (c) =>
                `<article class="grant-row" data-connection-id="${esc(c.id)}"><div class="grant-info"><h3>${esc(c.name)} <span class="badge neutral">${esc(names[c.status])}</span></h3><p>${esc(c.client)} · 角色 ${esc(c.role?.label || '原固定授权')} · ${c.follows_role_changes ? '已明确同意跟随未来角色变化' : '固定同意上限，角色新增能力不会扩展本连接'}</p><p>${c.expires ? '到期 ' + esc(timeText(c.expires)) : '等待完成连接'}${c.unavailable_reason ? ' · ' + esc(c.unavailable_reason) : ''}</p>${permissionRows(c)}<p class="form-note">资源操作仍需本机授权与在线状态；MCP 工具限制不代替后端账号自身的数据隔离。</p><details><summary>高级身份与授权信息</summary><p>连接 ${esc(c.id)}</p><p>稳定 Profile ${esc(c.profile?.id || '传统账号身份')}</p><p>授权模式 ${esc(c.authorization_mode)} · 角色版本 ${esc(c.role?.version || '—')}</p></details></div><div class="actions">${!['revoked', 'expired'].includes(c.status) ? `<button class="btn danger small" data-action="revoke-grant" data-id="${esc(c.id)}">撤销连接</button>` : ''}${gateway.grants?.some((g) => g.id === c.id) ? `<button class="btn small" data-connection-consent="${esc(c.id)}">外部 MCP 同意</button>` : ''}${c.profile ? `<button class="btn ghost small" data-connection-profile="${esc(c.profile.id)}">高级身份设置</button>` : ''}</div></article>`,
            )
            .join('') || empty('先配置角色，然后在这里连接客户端。'),
        );
    }
    return `<div id="access-page">${heading('访问', 'ACCESS', '角色定义资源规则；客户端连接保存明确同意和独立历史。')}${tabs(
      'access',
      tab,
      [
        ['roles', 'Roles · 角色'],
        ['connections', 'Client Connections · 客户端连接'],
        ...(CodePierIdentity.admin() ? [['members', 'Space Members · 成员与分配']] : []),
        ['advanced', '高级身份与兼容接入'],
      ],
    )}<div id="access-content">${withoutHeading(body)}</div></div>`;
  }
  async function newConnection() {
    const current = intent();
    const login = S.session,
      page = S.page;
    const [result, vps, gateway] = await Promise.all([
      api('/api/access-roles'),
      vpsInventory(),
      api('/api/mcp-gateway'),
    ]);
    if (!current()) return;
    const roles = result.roles.filter((r) => r.enabled);
    S.vps = vps;
    S.roleBindings = gateway.bindings;
    S.roleAccounts = gateway.accounts;
    if (!roles.length) return toast('先创建或获分配一个启用的角色', true);
    const key = crypto.randomUUID();
    const d = modal(
      '连接客户端',
      `<form id="connection-form"><label class="field">客户端 / 连接名称<input name="name" required maxlength="80" placeholder="例如 Worker client"></label><label class="field">角色<select name="role_id">${roles.map((r) => `<option value="${esc(r.id)}">${esc(r.label)}</option>`).join('')}</select></label><div id="connection-policy"></div><label class="field">授权方式<select name="authorization_mode"><option value="fixed">固定：仅同意当前资源与操作</option><option value="role">动态：明确跟随未来角色变化</option></select></label><label class="check" id="connection-dynamic" hidden><input name="confirm_dynamic_role" type="checkbox">我明确同意此角色当前及未来的资源、工具与能力变化</label><label class="check"><input name="confirm_external_mcp" type="checkbox">允许使用此角色中已批准的 MCP 帐户与工具（动态模式含今后明确加入角色的工具）</label><label class="field">有效天数<input name="days" type="number" min="1" max="365" value="30" required></label><p class="form-note">保存后自动建立稳定身份和独立 grant。凭据只显示一次；后续可在此撤销或通过高级设置复用身份。</p><p id="connection-status" role="status"></p></form>`,
      `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" form="connection-form" type="submit">建立连接</button>`,
    );
    const f = $('#connection-form', d);
    const update = () => {
      $('#connection-policy', d).innerHTML = CodePierRoles.summary(
        roles.find((r) => r.id === f.elements.role_id.value),
      );
      $('#connection-dynamic', d).hidden = f.elements.authorization_mode.value !== 'role';
      if (f.elements.authorization_mode.value === 'fixed')
        $('#connection-policy', d).insertAdjacentHTML(
          'beforeend',
          '<p class="form-note">固定连接冻结创建时的资源与使用操作；“现有及未来项目”规则也只包括当前项目。设备查看与项目创建等管理委派须另行明确同意动态角色授权。</p>',
        );
    };
    f.elements.role_id.onchange = update;
    f.elements.authorization_mode.onchange = update;
    update();
    f.onsubmit = (e) => {
      e.preventDefault();
      busy($('button[type="submit"]', d), async () => {
        if (!f.reportValidity()) return;
        const role = roles.find((r) => r.id === f.elements.role_id.value);
        if (
          f.elements.authorization_mode.value === 'role' &&
          !f.elements.confirm_dynamic_role.checked
        ) {
          $('#connection-status', d).textContent = '请明确勾选未来角色变化的同意。';
          return;
        }
        const body = {
          name: f.elements.name.value,
          role_id: role.id,
          expected_role_version: role.version,
          authorization_mode: f.elements.authorization_mode.value,
          confirm_dynamic_role: f.elements.confirm_dynamic_role.checked,
          confirm_external_mcp: f.elements.confirm_external_mcp.checked,
          days: Number(f.elements.days.value),
          idempotency_key: key,
        };
        try {
          const result = await post('/api/client-connections', body);
          if (login !== S.session || !d.isConnected) return;
          closeModal(d);
          if (!result.token) {
            toast(result.note);
            await renderPage(false);
            return;
          }
          const saved = modal(
            '保存客户端凭据',
            `<p>连接已建立。凭据仅在当前窗口显示一次。</p><div class="code-box secret">${esc(result.token)}</div><p>到期 ${esc(timeText(result.expires))}。在客户端配置 Bearer；可在 Client Connections 查看实际权限或撤销。</p>`,
            `<button class="btn ghost" data-action="close-modal">完成</button><button class="btn primary" id="connection-copy">复制凭据</button>`,
          );
          $('#connection-copy', saved).onclick = () => copy(result.token);
          await renderPage(false);
        } catch (err) {
          if (d.isConnected)
            $('#connection-status', d).textContent =
              err.code === 'NETWORK_UNCERTAIN'
                ? '创建回执未确认；请关闭并刷新列表核对。原请求键可安全重试，但凭据不会再次显示。'
                : err.message;
        }
      });
    };
  }
  async function resourceDetail(type, id, workspace_id = '') {
    const current = intent();
    const login = S.session,
      seq = S.renderSeq;
    const r = await api(`/api/resources/${type}/${encodeURIComponent(id)}`);
    if (!current()) return;
    const c = r.configuration;
    const facts =
      type === 'project'
        ? [
            ['目录', c.root],
            ['Agent', c.device_name],
            ['连接', c.online ? '在线' : '离线'],
            ['映射模式', c.mode],
            ['任务执行', c.allow_tasks ? '允许（仍需本机授权）' : '未允许'],
          ]
        : type === 'vps'
          ? [
              ['SSH 账号', c.username],
              ['地址', vpsEndpoint(c)],
              ['配置', c.enabled ? '启用，远端在线状态未知' : '停用'],
              ['执行路线', c.execution_route.alias || '尚未配置'],
              ['Agent', c.execution_route.device_name || '尚未配置'],
              ['路线可用', c.execution_route.available ? '已配置；连接时核验' : '未满足执行条件'],
              ['权限范围', 'SSH 账号权限；不是项目目录沙箱'],
            ]
          : [
              ['服务', c.name],
              ['Endpoint', c.endpoint],
              ['帐户', c.account],
              ['状态', c.enabled ? '启用；调用时核验连接' : '暂停'],
              ['批准工具', c.approved_tools.join(' / ')],
            ];
    const controls =
      type === 'project'
        ? `<button class="btn" data-action="edit-project" data-id="${esc(id)}">配置项目</button><button class="btn" data-vps-action="project" data-project="${esc(id)}">关联 VPS</button><button class="btn" data-project-mcp="${esc(id)}">关联 MCP Service</button>`
        : type === 'vps'
          ? `<button class="btn" data-vps-action="edit" data-id="${esc(id)}">配置连接与路线</button>`
          : '<button class="btn" data-product-tab="mcp" data-product-area="resources">服务、账号与工具配置</button>';
    const d = modal(
      '资源 · ' + r.resource.name,
      `${tabs('detail', 'configuration', [
        ['configuration', '配置与可用性'],
        ['access', '访问规则'],
        ['conversations', '相关对话'],
        ['records', '操作与审计'],
      ])}<div data-detail-pane="configuration"><dl class="kv">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join('')}</dl><div class="actions">${controls}</div><h3>资源关联</h3>${r.associations.map((a) => `<p>${esc(a.type)} · ${esc(a.name)}</p>`).join('') || '<p>暂无关联</p>'}<p class="form-note">关联不授予访问；角色规则与连接同意独立检查。</p></div><div data-detail-pane="access" hidden>${r.access.map((a) => `<p><strong>${esc(a.role)}</strong> → ${esc((a.actions || a.tools).join(' / '))} · ${a.enabled ? '启用' : '暂停'}</p>`).join('') || '<p>暂无角色规则。</p>'}<button class="btn" data-product-tab="roles" data-product-area="access">配置角色规则</button></div><div data-detail-pane="conversations" hidden>${conversationRows(r.conversations)}${r.next_conversation_offset !== null ? '<p>更多对话可在 Conversations 按资源筛选。</p>' : ''}</div><div data-detail-pane="records" hidden>${operationRows(r.operations)}<h3>审计记录</h3>${r.audit.map((a) => `<p>${esc(timeText(a.at))} · ${esc(a.action)} · ${esc(a.status)}</p>`).join('') || '<p>暂无记录。</p>'}<p class="form-note">最多显示 50 条当前身份获准读取的记录。</p></div>`,
      type === 'project'
        ? `<button class="btn primary" data-project-tools="${esc(id)}" data-workspace="${esc(workspace_id)}">Development Tools · 开发工具</button><button class="btn" data-project-artifacts="${esc(id)}">Artifacts / Downloads · 文件产物</button>`
        : '',
      true,
    );
    for (const b of $$('[data-product-area="detail"]', d))
      b.onclick = () => {
        $$('[data-detail-pane]', d).forEach(
          (p) => (p.hidden = p.dataset.detailPane !== b.dataset.productTab),
        );
        $$('[data-product-area="detail"]', d).forEach((p) =>
          p.setAttribute('aria-pressed', String(p === b)),
        );
      };
  }
  const operationRows = (rows) =>
    rows
      .map(
        (o) =>
          `<div class="grant-row"><div><strong>${esc(o.tool)}</strong> · ${esc(o.state)}<p class="mono">${esc(o.id)}</p></div>${o.type === 'native' ? `<button class="btn small" data-action="operation-detail" data-id="${esc(o.id)}">查看操作</button>` : '<span>通过原连接查询 gateway_call_get 回执</span>'}</div>`,
      )
      .join('') || '<p>暂无获准读取的操作。</p>';
  const conversationRows = (rows) =>
    rows
      .map(
        (c) =>
          `<article class="grant-row"><div><strong>${esc(c.label || c.platform + ' · ' + c.conversation_identifier)}</strong><p>${esc(c.resources.map((r) => r.name).join(' / ') || '暂无资源关联')}</p><small>${esc(timeText(c.last_activity))}</small></div><button class="btn small" data-conversation-detail="${esc(c.id)}">查看对话关联</button>${c.original_url ? `<a class="btn small" href="${esc(c.original_url)}" target="_blank" rel="noopener noreferrer">返回原对话</a>` : ''}</article>`,
      )
      .join('') || '<p>暂无对话关联。</p>';
  async function conversations() {
    const tab = S.conversationTab || 'index';
    let body;
    if (tab === 'archive') {
      const result = await tool('workflows_list', {
        limit: 20,
        project: '',
        state: '',
        cursor: S.archiveCursor || '',
      });
      S.archiveNext = result.next_cursor;
      body = `<p class="form-note">旧工作流与事件按原权限保留，只供历史读取。不会更新进度；取消操作仍在操作回执中处理。</p>${result.workflows.map((w) => `<article class="grant-row"><div><strong>${esc(w.title)}</strong><p>${esc(w.project_alias)} · ${esc(timeText(w.updated))}</p></div><button class="btn small" data-archive-detail="${esc(w.workflow_id)}">查看历史记录</button></article>`).join('') || empty('没有旧工作流记录。')}<button class="btn ghost" id="archive-next" ${result.next_cursor ? '' : 'disabled'}>下一页</button>`;
    } else {
      await loadBasics();
      const [vps, gw] = await Promise.all([vpsInventory(), api('/api/mcp-gateway')]);
      const options = [
        ...S.projects.map((p) => ({ type: 'project', id: p.id, name: p.alias })),
        ...vps.map((v) => ({ type: 'vps', id: v.id, name: v.name })),
        ...gw.bindings.map((b) => ({ type: 'mcp', id: b.id, name: b.alias })),
      ];
      const filter = S.conversationFilter || {};
      const q = new URLSearchParams({ offset: String(S.conversationOffset || 0), ...filter });
      const result = await api('/api/conversations?' + q);
      S.conversationNext = result.next_offset;
      body = `<label class="field">按资源筛选<select id="conversation-resource"><option value="">全部资源</option>${options.map((r) => `<option value="${r.type}|${esc(r.id)}" ${filter.resource_type === r.type && filter.resource_id === r.id ? 'selected' : ''}>${esc(r.type)} · ${esc(r.name)}</option>`).join('')}</select></label><div class="actions"><button class="btn primary" id="conversation-create">关联已有对话</button>${filter.resource_id ? '<button class="btn ghost" id="conversation-clear">清除资源筛选</button>' : ''}</div><p class="form-note">宿主提供 session 元数据时自动关联。匿名 ChatGPT session ID 用于关联调用；原对话网址须另外提供。这里不保存聊天正文、回答或进度；继续在原客户端阅读回答和发出指令。</p>${conversationRows(result.conversations)}<div class="pagination"><button class="btn ghost" id="conversation-prev" ${S.conversationOffset || 0 ? '' : 'disabled'}>上一页</button><button class="btn ghost" id="conversation-next" ${result.next_offset !== null ? '' : 'disabled'}>下一页</button></div>`;
    }
    return `<div id="conversations-page">${heading('对话关联', 'CONVERSATIONS', '一条对话可使用多个资源；同一资源可出现在不同对话中。')}${tabs(
      'conversations',
      tab,
      [
        ['index', '对话索引'],
        ['archive', '旧工作流历史'],
      ],
    )}${section(tab === 'archive' ? '历史归档' : '对话与资源', body)}</div>`;
  }
  async function conversationDetail(id) {
    const current = intent();
    const login = S.session,
      seq = S.renderSeq;
    const { conversation: c } = await api('/api/conversations/' + encodeURIComponent(id));
    if (!current()) return;
    const d = modal(
      c.label || '对话关联',
      `<dl class="kv"><dt>平台</dt><dd>${esc(c.platform)}</dd><dt>客户端对话标识</dt><dd class="product-wrap">${esc(c.conversation_identifier)}</dd><dt>来源连接</dt><dd>${esc(c.grant_id || '当前登录账号手动关联')}</dd><dt>首次活动</dt><dd>${esc(timeText(c.first_activity))}</dd><dt>最近活动</dt><dd>${esc(timeText(c.last_activity))}</dd></dl>${c.original_url ? `<a class="btn" href="${esc(c.original_url)}" target="_blank" rel="noopener noreferrer">返回原对话</a>` : '<p>没有提供原对话网址；无法从匿名标识生成网址或取得聊天正文。</p>'}<h3>关联资源</h3>${c.resources.map((r) => `<div class="grant-row"><span>${esc(r.type)} · ${esc(r.name)}</span>${detailButton(r.type, r.id)}</div>`).join('') || '<p>暂无获准查看的资源。</p>'}<h3>已有操作</h3>${operationRows(c.operations)}<p class="form-note">最多显示 ${c.operation_limit} 条操作；原执行状态、回执与取消功能独立保留。</p>`,
      '',
      true,
    );
    d.querySelector('.modal-body').insertAdjacentHTML(
      'beforeend',
      '<button class="btn" id="conversation-edit">编辑标签、网址与资源关联</button>',
    );
    $('#conversation-edit', d).onclick = () => conversationEdit(c);
  }
  async function conversationEdit(saved = null) {
    const current = intent();
    const login = S.session,
      seq = S.renderSeq;
    const [vps, gw] = await Promise.all([vpsInventory(), api('/api/mcp-gateway')]);
    await loadBasics();
    if (!current()) return;
    const choices = [
      ...S.projects.map((p) => ({ type: 'project', id: p.id, name: p.alias })),
      ...vps.map((v) => ({ type: 'vps', id: v.id, name: v.name })),
      ...gw.bindings.map((b) => ({ type: 'mcp', id: b.id, name: b.alias })),
    ];
    const d = modal(
      saved ? '编辑对话关联' : '关联已有对话',
      `<form id="conversation-form"><label class="field">平台 / 客户端<input name="platform" required maxlength="80" value="${esc(saved?.platform || '')}" placeholder="chatgpt / claude / 自有客户端" ${saved ? 'readonly' : ''}></label><label class="field">客户端提供的对话标识<input name="conversation_identifier" required maxlength="512" value="${esc(saved?.conversation_identifier || '')}" ${saved ? 'readonly' : ''}></label><label class="field">显示标签（可选）<input name="label" maxlength="160" value="${esc(saved?.label || '')}"></label><label class="field">真实原对话网址（可选）<input name="original_url" type="url" maxlength="2048" value="${esc(saved?.original_url || '')}" placeholder="https://…"></label><p class="form-note">只接受原客户端实际提供的 HTTPS 网址；不要用匿名 session ID 拼接网址。不存在可用标识时，可继续正常工具调用。</p><fieldset><legend>关联资源</legend>${choices.map((r) => `<label class="check"><input name="resource" type="checkbox" value="${r.type}|${esc(r.id)}" ${saved?.resources.some((s) => s.type === r.type && s.id === r.id) ? 'checked' : ''}>${esc(r.type)} · ${esc(r.name)}</label>`).join('')}</fieldset><p class="form-note">增加关联不会授予资源访问；已有关联保留，操作按原连接隔离。</p><p id="conversation-status" role="status"></p></form>`,
      `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" form="conversation-form" type="submit">保存关联</button>`,
    );
    const f = $('#conversation-form', d);
    f.onsubmit = (e) => {
      e.preventDefault();
      busy($('button[type="submit"]', d), async () => {
        if (!f.reportValidity()) return;
        const body = Object.fromEntries(new FormData(f));
        delete body.resource;
        body.resources = $$('[name="resource"]:checked', f).map((x) => {
          const [type, id] = x.value.split('|');
          return { type, id };
        });
        try {
          await api('/api/conversations' + (saved ? '/' + encodeURIComponent(saved.id) : ''), {
            method: saved ? 'PUT' : 'POST',
            body: JSON.stringify(body),
          });
          if (login === S.session && d.isConnected) {
            closeModal(d);
            await renderPage(false);
          }
        } catch (err) {
          if (d.isConnected) $('#conversation-status', d).textContent = err.message;
        }
      });
    };
  }
  async function projectMCP(id) {
    const current = intent();
    const [r, gw] = await Promise.all([
      api('/api/resources/project/' + id),
      api('/api/mcp-gateway'),
    ]);
    if (!current()) return;
    const selected = r.associations.filter((a) => a.type === 'mcp').map((a) => a.id);
    const d = modal(
      '关联 MCP Service',
      `<form id="project-mcp-form">${gw.bindings.map((b) => `<label class="check"><input name="binding" type="checkbox" value="${esc(b.id)}" ${selected.includes(b.id) ? 'checked' : ''}>${esc(b.alias)} · ${esc(gw.accounts.find((a) => a.id === b.account_id)?.label || '')}</label>`).join('') || '<p>先在 Resources → MCP Services 连接帐户并批准工具。</p>'}<p>关联不会改变角色或客户端权限。</p><p id="project-mcp-status" role="status"></p></form>`,
      `<button class="btn primary" form="project-mcp-form" type="submit">保存关联</button>`,
    );
    $('#project-mcp-form', d).onsubmit = (e) => {
      e.preventDefault();
      busy($('button[type="submit"]', d), async () => {
        try {
          await api('/api/projects/' + id + '/mcp-services', {
            method: 'PUT',
            body: JSON.stringify({
              binding_ids: $$('[name="binding"]:checked', d).map((x) => x.value),
              expected_binding_ids: selected,
            }),
          });
          closeModal(d);
          await renderPage(false);
        } catch (err) {
          $('#project-mcp-status', d).textContent = err.message;
        }
      });
    };
  }
  function bind() {
    if (S.page === 'resources') {
      if (S.resourceTab === 'vps') bindVps();
      if (S.resourceTab === 'mcp') CodePierGateway.bind();
    }
    if (S.page === 'access') {
      if (S.accessTab === 'members') CodePierIdentity.bind();
      if ((S.accessTab || 'roles') === 'roles') CodePierRoles.bind();
      if (S.accessTab === 'advanced') CodePierProfiles.bind();
      if ($('#connection-create')) $('#connection-create').onclick = newConnection;
    }
    if ($('#conversation-resource'))
      $('#conversation-resource').onchange = (e) => {
        const [type, id] = e.target.value.split('|');
        S.conversationFilter = id ? { resource_type: type, resource_id: id } : null;
        S.conversationOffset = 0;
        renderPage(false);
      };
    if ($('#conversation-create')) $('#conversation-create').onclick = () => conversationEdit();
    if ($('#conversation-next'))
      $('#conversation-next').onclick = () => {
        S.conversationOffset = S.conversationNext;
        renderPage(false);
      };
    if ($('#conversation-prev'))
      $('#conversation-prev').onclick = () => {
        S.conversationOffset = Math.max(0, (S.conversationOffset || 0) - 30);
        renderPage(false);
      };
    if ($('#conversation-clear'))
      $('#conversation-clear').onclick = () => {
        S.conversationFilter = null;
        S.conversationOffset = 0;
        renderPage(false);
      };
    if ($('#archive-next'))
      $('#archive-next').onclick = () => {
        S.archiveCursor = S.archiveNext;
        renderPage(false);
      };
  }
  document.addEventListener('click', async (e) => {
    const b = e.target.closest(
      '[data-product-tab],[data-resource-detail],[data-project-tools],[data-project-artifacts],[data-conversation-detail],[data-archive-detail],[data-project-mcp],[data-connection-profile],[data-connection-consent],[data-legacy-grant]',
    );
    if (!b || b.disabled || b.dataset.productArea === 'detail') return;
    try {
      if (b.hasAttribute('data-legacy-grant')) await newLegacyGrant();
      else if (b.dataset.productArea) {
        if (!panelDialogs.requestClose(null, { navigation: true })) return;
        const area = b.dataset.productArea;
        S[
          area === 'resources' ? 'resourceTab' : area === 'access' ? 'accessTab' : 'conversationTab'
        ] = b.dataset.productTab;
        await navigate(area);
      } else if (b.dataset.projectTools) {
        if (!panelDialogs.requestClose(null, { navigation: true })) return;
        await CodePierIntegrations.open({
          project: b.dataset.projectTools,
          workspace_id: b.dataset.workspace || '',
        });
      } else if (b.dataset.projectArtifacts) {
        if (!panelDialogs.requestClose(null, { navigation: true })) return;
        await openProjectArtifacts(b.dataset.projectArtifacts);
      } else if (b.dataset.resourceDetail)
        await resourceDetail(
          b.dataset.resourceType,
          b.dataset.resourceDetail,
          b.dataset.resourceWorkspace || '',
        );
      else if (b.dataset.conversationDetail) await conversationDetail(b.dataset.conversationDetail);
      else if (b.dataset.projectMcp) await projectMCP(b.dataset.projectMcp);
      else if (b.dataset.connectionProfile)
        await CodePierProfiles.edit(b.dataset.connectionProfile);
      else if (b.dataset.connectionConsent) {
        const current = await api('/api/mcp-gateway');
        const g = current.grants.find((g) => g.id === b.dataset.connectionConsent);
        if (!g) throw new Error('连接已变化，请刷新');
        const role = await api('/api/access-roles/' + g.role_id);
        const d = modal(
          '外部 MCP 同意',
          (g.authorization_mode === 'fixed'
            ? `<p>${esc(g.fixed_tools.map((r) => r.binding_id + ' → ' + r.tools.join(' / ')).join('；') || '原始快照没有 MCP 工具')}</p>`
            : CodePierRoles.summary(role)) +
            `<p>${g.consent_version ? '撤回此连接的外部 MCP 委派；本地资源权限继续按原政策检查。' : g.authorization_mode === 'fixed' ? '只同意此连接原始快照内的 MCP 帐户、工具与定义；不会随未来角色变化扩展。' : '此动态连接将使用角色明确配置的当前及未来 MCP 帐户与批准工具。'}</p>`,
          `<button class="btn primary" id="connection-consent-save">${g.consent_version ? '撤回同意' : '明确同意'}</button>`,
        );
        $('#connection-consent-save', d).onclick = () =>
          busy($('#connection-consent-save', d), async () => {
            await api('/api/mcp-gateway/grants/' + g.id + '/consent', {
              method: g.consent_version ? 'DELETE' : 'POST',
              ...(g.consent_version
                ? {}
                : {
                    body: JSON.stringify({ confirmed: true, expected_role_version: role.version }),
                  }),
            });
            closeModal(d);
            await renderPage(false);
          });
      } else if (b.dataset.archiveDetail) {
        const w = await tool('workflows_get', { workflow_id: b.dataset.archiveDetail });
        modal(
          '历史记录 · ' + w.title,
          `<p>只读归档；不再更新步骤或进度。</p><p>${esc(w.goal)}</p><pre class="product-wrap">${esc(json({ steps: w.steps, summary: w.summary, events: w.events }))}</pre>`,
          '',
          true,
        );
      }
    } catch (err) {
      toast(err.message, true);
    }
  });
  return { resources, access, conversations, bind, newConnection, resourceDetail };
})();
