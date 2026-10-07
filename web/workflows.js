'use strict';
// Compatibility for cached old entrypoints. All current reads use the archive.
async function workflowsHTML() {
  S.conversationTab = 'archive';
  return CodePierProduct.conversations();
}
function bindWorkflows() {
  CodePierProduct.bind();
}
