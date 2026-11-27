'use strict'
const test = require('node:test'), assert = require('node:assert/strict')
const {submissionURL, command} = require('./extension.cjs')
const env = {TEAM_APP_ORIGIN:'https://team.example/',TEAM_ID:'1',PROJECT_ID:'2',TASK_ID:'3'}
function fixture() {
  const opened=[], warnings=[]
  const vscode={workspace:{isTrusted:true,workspaceFolders:[{}],textDocuments:[{isDirty:false,getText(){throw Error('本文取得禁止')},save(){throw Error('保存禁止')}}]},
    window:{showWarningMessage:async value=>warnings.push(value),showInformationMessage:async()=> '変更を確認'},
    env:{openExternal:async url=>opened.push(url)},Uri:{parse:value=>value}}
  return {vscode,opened,warnings}
}
test('管理画面へIDだけを渡す',()=>assert.equal(submissionURL(env),'https://team.example/CodeSharing?team=1&project=2&task=3'))
test('通常環境のHTTPを拒否',()=>assert.throws(()=>submissionURL({...env,TEAM_APP_ORIGIN:'http://team.example/'})))
test('認証情報入りURLを拒否',()=>assert.throws(()=>submissionURL({...env,TEAM_APP_ORIGIN:'https://user:password@team.example/'})))
test('不正な対象IDを拒否',()=>assert.throws(()=>submissionURL({...env,TASK_ID:'../other'})))
test('明示確認後だけブラウザーを開く',async()=>{const f=fixture();await command(f.vscode,env)();assert.equal(f.opened.length,1);assert.equal(f.warnings.length,0)})
test('dirtyなら本文を読まず拒否',async()=>{const f=fixture();f.vscode.workspace.textDocuments[0].isDirty=true;await command(f.vscode,env)();assert.equal(f.opened.length,0)})
test('確認キャンセルで何もしない',async()=>{const f=fixture();f.vscode.window.showInformationMessage=async()=>undefined;await command(f.vscode,env)();assert.equal(f.opened.length,0)})
test('確認待ち中のdirty変化を拒否',async()=>{const f=fixture();f.vscode.window.showInformationMessage=async()=>{f.vscode.workspace.textDocuments[0].isDirty=true;return '変更を確認'};await command(f.vscode,env)();assert.equal(f.opened.length,0)})
test('未信頼workspaceを拒否',async()=>{const f=fixture();f.vscode.workspace.isTrusted=false;await command(f.vscode,env)();assert.equal(f.opened.length,0)})
test('複数workspaceを拒否',async()=>{const f=fixture();f.vscode.workspace.workspaceFolders.push({});await command(f.vscode,env)();assert.equal(f.opened.length,0)})
