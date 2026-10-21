'use strict';
window.CodePierRunnerSettings = (() => {
  const labels = {
    name: 'Runner 名称',
    hub_url: 'Hub 地址（本机修改）',
    state_dir: '状态目录（本机修改）',
    allowed_roots: '授权目录',
    tasks: '命名任务',
    shell: '本机 Shell',
    ssh: 'SSH / Host 授权',
    sessions: '持久会话 / PTY',
    rch: '远端编译 RCH',
    mcp_policy: 'MCP 执行策略',
    skills: '技能目录',
    computer: 'Computer / 原生权限',
    integrations: '开发工具 / 浏览器 / 文件导入',
    checkpoint_max_mib: 'Checkpoint 容量（MiB）',
    settings_management: '远程设置授权（本机修改）',
  };
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const local = new Set(['hub_url', 'state_dir', 'settings_management']);
  const pathText = (path) => path.join('.');
  const pathAttr = (path) => esc(JSON.stringify(path));
  const dynamic = (path) =>
    ['tasks', 'ssh.targets', 'integrations.language_servers'].includes(pathText(path)) ||
    ['env', 'settings', 'initialization_options'].includes(path.at(-1));
  const requestKey = () => 'runner-settings-' + uid();
  const endpoint = (device) => '/api/devices/' + encodeURIComponent(device.id);
  const hosts = (device) => settled(api(endpoint(device) + '/ssh-hosts'));
  const probe = (device, host) =>
    settled(
      api(endpoint(device) + '/settings', {
        method: 'POST',
        body: JSON.stringify({
          operation: 'probe',
          host_alias: host.host_alias,
          expected_target_fingerprint: host.target_fingerprint,
          idempotency_key: requestKey(),
        }),
      }),
    );

  async function open(device) {
    const login = S.session,
      page = location.hash;
    const d = modal(
      'Runner 设置 · ' + device.name,
      '<div data-runner-settings><p role="status">正在读取 Runner 配置…</p></div>',
      '<button class="btn ghost" data-action="close-modal">关闭</button>',
      true,
    );
    const root = $('[data-runner-settings]', d);
    const current = () => d.isConnected && S.session === login && location.hash === page;
    let snapshot = null,
      initial = {},
      draft = {},
      loading = false,
      uncertain = false,
      backup = '',
      activeReceipt = '';
    const status = (text) => {
      const node = $('[data-rs-status]', root);
      if (node) node.textContent = text;
    };
    const valueAt = (path) => path.reduce((value, key) => value?.[key], draft);
    const setAt = (path, value) => {
      let target = draft;
      for (const key of path.slice(0, -1)) {
        if (target[key] === null || target[key] === undefined)
          target[key] = clone(snapshot.defaults?.[key] ?? {});
        target = target[key];
      }
      target[path.at(-1)] = value;
    };
    const patch = () =>
      Object.fromEntries(
        [...new Set([...Object.keys(initial), ...Object.keys(draft)])]
          .filter((key) => JSON.stringify(initial[key]) !== JSON.stringify(draft[key]))
          .map((key) => [key, Object.hasOwn(draft, key) ? draft[key] : null]),
      );
    const changedLocal = () => Object.keys(patch()).some((key) => local.has(key));
    function controls() {
      if (!current() || !snapshot || !$('[data-rs-fields]', root)) return;
      const dirty = Object.keys(patch()).length > 0;
      const cannotSend =
        loading || uncertain || snapshot.stale || snapshot.status === 'unavailable';
      $('[data-rs-fields]', root).disabled = loading || uncertain;
      $('[data-rs-apply]', root).disabled =
        cannotSend || !snapshot.editable || !dirty || changedLocal();
      $('[data-rs-preview]', root).disabled = cannotSend || !dirty || changedLocal();
      $('[data-rs-export]', root).disabled = loading || uncertain || !dirty;
      $('[data-rs-json-load]', root).disabled = loading || uncertain;
      $('[data-rs-rollback]', root).disabled =
        cannotSend ||
        !snapshot.editable ||
        !/^[a-f0-9]{64}$/.test($('[data-rs-backup]', root).value);
      $('[data-rs-dirty]', root).textContent = dirty
        ? Object.keys(patch()).join(', ') +
          (changedLocal() ? ' · 包含本机专用字段，请导出补丁在 Runner 执行' : '')
        : '没有未保存的修改';
    }
    function input(value, path) {
      const name = pathText(path),
        attr = pathAttr(path);
      if (typeof value === 'boolean')
        return `<label class="check"><input type="checkbox" data-rs-value="${attr}" ${value ? 'checked' : ''}>${esc(path.at(-1))}</label>`;
      if (Array.isArray(value)) {
        return `<fieldset class="runner-setting-array"><legend>${esc(path.at(-1))}</legend>${value
          .map(
            (child, i) =>
              `<div class="runner-setting-item">${input(child, [...path, i])}<button type="button" class="btn danger small" data-rs-remove="${pathAttr([...path, i])}" aria-label="移除 ${esc(name)} ${i + 1}">移除</button></div>`,
          )
          .join('')}
          <button type="button" class="btn small" data-rs-add="${attr}">添加 ${esc(path.at(-1))}</button></fieldset>`;
      }
      if (value !== null && typeof value === 'object') {
        const optional =
          name === 'integrations'
            ? ['file_hosts', 'extra_file_hosts'].filter((key) => !Object.hasOwn(value, key))
            : [];
        return `<fieldset class="runner-setting-object"><legend>${esc(path.at(-1))}</legend>${Object.entries(
          value,
        )
          .map(
            ([key, child]) =>
              `<div class="runner-setting-entry">${input(child, [...path, key])}${dynamic(path) || optional.includes(key) || ['integrations.file_hosts', 'integrations.extra_file_hosts'].includes(name + '.' + key) ? `<button type="button" class="btn ghost small" data-rs-remove="${pathAttr([...path, key])}">删除 ${esc(key)}</button>` : ''}</div>`,
          )
          .join('')}
          ${dynamic(path) ? `<button type="button" class="btn small" data-rs-map-add="${attr}">添加 ${name === 'ssh.targets' ? 'SSH Alias 授权' : '条目'}</button>` : ''}
          ${optional.map((key) => `<button type="button" class="btn small" data-rs-optional="${esc(key)}">自定义 ${esc(key)}（未设置时继承默认）</button>`).join('')}</fieldset>`;
      }
      return `<label class="field">${esc(path.at(-1))}<input data-rs-value="${attr}" aria-label="${esc(name)}" type="${typeof value === 'number' ? 'number' : value === snapshot.private_placeholder ? 'password' : 'text'}" value="${esc(value ?? '')}" ${value === null ? 'data-rs-null="true"' : ''} ${typeof value === 'number' ? 'step="1"' : ''} autocomplete="off">${value === snapshot.private_placeholder ? '<small>隐藏值保持不变；删除字段才会删除原值。</small>' : ''}</label>`;
    }
    function renderFields() {
      const holder = $('[data-rs-fields]', root);
      const expanded = new Set(
        [...holder.querySelectorAll('details[open]')].map((node) => node.dataset.rsGroup),
      );
      holder.innerHTML = snapshot.groups
        .map(
          (
            group,
          ) => `<details class="runner-setting-group" data-rs-group="${esc(group.key)}" ${expanded.has(group.key) ? 'open' : ''}>
        <summary>${esc(labels[group.key] || group.key)} <small>${esc(group.status)} · ${group.management === 'local_only' ? '仅本机' : snapshot.editable ? '本机已授权远程管理' : '远程写入未授权'}</small></summary>
        ${group.restart_required ? '<p class="form-note">本组变更可能需要正常重启；保存不会终止现有任务。</p>' : ''}
        ${input(draft[group.key] ?? snapshot.defaults?.[group.key] ?? '', [group.key])}</details>`,
        )
        .join('');
      $$('[data-rs-value]', holder).forEach((node) => {
        node.oninput = () => {
          const path = JSON.parse(node.dataset.rsValue);
          let value =
            node.type === 'checkbox'
              ? node.checked
              : node.type === 'number'
                ? node.valueAsNumber
                : node.value;
          if (node.type === 'number' && !Number.isFinite(value)) {
            node.setCustomValidity('请输入整数');
            return;
          }
          node.setCustomValidity('');
          if (node.dataset.rsNull && value === '') value = null;
          setAt(path, value);
          controls();
        };
      });
      $$('[data-rs-remove]', holder).forEach(
        (node) =>
          (node.onclick = () => {
            const path = JSON.parse(node.dataset.rsRemove),
              parent = valueAt(path.slice(0, -1)),
              key = path.at(-1);
            if (Array.isArray(parent)) parent.splice(key, 1);
            else delete parent[key];
            renderFields();
            controls();
          }),
      );
      $$('[data-rs-add]', holder).forEach(
        (node) =>
          (node.onclick = () => {
            const path = JSON.parse(node.dataset.rsAdd),
              name = pathText(path);
            valueAt(path).push(clone(snapshot.templates?.[name] ?? ''));
            renderFields();
            controls();
          }),
      );
      $$('[data-rs-map-add]', holder).forEach(
        (node) =>
          (node.onclick = () => {
            const path = JSON.parse(node.dataset.rsMapAdd),
              name = pathText(path),
              map = valueAt(path);
            let key = prompt(
              name === 'ssh.targets'
                ? '输入 Runner SSH Config 中明确批准的 Host Alias'
                : '新增条目名称',
            );
            if (!key || ['__proto__', 'prototype', 'constructor'].includes(key)) return;
            let value = clone(snapshot.templates?.[name] ?? '');
            if (name === 'ssh.targets') {
              value.host_alias = key;
              key = 'host_' + uid().replaceAll('-', '');
            }
            if (Object.hasOwn(map, key)) {
              status('该名称已经存在');
              return;
            }
            if (['settings', 'initialization_options'].includes(path.at(-1))) {
              const raw = prompt('输入 JSON 值（文字需要双引号）', '""');
              if (raw === null) return;
              try {
                value = JSON.parse(raw);
              } catch {
                status('JSON 值无效');
                return;
              }
            }
            map[key] = value;
            renderFields();
            controls();
          }),
      );
      $$('[data-rs-optional]', holder).forEach(
        (node) =>
          (node.onclick = () => {
            draft.integrations[node.dataset.rsOptional] = [];
            renderFields();
            controls();
          }),
      );
    }
    async function load(force = false) {
      if (!current() || loading || uncertain) return;
      if (force && Object.keys(patch()).length && !confirm('重新读取会丢弃未保存输入。继续？'))
        return;
      loading = true;
      status('正在读取 Runner…');
      try {
        const value = await settled(api(endpoint(device) + '/settings'));
        if (!current()) return;
        snapshot = value;
        if (value.status === 'builtin_roots_only') {
          root.innerHTML = notice(
            '内置 Runner 仅使用 Hub 管理的目录授权，不开放执行能力或本机配置。',
          );
          return;
        }
        if (!value.groups?.length) {
          root.innerHTML = notice(
            '尚无可用配置快照。请使 Runner 上线并升级；不会把空数据作为已保存配置。',
          );
          return;
        }
        draft = Object.fromEntries(
          value.groups.map((group) => [
            group.key,
            clone(value.configuration[group.key] ?? value.defaults?.[group.key] ?? ''),
          ]),
        );
        initial = clone(draft);
        root.innerHTML = `<p class="form-note">配置来源：${value.stale ? '离线缓存 / 不可应用' : 'Runner 实时快照'} · ${esc(value.platform || '')} · ${esc(value.version || '')} · ${esc(value.status)} · ${esc(timeText(value.observed_at))}</p>
          ${notice(value.editable ? '本机已明确启用远程配置。Shell 运行于 Runner 操作系统账号，不是目录沙箱。' : '远程写入未授权。仍可编辑并导出最小补丁；由 Runner 所有者在本机验证 / 应用或开启远程管理。')}
          ${value.unknown_fields?.length ? notice('以下非正式字段将保留，但不能在本页管理：' + value.unknown_fields.join(', ')) : ''}
          <p class="form-note">设备密钥与身份不可编辑。环境变量和敏感字段使用保留占位符。配置、已生效、已验证与连接健康是独立状态。</p>
          <fieldset data-rs-fields></fieldset><p data-rs-dirty class="form-note"></p>
          <div class="actions"><button class="btn" data-rs-refresh>重新读取</button><button class="btn" data-rs-preview>校验 / 预览</button><button class="btn primary" data-rs-apply>保存到 Runner</button><button class="btn" data-rs-export>导出最小补丁</button></div>
          <p data-rs-status role="status" aria-live="polite"></p><div data-rs-receipt></div>
          <details><summary>本机应用命令 / 授权</summary><pre class="code-box">${esc(value.local_command || '')}\n\n${esc(value.local_authorization || '')}</pre><p>先替换命令中的文件路径。只有本机操作者能开启 settings_management.enabled；不要通过另一个远程 Shell 绕过这个授权。</p></details>
          <details><summary>高级 JSON（隐藏值保留）</summary><textarea data-rs-json rows="16" aria-label="Runner settings JSON"></textarea><button class="btn" data-rs-json-load>载入 JSON 到表单</button><button class="btn ghost" data-rs-json-refresh>用当前表单刷新 JSON</button></details>
          <details><summary>生效值 / 就绪检查（不等于连接已测试）</summary><pre class="code-box">${esc(JSON.stringify({ effective: value.effective, readiness: value.readiness, restart_required: value.restart_required }, null, 2))}</pre></details>
          <details><summary>备份回滚</summary><label class="field">备份 revision<input data-rs-backup value="${esc(backup)}" pattern="[a-f0-9]{64}"></label><button class="btn danger" data-rs-rollback>回滚可远程管理的设置</button><p>回滚同样检查当前 revision，不还原身份、Hub 地址、状态目录或本机远程管理授权。</p></details>
          <section><h3>SSH Host 发现</h3><p>仅解析配置，不执行 Match exec 或 ProxyCommand。发现不等于授权；连接测试必须单独发起。</p><button class="btn" data-rs-discover>刷新 Host 列表</button><div data-rs-hosts></div></section>`;
        renderFields();
        $('[data-rs-json]', root).value = JSON.stringify(draft, null, 2);
        $('[data-rs-refresh]', root).onclick = () => void load(true);
        $('[data-rs-preview]', root).onclick = () => void submit('preview');
        $('[data-rs-apply]', root).onclick = () => void submit('apply');
        $('[data-rs-rollback]', root).onclick = () => {
          if (confirm('回滚将覆盖可远程管理的配置字段，仍不会终止已运行任务。继续？'))
            void submit('rollback');
        };
        $('[data-rs-backup]', root).oninput = controls;
        $('[data-rs-export]', root).onclick = () => {
          const blob = new Blob([JSON.stringify(patch(), null, 2)], { type: 'application/json' }),
            url = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = url;
          link.download = 'runner-settings.patch.json';
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          status('补丁已导出；尚未应用。请在 Runner 上使用显示的 revision 执行本机命令。');
        };
        $('[data-rs-json-refresh]', root).onclick = () => {
          $('[data-rs-json]', root).value = JSON.stringify(draft, null, 2);
        };
        $('[data-rs-json-load]', root).onclick = () => {
          try {
            const value = JSON.parse($('[data-rs-json]', root).value);
            if (
              !value ||
              Array.isArray(value) ||
              typeof value !== 'object' ||
              Object.keys(value).some((key) => !Object.hasOwn(labels, key))
            )
              throw new Error('只接受公开的 Runner 设置字段');
            draft = value;
            renderFields();
            controls();
            status('JSON 已载入表单，尚未验证或应用。');
          } catch (error) {
            status('JSON 无效：' + error.message);
          }
        };
        $('[data-rs-discover]', root).onclick = () => void discoverHosts();
      } catch (error) {
        if (current()) root.innerHTML = notice(error.message);
      } finally {
        loading = false;
        controls();
      }
    }
    async function submit(operation) {
      if (loading || uncertain || !current()) return;
      if (
        operation === 'apply' &&
        !confirm(
          '将这份配置保存到 Runner？正在运行的任务不会被终止；需要重启的设置不会被报告为已生效。',
        )
      )
        return;
      const body = {
        operation,
        expected_revision: snapshot.revision,
        idempotency_key: requestKey(),
      };
      if (operation === 'rollback') body.backup_revision = $('[data-rs-backup]', root).value;
      else body.patch = patch();
      loading = true;
      controls();
      status('正在验证原配置 revision…');
      try {
        const result = await settled(
          api(endpoint(device) + '/settings', { method: 'POST', body: JSON.stringify(body) }),
        );
        if (!current()) return;
        activeReceipt = result.operation_id;
        backup = result.backup_revision || backup;
        $('[data-rs-receipt]', root).textContent =
          '原操作：' + (activeReceipt || '') + (backup ? ' · 备份：' + backup : '');
        status(
          operation === 'preview'
            ? '验证通过，未保存。变更：' + result.changed_fields.join(', ')
            : result.saved
              ? '已保存到 Runner，尚待确认生效。请重新读取；需要重启：' +
                (result.restart_required.join(', ') || '无')
              : '没有需要保存的变更。',
        );
        if (result.saved) {
          // Keep stale revisions fenced. Reading a new snapshot is an explicit
          // action; saved must never silently become applied/healthy.
          snapshot.editable = false;
          initial = clone(draft);
          $('[data-rs-backup]', root).value = backup;
        }
      } catch (error) {
        if (!current()) return;
        activeReceipt = error.operation_id || activeReceipt;
        uncertain =
          error.operation_uncertain ||
          ['NETWORK_UNCERTAIN', 'CONFIG_WRITE_UNCERTAIN'].includes(error.code) ||
          [408, 500, 502, 503, 504].includes(error.status);
        status(
          error.message +
            (uncertain ? ' · 保存结果不确定，禁止重新提交。请查看原操作或在本机核对配置。' : ''),
        );
        if (activeReceipt) {
          const button = document.createElement('button');
          button.className = 'btn';
          button.textContent = '查看原操作 ' + activeReceipt;
          button.onclick = () => operationDetail(activeReceipt);
          $('[data-rs-receipt]', root).replaceChildren(button);
        }
      } finally {
        loading = false;
        controls();
      }
    }
    async function discoverHosts() {
      const holder = $('[data-rs-hosts]', root);
      holder.textContent = '正在安全扫描…';
      try {
        const result = await hosts(device);
        if (!current()) return;
        holder.innerHTML =
          `<p>${esc((result.warnings || []).join(' · '))}${result.stale ? ' · 过期缓存，不可授权' : ''}</p>` +
          (result.hosts || [])
            .map(
              (host, index) =>
                `<article class="grant-row"><div><strong>${esc(host.host_alias)}</strong><p>${esc(host.status)} · ${host.owner_approved ? '本机明确授权' : '仅发现，未授权'}</p></div>${host.owner_approved && host.status === 'configured' && !result.stale ? `<button class="btn small" data-rs-probe="${index}">明确测试连接</button>` : ''}</article>`,
            )
            .join('');
        $$('[data-rs-probe]', holder).forEach(
          (button) =>
            (button.onclick = () => {
              const host = result.hosts[Number(button.dataset.rsProbe)];
              if (
                !confirm(
                  '使用 Runner 的 SSH 账号连接 ' +
                    host.host_alias +
                    ' 并读取系统信息？OpenSSH 会执行本机配置的连接规则。',
                )
              )
                return;
              void busy(button, async () => {
                const observed = await probe(device, host);
                if (current())
                  status(
                    observed.ready
                      ? 'SSH 连接测试成功：' + JSON.stringify(observed.metadata)
                      : 'SSH 测试未成功：' + observed.error,
                  );
              });
            }),
        );
      } catch (error) {
        if (current()) holder.textContent = error.message;
      }
    }
    await load();
  }
  return { open, hosts, probe };
})();
