import { auth } from '../firebase'
import { apiRequest } from './api'

// 認証状態や登録状況の変更を検出するための世代番号
let statusVersion = 0

// 確認処理が現在も有効か確認する
function assertCurrentUser(user, version) {
  if (
    auth.currentUser?.uid !== user.uid ||
    version !== statusVersion
  ) {
    throw new Error('ログイン状態または登録状況が変更されました。')
  }
}

// APIから取得した一覧データを確認する
function validateList(data, label) {
  if (!Array.isArray(data)) {
    throw new Error(`${label}の取得データが不正です。`)
  }

  for (const item of data) {
    if (!item || !Number.isSafeInteger(item.id) || item.id <= 0) {
      throw new Error(`${label}のIDが不正です。`)
    }
  }

  return data
}

// ログインユーザーの所属チームを取得する
export async function getUserTeams(user = auth.currentUser) {
  if (!user) {
    throw new Error('ログイン情報を確認できません。')
  }

  // PostgreSQLにユーザー情報を同期
  await apiRequest('/api/me', {}, user)

  const teams = await apiRequest('/api/teams', {}, user)

  return validateList(teams, 'チーム一覧')
}

// 指定したチームのプロジェクト一覧を取得する
export async function getTeamProjects(
  teamId,
  user = auth.currentUser
) {
  if (!Number.isSafeInteger(teamId) || teamId <= 0) {
    throw new Error('チームIDが不正です。')
  }

  const projects = await apiRequest(
    `/api/teams/${teamId}/projects`,
    {},
    user
  )

  return validateList(projects, 'プロジェクト一覧')
}

// ログインユーザーにプロジェクトが登録されているか確認
export async function checkUserHasProjects(user) {
  if (!user) {
    throw new Error('ログイン情報を確認できません。')
  }

  const version = statusVersion

  assertCurrentUser(user, version)

  // 所属チーム一覧を取得
  const teams = await getUserTeams(user)

  assertCurrentUser(user, version)

  // 各チームのプロジェクトを確認
  for (const team of teams) {
    const projects = await getTeamProjects(team.id, user)

    assertCurrentUser(user, version)

    if (projects.length > 0) {
      return true
    }
  }

  assertCurrentUser(user, version)

  // 正常に確認でき、プロジェクトが存在しない場合のみfalse
  return false
}

// 既存の呼び出し元との互換性を維持する
// 古い確認処理の結果を無効化する
export function clearProjectStatusCache() {
  statusVersion += 1
}