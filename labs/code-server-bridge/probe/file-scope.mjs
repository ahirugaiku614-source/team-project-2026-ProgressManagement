/** 実本文を読まず、Linux上の専用rootと対象ファイルのメタデータだけを検査する。 */
import * as filesystem from 'node:fs/promises';
import { constants } from 'node:fs';
import path from 'node:path';
import { BridgeRejected } from '../capture.mjs';

/** 大きなinode等の数値丸めを同一性の証拠にしない。表現できない値なら拒否する。 */
function validMetadata(info) {
  return ['dev', 'ino', 'uid', 'mode', 'nlink'].every(key => Number.isSafeInteger(info[key]) && info[key] >= 0);
}

/** inode/所有者/権限/リンク数を比較する。mtimeだけでファイルの同一性を推測しない。 */
function sameFile(a, b) {
  return validMetadata(a) && validMetadata(b) && ['dev', 'ino', 'uid', 'mode', 'nlink'].every(key => a[key] === b[key]);
}

/** 絶対POSIXパスを厳格に検査し、root全体や字句上の迂回を許可しない。 */
function validPath(value) {
  return typeof value === 'string' && value.startsWith('/') && value !== '/' &&
    !value.includes('\\') && !value.includes('\0') && path.posix.normalize(value) === value &&
    value.split('/').slice(1).every(part => part && part !== '.' && part !== '..');
}

/**
 * 本人UIDの通常ファイルだけをO_NOFOLLOWで開く。本文read/writeはせず、FDを保持する。
 * 同時にパスを変更する攻撃の完全な防御や、既存バッファの出自証明ではない。
 * io/flags注入はモック試験用で、実拡張ではOS標準だけを使う。
 */
export async function approveFile({ rootPath, filePath, uid, platform }, io = filesystem, flags = constants) {
  let handle;
  try {
    if (platform !== 'linux' || !Number.isSafeInteger(uid) || uid <= 0 ||
        !validPath(rootPath) || !validPath(filePath) || !filePath.startsWith(rootPath + '/') ||
        !Number.isInteger(flags.O_NOFOLLOW) || !Number.isInteger(flags.O_NONBLOCK)) throw new BridgeRejected();

    /** 祖先を含めsymlinkを拒否し、専用root以下は本人だけが書けるディレクトリに限定する。 */
    async function inspectPath() {
      let current = '';
      const directories = [];
      let rootDevice;
      for (const part of filePath.split('/').slice(1, -1)) {
        current += '/' + part;
        const info = await io.lstat(current);
        const inRoot = current === rootPath || current.startsWith(rootPath + '/');
        if (!validMetadata(info) || info.isSymbolicLink() || !info.isDirectory() || (info.mode & 0o022) !== 0 ||
            (inRoot ? info.uid !== uid || (info.mode & 0o077) !== 0 : ![0, uid].includes(info.uid))) throw new BridgeRejected();
        if (current === rootPath) rootDevice = info.dev;
        if (inRoot && info.dev !== rootDevice) throw new BridgeRejected();
        directories.push(info);
      }
      if (await io.realpath(rootPath) !== rootPath || await io.realpath(filePath) !== filePath) throw new BridgeRejected();
      const info = await io.lstat(filePath);
      if (!validMetadata(info) || info.isSymbolicLink() || !info.isFile() || info.nlink !== 1 || info.uid !== uid ||
          (info.mode & 0o022) !== 0 || info.dev !== rootDevice) throw new BridgeRejected();
      return { file: info, directories };
    }

    const before = await inspectPath();
    handle = await io.open(filePath, flags.O_RDONLY | flags.O_NOFOLLOW | flags.O_NONBLOCK);
    const opened = await handle.stat();
    if (!sameFile(before.file, opened) || !opened.isFile()) throw new BridgeRejected();
    return {
      /** getText後も、FDと実体パスが同じ通常ファイルを指すか再検査する。 */
      async verify() {
        try {
          const current = await inspectPath();
          if (!sameFile(opened, current.file) || !sameFile(opened, await handle.stat()) ||
              current.directories.length !== before.directories.length ||
              current.directories.some((info, index) => !sameFile(info, before.directories[index]))) throw new BridgeRejected();
        } catch { throw new BridgeRejected(); }
      },
      /** 開いたFDだけを閉じる。ファイル・workspace・ディスクの削除は行わない。 */
      async close() {
        try { await handle.close(); } catch { throw new BridgeRejected(); }
      },
    };
  } catch {
    if (handle) { try { await handle.close(); } catch {} }
    throw new BridgeRejected();
  }
}
