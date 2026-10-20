'use strict';
window.CodePierProduct = (() => {
  async function resources(seq) {
    const tab = S.resourceTab || 'projects';
    let body = '';
    if (tab === 'projects') {
      await loadBasics();
      body = `<section class="panel"><div class="panel-head"><h2>Projects</h2><button class="btn primary" data-action="add-project">添加 Project</button></div><div class="panel-body">${CodePierSpaces.projectCollection(S.projects, { workspaceLabels: true })}</div></section>`;
    } else if (tab === 'devices') {
      await loadBasics();
      body = devicesHTML();
    } else if (tab === 'storage' || tab === 'vps') body = await CodePierSpaces.inventoryHTML(tab);
    else if (tab === 'mcp') body = await CodePierGateway.inventoryHTML();
    body = body.replace(/<h1(\s[^>]*)?>/g, '<h2$1>').replace(/<\/h1>/g, '</h2>');
    return `<div id="resources-page">${heading('资源库', 'RESOURCES', '登记基础资源；具体使用、映射和权限在工作空间配置。')}<nav class="product-tabs" aria-label="资源类别">${navigationTabs(
      'resources',
    )
      .map(
        ([id, title]) =>
          `<button class="btn ${id === tab ? 'primary' : 'ghost'}" data-nav="resources/${id}" aria-pressed="${id === tab}">${esc(title)}</button>`,
      )
      .join('')}</nav>${body}</div>`;
  }
  function bind() {
    if (['storage', 'vps'].includes(S.resourceTab)) CodePierSpaces.bind();
    if (S.resourceTab === 'mcp') CodePierGateway.bind();
    $$('[data-project-detail]', $('#page')).forEach(
      (b) =>
        (b.onclick = () => busy(b, () => CodePierSpaces.projectDetail(b.dataset.projectDetail))),
    );
  }
  async function resourceDetail(kind, id) {
    if (kind === 'project') return CodePierSpaces.projectDetail(id);
    const space = CodePierSpaces.route().id;
    if (!space) throw new Error('从明确的工作空间打开资源详情。');
    const out = await api(
      '/api/spaces/' +
        encodeURIComponent(space) +
        '/resources/' +
        kind +
        '/' +
        encodeURIComponent(id),
    );
    modal(
      '资源详情',
      `<pre class="code-box ws-json">${esc(JSON.stringify(out, null, 2))}</pre>`,
      undefined,
      true,
    );
  }
  document.addEventListener('click', (event) => {
    const b = event.target.closest('[data-resource-detail]');
    if (b && !b.disabled)
      void busy(b, () => resourceDetail(b.dataset.resourceType, b.dataset.resourceDetail));
  });
  return { resources, bind, resourceDetail };
})();
