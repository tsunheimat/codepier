'use strict';
// Human identity/Space management. No ID/access/refresh Token is persisted here.
window.CodePierIdentity = (() => {
  let me = null,
    providers = [],
    users = [];
  const check = (name, label, value = false) =>
    `<label class="check"><input type="checkbox" name="${name}" ${value ? 'checked' : ''}>${esc(label)}</label>`;
  const field = (name, label, value = '', extra = '') =>
    `<div class="field"><label for="iam-${name}">${esc(label)}</label><input id="iam-${name}" name="${name}" value="${esc(value)}" ${extra}></div>`;
  const select = (name, label, options, value) =>
    `<div class="field"><label for="iam-${name}">${esc(label)}</label><select id="iam-${name}" name="${name}">${options.map(([id, text]) => `<option value="${esc(id)}" ${id === value ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select></div>`;
  function renewalNote(identity) {
    if (!identity.enabled) return '';
    const status = identity.reauthentication_required
      ? '身份提供者要求重新认证；已有 CodePier 登录和 MCP 授权未删除。'
      : identity.sync_code
        ? '身份同步状态：' + identity.sync_code + '；下次尝试：' + timeText(identity.next_attempt)
        : identity.has_refresh_token === false
          ? '身份提供者未提供续期凭据；请启用 offline_access 并重新认证一次，否则到期后无法自动续期。'
          : identity.has_refresh_token === true
            ? '已取得身份提供者续期凭据。'
            : '';
    return status ? `<p class="form-note">${esc(status)}</p>` : '';
  }
  function current() {
    const match = /^#spaces\/([A-Za-z0-9_-]+)(?:\/|$)/.exec(location.hash);
    return match ? me?.spaces.find((x) => x.id === match[1]) : null;
  }
  function admin() { return !!current()?.management?.includes('resources'); }
  function reset() { me = null; providers = []; users = []; }
  async function loginButtons() {
    const host = $('#oidc-login-buttons');
    if (!host) return;
    try {
      const out = await api('/api/auth/providers');
      if (!host.isConnected) return;
      host.replaceChildren();
      const bootstrap = $('#login-bootstrap-state');
      if (bootstrap) {
        bootstrap.innerHTML = out.bootstrap_admin_available
          ? notice(
              '已配置首次 SSO 管理员初始化。首位获准登录将获得实例管理身份，并进入自己的 Personal Space。',
            )
          : bootstrap.dataset.unconfigured === 'true'
            ? notice(
                '请在服务器运行 <code>python -m hub init</code> 初始化本地恢复账号，或由管理员配置首次 SSO 初始化。',
              )
            : '';
      }
      for (const provider of out.providers) {
        const button = document.createElement('a');
        button.className = 'btn primary';
        button.textContent = '使用 ' + provider.label + ' 登录';
        const returnTo = '/' + location.search + location.hash;
        button.href =
          '/auth/oidc/' +
          encodeURIComponent(provider.id) +
          '/start?' +
          new URLSearchParams({ return_to: returnTo });
        host.append(button);
      }
      if (out.providers.length) {
        const note = document.createElement('p');
        note.className = 'form-note';
        note.textContent = out.bootstrap_admin_available
          ? '首次管理员窗口由后台校验；后续用户只获得自己的 Personal Space 与明确分配的权限。'
          : out.local_login_available
            ? '也可使用本地账号登录；外部登录权限由后台的身份与空间分配决定。'
            : '此安装使用外部身份登录。';
        host.append(note);
      }
    } catch (error) {
      if (host.isConnected) host.textContent = '外部登录暂时不可用；可使用本地恢复账号。';
    }
  }
  async function bootstrap() {
    const identity = await api('/api/iam/me');
    if (identity.identity_access && S.page !== 'identity')
      throw Object.assign(new Error(identity.identity_access.message), identity.identity_access);
    me = identity;
    S.identity = me;
    Object.assign(S.session, { instance_admin: me.instance_admin, spaces: me.spaces });
  }
  function bindShell() {}
  async function refresh() { await bootstrap(); }
  function form(title, body, save, options = {}) {
    const login = S.session,
      space = location.hash;
    const dialog = modal(
      title,
      `<form id="iam-form">${body}<p class="form-note" id="iam-save-status" role="status"></p></form>`,
      `<button class="btn ghost" data-action="close-modal">取消</button><button type="submit" form="iam-form" class="btn primary">保存</button>`,
      !!options.large,
    );
    $('#iam-form', dialog).onsubmit = (e) => {
      e.preventDefault();
      const form = e.target;
      busy($('button[type="submit"]', dialog), async () => {
        if (!form.reportValidity()) return;
        const status = $('#iam-save-status', dialog);
        status.textContent = '正在保存…';
        try {
          const result = await save(form);
          if (S.session !== login || location.hash !== space || !dialog.isConnected) return;
          closeModal(dialog);
          if (options.done) await options.done(result);
          else {
            await refresh();
            await renderPage(false);
            toast('已保存');
          }
        } catch (error) {
          if (dialog.isConnected) status.textContent = error.message;
        }
      });
    };
    return dialog;
  }
  function freshLink(provider) {
    return busy(null, async () => {
      const out = await post('/api/iam/oidc/' + encodeURIComponent(provider) + '/link', {
        return_to: '/#identity',
      });
      location.assign(out.redirect);
    });
  }
  async function html(page) {
    const login = S.session,
      space = location.hash,
      seq = S.renderSeq;
    const currentView = () => login === S.session && space === location.hash && seq === S.renderSeq;
    const identity = await api('/api/iam/me');
    if (!currentView()) return '';
    me = identity;
    S.identity = me;
    const tab = S.identityTab || 'account';
    const tabs = () =>
      `<nav class="product-tabs" aria-label="账号与身份">${navigationTabs('identity')
        .map(
          ([id, label]) =>
            `<button class="btn ${id === tab ? 'primary' : 'ghost'}" data-identity-tab="${id}" aria-pressed="${id === tab}">${label}</button>`,
        )
        .join('')}</nav>`;
    const accountHeader = (actions = '') =>
      heading('账号与身份', 'ACCOUNT / IDENTITY', me.display_name, actions) + tabs();
    if (page === 'identity' && tab === 'account') {
      const [sessionData, providerData] = await Promise.all([
        api('/api/iam/sessions'),
        api('/api/auth/providers'),
      ]);
      if (!currentView()) return '';
      providers = providerData.providers;
      return (
        accountHeader(
          `<button class="btn primary" data-nav="spaces">管理工作空间</button><button class="btn" data-iam="accept-invite">接受邀请</button>`,
        ) +
        `<section class="panel"><div class="panel-head"><h2>工作空间</h2></div><div class="panel-body"><p>分别打开工作空间管理资源，不改变任何已建立的 MCP 连接。</p>${me.spaces.map((x) => `<div class="grant-row"><strong>${esc(x.label)}</strong><a class="btn" href="#spaces/${encodeURIComponent(x.id)}">打开工作空间</a></div>`).join('') || empty('没有可用工作空间。')}${(me.disabled_spaces || []).map((x) => `<div class="grant-row"><span>${esc(x.label)} · 已停用</span><button class="btn" data-iam-restore="${esc(x.id)}">恢复</button></div>`).join('')}</div></section>` +
        `<section class="panel"><div class="panel-head"><h2>登录身份</h2></div><div class="panel-body"><p>${me.local_login ? '本地密码登录已启用。' : '本账号仅使用外部身份登录。'}</p>${me.identities.map((i) => `<div class="grant-row"><div><strong>${esc(i.label)}</strong><p>${i.enabled ? '已关联' : '已停用 / 已解除关联'} · 权限有效至 ${esc(timeText(i.fresh_until))}</p>${renewalNote(i)}</div><button class="btn danger small" data-iam-unlink="${esc(i.id)}">解除关联</button></div>`).join('')}<div class="actions">${providers.map((p) => `<button class="btn" data-iam-link="${esc(p.id)}">关联 ${esc(p.label)}</button><a class="btn ghost" href="/auth/oidc/${encodeURIComponent(p.id)}/start?return_to=%2F%23identity">重新认证</a>`).join('')}</div><p class="form-note">关联 / 解除关联要求近期登录。不会按邮箱自动合并账号；解除关联会撤销该身份的会话和 MCP 凭据。</p></div></section>` +
        `<section class="panel"><div class="panel-head"><h2>登录会话</h2></div><div class="panel-body">${sessionData.sessions.map((s) => `<div class="grant-row"><div><strong>${s.id === sessionData.current ? '当前浏览器' : '其他浏览器'}</strong><p>${s.expires >= 253402300799 ? '有效至主动撤销（浏览器可能清除登录 Cookie）' : '到期 ' + esc(timeText(s.expires))}</p></div><button class="btn danger small" data-iam-session="${esc(s.id)}">退出此会话</button></div>`).join('')}</div></section>`
      );
    }
    if (!me.instance_admin)
      return accountHeader() + uiPermissionState('管理权限', 'Users 和 SSO 需要实例管理员权限。');
    if (tab === 'users') {
      const out = await api('/api/iam/users');
      if (!currentView()) return '';
      users = out.users;
      return (
        accountHeader() +
        `<section class="panel"><div class="panel-head"><h2>用户</h2></div><div class="panel-body">${users.map((u) => `<div class="grant-row"><div><strong>${esc(u.display_name || u.username)}</strong><p>${u.active ? '有效' : '已停用'} · ${u.instance_admin ? '实例管理员' : '普通用户'} · ${u.local_login ? '本地登录' : '外部登录'}</p><small class="mono">${esc(u.id)}</small></div><button class="btn small" data-iam-user="${esc(u.id)}">管理</button></div>`).join('')}</div></section>`
      );
    }
    const out = await api('/api/iam/oidc/providers');
    if (!currentView()) return '';
    providers = out.providers;
    return (
      accountHeader(
        `<button class="btn" data-iam="sync">校验群组权限</button><button class="btn primary" data-iam="new-provider">添加 OIDC 提供者</button>`,
      ) +
      '<p class="form-note">OIDC 负责认证；CodePier 管理工作空间成员和资源授权。这里的设置作用于整个实例。</p>' +
      `<section class="panel"><div class="panel-head"><h2>身份提供者</h2></div><div class="panel-body">${providers.map((p) => `<div class="grant-row"><div><h3>${esc(p.label)} · ${p.enabled ? '已启用' : '已停用'}</h3><p class="iam-wrap">${esc(p.issuer)}</p><small>加入策略：${esc(p.admission)} · 权限校验窗口 ${p.freshness_seconds} 秒</small></div><div class="actions"><button class="btn small" data-iam-provider="${esc(p.id)}">编辑</button><button class="btn small" data-iam-check="${esc(p.id)}">测试发现</button><button class="btn small" data-iam-mappings="${esc(p.id)}">群组映射</button></div></div>`).join('') || empty('没有 OIDC 提供者。本地恢复登录继续有效。')}</div></section>`
    );
  }

  function providerEdit(id) {
    const p = providers.find((p) => p.id === id);
    const body =
      '<div class="form-sections"><section class="form-section"><h3>连接信息</h3><p class="form-note">先填写身份提供者的基本连接信息。</p><div class="form-grid">' +
      field('label', '显示名称', p?.label || '', 'required maxlength="80"') +
      field('issuer', '精确 Issuer', p?.issuer || '', 'required type="url"') +
      field('client_id', 'Client ID', p?.client_id || '', 'required') +
      field(
        'client_secret',
        p ? 'Client secret（留空保留）' : 'Client secret',
        '',
        'type="password" autocomplete="new-password" ' + (p ? '' : 'required'),
      ) +
      '</div></section><section class="form-section"><h3>登录准入与群组</h3><div class="form-grid">' +
      select(
        'admission',
        '未知外部用户加入策略',
        [
          ['closed', '关闭；只允许已关联身份'],
          ['jit', '验证后创建个人账号和个人空间'],
        ],
        p?.admission || 'closed',
      ) +
      field('required_group', '允许登录的群组（可空）', p?.required_group || '') +
      field('group_claim', '群组 claim', p?.group_claim || 'groups', 'required') +
      field(
        'freshness_seconds',
        '权限校验最大有效期（秒）',
        p?.freshness_seconds || 900,
        'type="number" min="60" max="86400" required',
      ) +
      '</div>' +
      notice(
        '群组限制/映射要求 UserInfo 返回字段；Microsoft Entra 的 UserInfo 不支持 groups，不能靠添加 ID Token claim 修复。请先验证兼容性；不要直接移除准入限制。',
        true,
      ) +
      '</section><details class="form-section form-advanced"><summary>高级连接配置</summary><div class="form-grid">' +
      field(
        'discovery_url',
        'Discovery URL（留空使用默认）',
        p?.discovery_url || '',
        'type="url"',
      ) +
      field('scopes', 'OIDC scopes', p?.scopes || 'openid profile offline_access', 'required') +
      field(
        'endpoint_origins',
        '额外批准的端点来源（逗号分隔）',
        p?.endpoint_origins?.join(', ') || '',
      ) +
      '</div></details><section class="form-section form-review"><h3>启用与安全检查</h3>' +
      check('enabled', '启用外部登录', !!p?.enabled) +
      notice('使用 TLS、精确回调和 issuer。不会从邮箱自动关联账号；请保留本地恢复管理员。', true) +
      '</section></div>';
    const dialog = form(
      p ? '编辑 OIDC 提供者' : '添加 OIDC 提供者',
      body,
      (f) => {
        const data = Object.fromEntries(new FormData(f));
        data.enabled = f.elements.enabled.checked;
        data.freshness_seconds = Number(data.freshness_seconds);
        data.endpoint_origins = data.endpoint_origins
          .split(',')
          .map((s) => s.trim())
          .filter(Boolean);
        if (!data.client_secret) delete data.client_secret;
        if (p) data.expected_version = p.version;
        return api('/api/iam/oidc/providers' + (p ? '/' + p.id : ''), {
          method: p ? 'PUT' : 'POST',
          body: JSON.stringify(data),
        });
      },
      { large: true },
    );
    if (p) $('#iam-issuer', dialog).readOnly = true;
  }
  async function mappingEditor(id) {
    const [mapping, targets] = await Promise.all([
      api('/api/iam/oidc/providers/' + id + '/mappings'), api('/api/iam/oidc/targets')]);
    const d = form('OIDC 群组直接权限映射',
      mapping.mappings.map((m) => `<div class="grant-row"><span>${esc(m.group_name)} → ${esc(targets.spaces.find((x) => x.id === m.space_id)?.label || m.space_id)}</span><button class="btn danger" type="button" data-delete-map="${esc(m.id)}">移除</button></div>`).join('') +
      field('group_name','精确群组名称','','required') +
      select('space_id','工作空间', targets.spaces.map((x) => [x.id,x.label]), '') +
      '<div class="field"><label for="iam-policy">直接成员权限（JSON）</label><textarea id="iam-policy" name="policy" rows="10" required>{"manage":[],"project_rules":[]}</textarea></div>' +
      notice('仅同步 UserInfo 群组 claim；人工成员权限独立保留。不会授予实例管理员权限。'),
      (f) => post('/api/iam/oidc/providers/'+id+'/mappings', {group_name:f.elements.group_name.value, space_id:f.elements.space_id.value, policy:JSON.parse(f.elements.policy.value)}), {large:true});
    $$('[data-delete-map]',d).forEach((b) => b.onclick = () => busy(b,async () => {
      await api('/api/iam/oidc/mappings/'+b.dataset.deleteMap,{method:'DELETE'});
      closeModal(d); await mappingEditor(id);
    }));
  }
  function bind() {
    $$('[data-identity-tab]').forEach((button) => {
      button.onclick = () => {
        if (button.dataset.identityTab === (S.identityTab || 'account')) return;
        return navigate(
          button.dataset.identityTab === 'account'
            ? 'identity'
            : 'identity/' + button.dataset.identityTab,
        );
      };
    });
    $$('[data-iam-link]').forEach((x) => (x.onclick = () => freshLink(x.dataset.iamLink)));
    $$('[data-iam-unlink]').forEach(
      (x) =>
        (x.onclick = () =>
          busy(x, async () => {
            if (!confirm('解除关联并撤销此身份签发的会话和 MCP 连接？')) return;
            const out = await api('/api/iam/identities/' + x.dataset.iamUnlink, {
              method: 'DELETE',
            });
            if (out.relogin_required) endSession(true);
            else await renderPage(false);
          })),
    );
    $$('[data-iam-session]').forEach(
      (x) =>
        (x.onclick = () =>
          busy(x, async () => {
            await api('/api/iam/sessions/' + x.dataset.iamSession, { method: 'DELETE' });
            const checkSession = await api('/api/session');
            if (!checkSession.authenticated) endSession(true);
            else await renderPage(false);
          })),
    );
    $$('[data-iam-provider]').forEach(
      (x) => (x.onclick = () => providerEdit(x.dataset.iamProvider)),
    );
    $$('[data-iam-mappings]').forEach(
      (x) => (x.onclick = () => busy(x, () => mappingEditor(x.dataset.iamMappings))),
    );
    $$('[data-iam-check]').forEach(
      (x) =>
        (x.onclick = () =>
          busy(x, async () => {
            const login = S.session,
              space = location.hash,
              page = S.page,
              tab = S.identityTab;
            const intent = (S.modalIntent = (S.modalIntent || 0) + 1);
            const out = await post('/api/iam/oidc/providers/' + x.dataset.iamCheck + '/check');
            if (
              login !== S.session ||
              space !== location.hash ||
              page !== S.page ||
              tab !== S.identityTab ||
              intent !== S.modalIntent
            )
              return;
            modal(
              'OIDC 发现已验证',
              `${(out.warnings || []).map((text) => notice(esc(text), true)).join('')}<p>签名密钥：${out.signing_keys}</p><p>精确回调：</p><div class="code-box iam-wrap">${esc(out.callback)}</div><p>Back-channel logout：</p><div class="code-box iam-wrap">${esc(out.backchannel_logout)}</div>`,
            );
          })),
    );
    $$('[data-iam-restore]').forEach(
      (x) =>
        (x.onclick = () =>
          busy(x, async () => {
            const s = me.disabled_spaces.find((s) => s.id === x.dataset.iamRestore);
            await post('/api/spaces/' + s.id + '/restore', {
              label: s.label,
              active: true,
              expected_version: s.version,
              idempotency_key: 'restore-'+uid(),
            });
            await refresh();
            await renderPage(false);
          })),
    );
    $$('[data-iam-user]').forEach(
      (x) =>
        (x.onclick = () => {
          const u = users.find((u) => u.id === x.dataset.iamUser);
          form(
            '管理账号',
            check('active', '启用账号', !!u.active) +
              check('instance_admin', '实例管理员（全局管理权限）', !!u.instance_admin) +
              notice('修改会撤销该账号现有会话和 MCP 凭据；最后一位恢复管理员不能移除。'),
            (f) =>
              api('/api/iam/users/' + u.id, {
                method: 'PUT',
                body: JSON.stringify({
                  active: f.elements.active.checked,
                  instance_admin: f.elements.instance_admin.checked,
                  expected_version: u.version,
                }),
              }),
          );
        }),
    );
    $$('[data-iam]').forEach(
      (x) =>
        (x.onclick = () =>
          busy(x, async () => {
            switch (x.dataset.iam) {
              case 'accept-invite':
                form(
                  '接受团队邀请',
                  field('invitation', '邀请凭据', '', 'required autocomplete="off"'),
                  (f) =>
                    post('/api/invitations/accept', { invitation: f.elements.invitation.value }),
                );
                break;
              case 'new-provider':
                providerEdit();
                break;
              case 'sync':
                await post('/api/iam/oidc/reconcile');
                toast('权限同步已完成；网络故障不会延长旧权限有效期。');
                await renderPage(false);
                break;
            }
          })),
    );
  }
  function applyCapabilities() {
    if (!admin()) {

    }
    $$(
      '[data-action="agent-update"],[data-action="agent-restart"],[data-action="agent-uninstall"],[data-action="agent-commands"],[data-action="agent-repair"]',
    ).forEach((x) => {
      const d = S.devices.find((d) => d.id === x.dataset.id);
      if (d && d.can_manage === false) x.disabled = true;
    });
  }
  return {
    loginButtons,
    canNavigate: () => true,
    bootstrap,
    bindShell,
    html,
    bind,
    refresh,
    admin,
    current,
    reset,
    applyCapabilities,
  };
})();
