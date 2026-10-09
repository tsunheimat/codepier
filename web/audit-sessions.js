'use strict';
window.CodePierAuditSessions = (() => {
  let root = null,
    timer = null,
    inflight = null,
    epoch = 0;
  const st = () =>
    S.auditSessions ||
    (S.auditSessions = { offset: 0, q: '', state: '', project: '', tool: '', live: true });
  const params = () =>
    new URLSearchParams({
      limit: '30',
      offset: String(st().offset),
      q: st().q,
      state: st().state,
      project: st().project,
      tool: st().tool,
    }).toString();
  const help =
    '首次带有宿主 session 标识的 CodePier 调用会自动出现，无需登记或复制网址。尚未调用 CodePier 的标签页不可见；这里不判断 ChatGPT 思考、标签页活动或整个对话是否完成。';
  function detach() {
    clearTimeout(timer);
    timer = null;
    epoch++;
    inflight?.abort();
    inflight = null;
    root = null;
  }
  function clear() {
    detach();
    S.auditSessions = null;
  }
  const operation = (r) =>
    `<li><div class="session-action"><a href="#audit/operation/${esc(r.id)}"><code>${esc(r.tool)}${r.action ? ' · ' + esc(r.action) : ''}</code></a>${CodePierCallLog.stateBadge(r)}</div><p>${(r.resources || []).map((x) => `<span class="session-resource">${esc(x.type)} · ${esc(x.name)}</span>`).join('') || '<span class="muted">无资源关联 / 资源已不可见</span>'}</p>${r.summary ? `<p class="session-preview">${esc(r.summary.slice(0, 240))}${r.summary.length > 240 ? '…' : ''}</p>` : ''}${r.kind === 'mcp' ? '<small class="muted">仅为外部调用回执，下游任务状态未知</small>' : ''}</li>`;
  function card(s) {
    const count = s.active_count + s.attention_count;
    return `<article class="audit-session" data-session-id="${esc(s.id)}"><header><div><a href="#audit/session/${esc(s.id)}" class="session-title">${esc(s.label || s.platform)} <code>#${esc(s.short_id)}</code></a><small class="muted">连接 ${esc(s.grant_id ? s.grant_id.slice(0, 14) : '账号关联')} · ${esc(s.platform)}</small></div><span class="badge ${esc(s.state)}">${s.active_count ? s.active_count + ' 项进行中' : s.attention_count ? s.attention_count + ' 项待核实' : s.state === 'failed' ? '最近调用失败' : '最近记录'}</span></header><ol class="session-operations">${s.current.slice(0, 4).map(operation).join('') || '<li class="muted">仅有旧关联资料；没有可见调用回执。</li>'}</ol>${count > 4 ? `<p class="form-note">另有 ${count - 4} 项当前操作，在时间线中查看。</p>` : ''}<footer><time>最近观测 ${esc(timeText(s.last_activity))}</time><a class="btn small" href="#audit/session/${esc(s.id)}">查看会话时间线</a></footer></article>`;
  }
  function cards(data) {
    const groups = [
      ['active', '进行中的操作'],
      ['attention', '结果待核实'],
      ['recent', '最近记录（已返回 / 失败 / 结束）'],
    ];
    return (
      groups
        .map(([key, title]) => {
          const rows = data.sessions.filter((s) =>
            key === 'recent' ? !['active', 'attention'].includes(s.state) : s.state === key,
          );
          return rows.length
            ? `<section class="session-group" aria-label="${title}"><h2>${title}</h2><div class="session-grid">${rows.map(card).join('')}</div></section>`
            : '';
        })
        .join('') ||
      empty('当前筛选范围没有已观测会话。发起一次带 session 元数据的 CodePier 调用后自动显示。')
    );
  }
  const gap = (data) =>
    `<a href="#audit/unassociated">${data.unassociated_count} 项未关联活动</a> · 包含未提供 / 不支持 session 元数据的调用、面板操作及旧记录。${data.write_errors ? ' 活动记录曾写入失败，请核对原操作回执。' : ''}`;
  async function html(seq) {
    const login = S.session,
      filter = params();
    const [data, filters] = await Promise.all([
      api('/api/audit/sessions?' + filter),
      api('/api/call-log?limit=1'),
    ]);
    if (login !== S.session || seq !== S.renderSeq || filter !== params()) return '';
    const options = (rows, selected, label) =>
      rows
        .map(
          (r) =>
            `<option value="${esc(r.id ?? r)}" ${(r.id ?? r) === selected ? 'selected' : ''}>${esc(r[label] ?? r)}</option>`,
        )
        .join('');
    return (
      heading('操作审计', 'AUDIT / BY SESSION', help) +
      CodePierCallLog.tabs('sessions') +
      `<div id="audit-sessions"><form id="session-filter" class="filters"><label>观测状态<select id="session-state"><option value="">全部观测状态</option>${[
        ['active', '进行中'],
        ['attention', '待核实'],
        ['recent', '最近记录'],
        ['failed', '最近失败'],
      ]
        .map(([v, l]) => `<option value="${v}" ${st().state === v ? 'selected' : ''}>${l}</option>`)
        .join(
          '',
        )}</select></label><label>当前 / 最近项目<select id="session-project"><option value="">全部项目</option>${options(filters.projects, st().project, 'alias')}</select></label><label>当前 / 最近工具<select id="session-tool"><option value="">全部工具</option>${options(filters.tools, st().tool, 'name')}</select></label><label>会话标识或标签<input id="session-query" type="search" maxlength="200" value="${esc(st().q)}" placeholder="平台、标签或短编号"></label><button class="btn small" type="submit">筛选</button></form><div class="call-controls"><button class="btn ghost small" id="sessions-live" aria-pressed="${st().live}">${st().live ? '暂停实时更新' : '恢复实时更新'}</button><button class="btn ghost small" id="sessions-refresh">刷新</button><span id="sessions-sync" class="muted tiny" role="status">每 5 秒刷新观测状态</span></div><p id="session-gap" class="notice">${gap(data)}</p><div id="session-cards">${cards(data)}</div><div class="pagination"><span id="session-count">本页 ${data.sessions.length} 个会话 · 仅当前账号 / Space 可见</span><div class="actions"><button id="sessions-prev" class="btn ghost small" ${st().offset ? '' : 'disabled'}>上一页</button><button id="sessions-next" class="btn ghost small" ${data.next_offset === null ? 'disabled' : ''}>下一页</button></div></div></div>`
    );
  }
  function schedule() {
    clearTimeout(timer);
    if (root?.isConnected && st().live) timer = setTimeout(() => refresh(true), 5000);
  }
  async function refresh(automatic = false) {
    if (!root?.isConnected || S.page !== 'audit' || S.auditMode !== 'sessions') return detach();
    if (inflight) return;
    if (automatic && (!st().live || document.hidden)) return schedule();
    const generation = epoch,
      login = S.session,
      filter = params(),
      controller = new AbortController();
    inflight = controller;
    try {
      const data = await api('/api/audit/sessions?' + filter, {
        signal: controller.signal,
        retryDelays: [],
      });
      if (
        generation !== epoch ||
        login !== S.session ||
        filter !== params() ||
        !root?.isConnected ||
        (automatic && !st().live)
      )
        return;
      const body = root.querySelector('#session-cards'),
        selection = window.getSelection();
      if (
        automatic &&
        (body.contains(document.activeElement) ||
          (selection && !selection.isCollapsed && body.contains(selection.anchorNode)))
      ) {
        root.querySelector('#sessions-sync').textContent = '有新观测；结束阅读或点击刷新后更新';
        return;
      }
      body.innerHTML = cards(data);
      root.querySelector('#session-gap').innerHTML = gap(data);
      root.querySelector('#sessions-next').disabled = data.next_offset === null;
      root.querySelector('#session-count').textContent =
        `本页 ${data.sessions.length} 个会话 · 仅当前账号 / Space 可见`;
      root.querySelector('#sessions-sync').textContent =
        '已同步 ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
    } catch (err) {
      if (
        generation === epoch &&
        login === S.session &&
        root?.isConnected &&
        err.name !== 'AbortError'
      )
        root.querySelector('#sessions-sync').textContent = '更新失败，保留上次观测；稍后重试。';
    } finally {
      if (inflight === controller) inflight = null;
      if (generation === epoch) schedule();
    }
  }
  function bind() {
    detach();
    root = document.querySelector('#audit-sessions');
    if (!root) return;
    root.querySelector('#session-filter').onsubmit = (e) => {
      e.preventDefault();
      st().q = root.querySelector('#session-query').value.trim();
      st().state = root.querySelector('#session-state').value;
      st().project = root.querySelector('#session-project').value;
      st().tool = root.querySelector('#session-tool').value;
      st().offset = 0;
      renderPage(false);
    };
    root.querySelector('#sessions-refresh').onclick = () => refresh(false);
    root.querySelector('#sessions-live').onclick = (e) => {
      st().live = !st().live;
      e.target.textContent = st().live ? '暂停实时更新' : '恢复实时更新';
      e.target.setAttribute('aria-pressed', String(st().live));
      if (st().live) refresh(true);
      else clearTimeout(timer);
    };
    root.querySelector('#sessions-prev').onclick = () => {
      st().offset = Math.max(0, st().offset - 30);
      renderPage(false);
    };
    root.querySelector('#sessions-next').onclick = () => {
      st().offset += 30;
      renderPage(false);
    };
    schedule();
  }
  return { html, bind, refresh, detach, clear };
})();
