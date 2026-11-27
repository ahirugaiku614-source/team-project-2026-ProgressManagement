
const API_BASE_URL =
  import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';

// プロジェクト確認結果の一時保存
let cachedUserUid = null;
let cachedProjectStatus = null;

// API通信
async function apiRequest(url, token, options = {}) {
  const response = await fetch(`${API_BASE_URL}${url}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers
    }
  });

  if (!response.ok) {
    let message = 'サーバーとの通信に失敗しました。';

    try {
      const data = await response.json();
      message = data.detail || message;
    } catch {
      // エラー本文がJSONでない場合
    }

    throw new Error(message);
  }

  if (response.status === 204) {
    return null;
  }

  return response.json();
}

// ログインユーザーにプロジェクトが登録されているか確認
export async function checkUserHasProjects(user) {
  if (!user) {
    throw new Error('ログイン情報を確認できません。');
  }

  // 同じユーザーの確認結果があれば再利用
  if (
    cachedUserUid === user.uid &&
    cachedProjectStatus !== null
  ) {
    return cachedProjectStatus;
  }

  const token = await user.getIdToken();

  // PostgreSQLにユーザー情報を同期
  await apiRequest('/api/me', token);

  // 所属チーム一覧を取得
  const teams = await apiRequest('/api/teams', token);

  let hasProjects = false;

  // 各チームのプロジェクトを確認
  for (const team of teams) {
    const projects = await apiRequest(
      `/api/teams/${team.id}/projects`,
      token
    );

    if (projects.length > 0) {
      hasProjects = true;
      break;
    }
  }

  cachedUserUid = user.uid;
  cachedProjectStatus = hasProjects;

  return hasProjects;
}

// 登録後に確認結果をリセット
export function clearProjectStatusCache() {
  cachedUserUid = null;
  cachedProjectStatus = null;
}
