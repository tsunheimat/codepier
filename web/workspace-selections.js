'use strict';
// Workspace allocations consume persisted inventory, never SSH discovery APIs.
window.CodePierWorkspaceSelections = (() => {
  let draft = null;
  const statusLabels = {
    configured: '已配置（尚未验证连接）',
    ready: '连接测试通过',
    connection_failed: '上层连接测试失败',
    offline: 'Runner 离线',
    disabled: '上层已停用',
    withdrawn: 'Runner 已撤销授权',
    unavailable: '上层配置不可用',
    unverified: '等待 Runner 上报 / 更新',
    parent_unavailable: '上层资源不可访问',
  };
  function reset() {
    draft = null;
  }
  function html(space, kind, data, projects = []) {
    const page = location.hash,
      login = S.session;
    const identity = JSON.stringify([login?.user_id, login?.csrf]);
    // Security revalidation replaces S.session even for the same login. Never
    // restore old private form values across that boundary. Retain only an
    // uncertain-save marker for the same account/session and exact view; the
    // rows below come exclusively from the newly authorized parent snapshot.
    const pending =
      draft &&
      draft.page === page &&
      draft.identity === identity &&
      (draft.saving || draft.uncertain);
    if (!draft || draft.page !== page || draft.login !== login || (!draft.dirty && !draft.saving)) {
      draft = {
        page,
        login,
        identity,
        kind,
        revision: data.revision,
        rows: new Map(),
        dirty: false,
        saving: false,
        uncertain: !!pending,
        query: '',
        key: pending ? draft.key : 'selection-' + uid(),
      };
    }
    const currentIds = new Set();
    const rows = data.items
      .map((entry) => {
        const source = kind === 'vps' ? entry.resource : entry.device;
        const id = source.id;
        currentIds.add(id);
        if (!draft.rows.has(id))
          draft.rows.set(id, {
            selected: entry.selected,
            name: entry.binding?.name || entry.alias || source.name,
            description: entry.binding?.description || '',
            display_mode: entry.binding?.display_mode || 'top_level',
            projects: (entry.binding?.projects || []).map((p) => p.id),
          });
        const value = draft.rows.get(id);
        const status = source.status || (source.online ? 'configured' : 'offline');
        return `<article class="ws-selection-row" data-selection-row="${esc(id)}" data-selection-search="${esc([source.name, source.host_alias, source.runner_name].filter(Boolean).join(' ').toLocaleLowerCase())}">
        <label class="check ws-selection-choice"><input type="checkbox" data-selection-check ${value.selected ? 'checked' : ''}><strong>${esc(source.name)}</strong></label>
        ${kind === 'vps' && entry.binding?.can_execute ? `<button type="button" class="btn small" data-ws-action="vps-command" data-id="${esc(entry.binding.id)}">运行命令</button>` : ''}
        <div class="ws-selection-origin"><span>${esc(statusLabels[status] || status)}</span>${kind === 'vps' ? `<small>SSH Alias：${esc(source.host_alias)} · Runner：${esc(source.runner_name)}</small>` : ''}${source.source_error ? `<small role="status">${esc(source.source_error)} · 请在 Devices 处理</small>` : ''}</div>
        <label class="field">工作空间别名<input data-selection-alias aria-label="${esc(source.name)}：工作空间别名" value="${esc(value.name || source.name)}" maxlength="${kind === 'vps' ? 253 : 100}" required></label>
        ${
          kind === 'vps'
            ? `<label class="field">显示位置<select data-selection-mode aria-label="${esc(source.name)}：显示位置">${[
                ['top_level', '工作空间顶层'],
                ['project_only', '仅 Project 类别'],
                ['both', '顶层及 Project 类别'],
              ]
                .map(
                  ([id, label]) =>
                    `<option value="${id}" ${id === value.display_mode ? 'selected' : ''}>${label}</option>`,
                )
                .join('')}</select></label>
        <label class="field">说明（可选）<input data-selection-description value="${esc(value.description)}" maxlength="2000"></label>
        <details open data-selection-projects ${value.display_mode === 'top_level' ? 'hidden' : ''}><summary>Project 位置（${value.projects.length}）</summary><fieldset>${projects.map((p) => `<label class="check"><input type="checkbox" data-selection-project value="${esc(p.id)}" ${value.projects.includes(p.id) ? 'checked' : ''}>${esc(p.alias)}</label>`).join('') || '<p>此工作空间尚无 Project。</p>'}</fieldset></details>`
            : ''
        }
      </article>`;
      })
      .join('');
    // Never silently save a disappeared parent as a different resource.
    draft.currentIds = currentIds;
    draft.unavailable = data.unavailable || [];
    return `<div data-workspace-selection="${kind}"><p class="form-note">从上层资源列表勾选，直接修改每行别名${kind === 'vps' ? '及显示位置' : ''}，一次保存。${kind === 'vps' ? 'SSH 配置、发现与验证只在 Devices 管理；这里不发起扫描。分配 VPS 后，获准使用该 VPS 的 Profile / 成员即有 Shell 权限。' : '设备分配不改变 Folder / Storage Pool 的执行设备与本机权限。'}</p>
      ${draft.unavailable.length ? `<p class="form-note" role="status">${draft.unavailable.map((v) => esc(v.name)).join('、')}：上层资源已不可访问，保存选择会解除这些分配，不会重建 SSH 目标。</p>` : ''}
      <label class="field">筛选资源<input type="search" data-selection-search-input value="${esc(draft.query)}" placeholder="名称 / SSH Alias / Runner"></label>
      <div class="actions"><button type="button" class="btn small" data-selection-all>勾选筛选结果</button><button type="button" class="btn small" data-selection-none>取消全部</button><button type="button" class="btn small" data-selection-reload>重新读取资源列表</button><span data-selection-count></span></div>
      <fieldset data-selection-fields ${draft.saving || draft.uncertain ? 'disabled' : ''}>${rows || '<p class="form-note">没有可选的上层资源。请先在 Devices 的 Runner 设置授权 SSH Alias；VPS 会自动建立，无需重复登记。</p>'}</fieldset>
      <button type="button" class="btn primary" data-selection-save ${!draft.dirty || draft.saving || draft.uncertain ? 'disabled' : ''}>保存选择</button><p data-selection-status role="status">${draft.uncertain ? '保存结果不确定。请重新读取列表核对，不自动重送。' : draft.dirty ? '有未保存的修改' : '没有未保存的修改'}</p></div>`;
  }
  function bind(space, kind) {
    const root = document.querySelector('[data-workspace-selection]');
    if (!root || !draft || draft.kind !== kind) return;
    const state = draft;
    const sameView = () =>
      draft === state && S.session === state.login && location.hash === state.page;
    const current = () => root.isConnected && sameView();
    const status = root.querySelector('[data-selection-status]');
    const save = root.querySelector('[data-selection-save]');
    const fieldset = root.querySelector('[data-selection-fields]');
    const update = () => {
      if (!current()) return;
      const query = state.query.toLocaleLowerCase();
      root.querySelectorAll('[data-selection-row]').forEach((row) => {
        const value = state.rows.get(row.dataset.selectionRow);
        row.hidden = !row.dataset.selectionSearch.includes(query);
        row.querySelectorAll('input:not([data-selection-check]),select').forEach((n) => {
          n.disabled = !value.selected;
        });
        const positions = row.querySelector('[data-selection-projects]');
        if (positions) {
          positions.hidden = value.display_mode === 'top_level';
          positions.open = value.display_mode !== 'top_level';
          positions.querySelector('summary').textContent =
            'Project 位置（' + value.projects.length + '）';
        }
      });
      root.querySelector('[data-selection-count]').textContent =
        `已选择 ${[...state.rows.values()].filter((r) => r.selected).length} 项`;
      save.disabled = !state.dirty || state.saving || state.uncertain;
      fieldset.disabled = state.saving || state.uncertain;
      if (!state.saving && !state.uncertain)
        status.textContent = state.dirty ? '有未保存的修改' : '没有未保存的修改';
    };
    root.querySelectorAll('[data-selection-row]').forEach((row) => {
      const value = state.rows.get(row.dataset.selectionRow);
      row.oninput = () => {
        if (state.saving || state.uncertain) return;
        value.selected = row.querySelector('[data-selection-check]').checked;
        value.name = row.querySelector('[data-selection-alias]').value;
        if (kind === 'vps') {
          value.display_mode = row.querySelector('[data-selection-mode]').value;
          value.description = row.querySelector('[data-selection-description]').value;
          value.projects = [...row.querySelectorAll('[data-selection-project]:checked')].map(
            (n) => n.value,
          );
        }
        state.dirty = true;
        update();
      };
      row.onchange = row.oninput;
    });
    root.querySelector('[data-selection-search-input]').oninput = (event) => {
      state.query = event.target.value;
      update();
    };
    root.querySelector('[data-selection-all]').onclick = () => {
      if (state.saving || state.uncertain) return;
      root.querySelectorAll('[data-selection-row]').forEach((row) => {
        if (row.hidden) return;
        state.rows.get(row.dataset.selectionRow).selected = true;
        row.querySelector('[data-selection-check]').checked = true;
      });
      state.dirty = true;
      update();
    };
    root.querySelector('[data-selection-none]').onclick = () => {
      if (state.saving || state.uncertain) return;
      state.rows.forEach((r) => {
        r.selected = false;
      });
      root.querySelectorAll('[data-selection-check]').forEach((n) => {
        n.checked = false;
      });
      state.dirty = true;
      update();
    };
    root.querySelector('[data-selection-reload]').onclick = async () => {
      if (
        state.saving ||
        ((state.dirty || state.uncertain) && !confirm('丢弃本页草稿并重新读取已保存的选择？'))
      )
        return;
      reset();
      await renderPage(false);
    };
    save.onclick = async () => {
      if (!current() || save.disabled) return;
      const items = [];
      for (const [id, value] of state.rows) {
        if (!value.selected) continue;
        if (!state.currentIds.has(id)) {
          status.textContent = '上层资源已变化；请重新读取并确认选择。';
          return;
        }
        if (!value.name.trim()) {
          status.textContent = '已选资源需要工作空间别名。';
          return;
        }
        if (kind === 'vps' && value.display_mode !== 'top_level' && !value.projects.length) {
          status.textContent = 'Project 显示位置至少选择一个 Project。';
          return;
        }
        items.push(
          kind === 'vps'
            ? {
                resource_id: id,
                name: value.name,
                description: value.description,
                display_mode: value.display_mode,
                projects: value.display_mode === 'top_level' ? [] : value.projects,
              }
            : { device_id: id, alias: value.name },
        );
      }
      if (state.unavailable.length && !confirm('保存将解除已不可访问的上层资源分配，继续？'))
        return;
      const body = { items, expected_revision: state.revision, idempotency_key: state.key };
      state.saving = true;
      update();
      status.textContent = '正在保存整份选择…';
      try {
        await api(
          '/api/spaces/' +
            encodeURIComponent(space.id) +
            '/' +
            (kind === 'vps' ? 'vps-selection' : 'device-selection'),
          { method: 'PUT', body: JSON.stringify(body) },
        );
        if (!sameView()) return;
        reset();
        await renderPage(false);
        toast('选择已保存');
      } catch (error) {
        if (
          error.code === 'SESSION_CHANGED' &&
          error.mutation_acknowledged &&
          draft?.key === state.key &&
          draft.identity === state.identity &&
          location.hash === state.page
        ) {
          // Never reuse the old response or its form values after a security
          // boundary. A confirmed PUT only permits a fresh authorized read.
          reset();
          if (S.identity && document.querySelector('#breadcrumb-page')) await renderPage(false);
          return;
        }
        if (!sameView()) return;
        state.uncertain =
          error.code === 'NETWORK_UNCERTAIN' || [408, 500, 502, 503, 504].includes(error.status);
        state.saving = false;
        if (!state.uncertain) state.key = 'selection-' + uid();
        if (!root.isConnected) {
          await renderPage(false);
          return;
        }
        update();
        status.textContent =
          error.message +
          (state.uncertain
            ? ' · 结果不确定，请重新读取核对；不会自动重送。'
            : ' · 草稿保留，未自动重送。');
      }
    };
    update();
  }
  const hasUnsaved = () =>
    !!draft && draft.login === S.session && (draft.dirty || draft.saving || draft.uncertain);
  function requestNavigation(page) {
    if (!draft || draft.login !== S.session) {
      reset();
      return true;
    }
    if (draft.page === '#' + page) return true;
    if (hasUnsaved() && !confirm('资源选择尚未保存或结果未确认。离开将丢弃本页草稿，继续？')) {
      history.replaceState(null, '', location.pathname + location.search + draft.page);
      return false;
    }
    reset();
    return true;
  }
  return { html, bind, reset, hasUnsaved, requestNavigation };
})();
