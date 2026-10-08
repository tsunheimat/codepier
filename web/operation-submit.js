'use strict';
// Receipt handling for ordinary file/search operations, independent of archives.
function bindOperationSubmit(dialog, form, button, name, readArgs, done) {
  let pending = null,
    receipt = null;
  const session = S.session,
    space = S.space_id;
  const controls = () => $$('input,textarea,select', form);
  const submit = () =>
    busy(button, async () => {
      if (session !== S.session || space !== S.space_id || !dialog.isConnected) return;
      if (!pending) {
        if (!form.reportValidity()) return;
        pending = { ...readArgs(), idempotency_key: 'panel-operation-' + uid() };
        controls().forEach((el) => (el.disabled = true));
        CodePierIntegrations.rememberOperation(name, pending);
      }
      try {
        receipt = receipt?.operation_id ? receipt : await tool(name, pending);
        CodePierIntegrations.rememberOperation(name, pending, receipt);
        const result = await settled(Promise.resolve(receipt));
        if (session !== S.session || space !== S.space_id) return;
        CodePierIntegrations.forgetOperation(pending.idempotency_key);
        if (dialog.isConnected) await done(result);
      } catch (error) {
        if (session !== S.session || space !== S.space_id) return;
        if (error.operation_id) {
          receipt = { pending: true, operation_id: error.operation_id };
          CodePierIntegrations.rememberOperation(name, pending, receipt);
        }
        const rejected =
          !receipt?.operation_id &&
          error.status >= 400 &&
          error.status < 500 &&
          ![408, 429].includes(error.status);
        if (rejected) {
          CodePierIntegrations.forgetOperation(pending.idempotency_key);
          pending = null;
          controls().forEach((el) => (el.disabled = false));
        }
        const hint = $('[data-operation-hint]', form);
        if (hint && dialog.isConnected)
          hint.textContent = rejected
            ? error.message
            : '原操作回执待核实；再次点击只恢复同一请求。离开后可在该项目 Development Tools 查询原结果。';
        throw error;
      }
    });
  button.onclick = submit;
  form.onsubmit = (e) => {
    e.preventDefault();
    submit();
  };
}
