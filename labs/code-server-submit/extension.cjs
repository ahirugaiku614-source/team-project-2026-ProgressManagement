'use strict'

// この拡張は操作の入口であり、保存状態の認証局ではない。本文・トークン・
// ハッシュは読まない。利用者が拡張を書き換えても、管理側の収集認可は変わらない。
function submissionURL(env) {
  const url = new URL(env.TEAM_APP_ORIGIN || '')
  const demo = env.TEAM_SYNTHETIC_DEMO === '1' && url.hostname === '127.0.0.1'
  if ((!demo && url.protocol !== 'https:') || !['https:', 'http:'].includes(url.protocol)
      || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('接続先を確認できません')
  for (const key of ['TEAM_ID', 'PROJECT_ID', 'TASK_ID']) {
    if (!/^[1-9]\d*$/.test(env[key] || '') || !Number.isSafeInteger(Number(env[key]))) throw new Error('作業対象を確認できません')
  }
  url.pathname = '/CodeSharing'
  for (const [name, key] of [['team', 'TEAM_ID'], ['project', 'PROJECT_ID'], ['task', 'TASK_ID']]) url.searchParams.set(name, env[key])
  return url.toString()
}

function command(vscode, env) {
  return async () => {
    if (!vscode.workspace.isTrusted || vscode.workspace.workspaceFolders?.length !== 1) {
      await vscode.window.showWarningMessage('信頼済みの単一作業環境で操作してください。'); return
    }
    // 方式Cではローカルの未保存状態は注意喚起だけに使用する。
    // getText・saveは呼ばず、管理画面で固定した保存済み内容への同意を得る。
    if (vscode.workspace.textDocuments.some(document => document.isDirty)) {
      await vscode.window.showWarningMessage('未保存の変更は提出されません。含めたい変更は先に保存してください。')
    }
    let url
    try { url = submissionURL(env) } catch {
      await vscode.window.showWarningMessage('管理側の接続設定を確認できません。'); return
    }
    const answer = await vscode.window.showInformationMessage(
      '保存済みの内容を管理画面で固定し、差分を確認してから提出します。未保存の変更は含めません。ここでは本文取得・保存・提出を行いません。', {modal: true}, '変更を確認')
    if (answer !== '変更を確認') return
    await vscode.env.openExternal(vscode.Uri.parse(url))
  }
}

function activate(context) {
  const vscode = require('vscode')
  context.subscriptions.push(vscode.commands.registerCommand('teamChanges.submit', command(vscode, process.env)))
  const item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left)
  item.text = '$(git-pull-request) 変更を提出'; item.command = 'teamChanges.submit'; item.show()
  context.subscriptions.push(item)
}
module.exports = {activate, submissionURL, command}
