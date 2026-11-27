/** 明示コマンドだけを登録するNode/workspace拡張入口。登録時に本文やファイルを読まない。 */
exports.activate = async function activate(context) {
  const vscode = require('vscode');
  const { registerProbe } = await import('./controller.mjs');
  registerProbe(vscode, context);
};

/** 追加の常駐処理・通信・保存状態を持たない。コマンドのdisposeはVS Codeが管理する。 */
exports.deactivate = function deactivate() {};
