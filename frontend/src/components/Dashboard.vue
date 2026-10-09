<template>
  <div class="dashboard-page">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">T</span><span>TeamFlow</span></div>
      <p class="nav-label">ワークスペース</p>
      <button class="nav-item active"><span>▦</span> ダッシュボード</button>
      <button class="nav-item" @click="focusTasks"><span>☷</span> タスク管理</button>
      <div class="sidebar-bottom">
        <div class="user-chip">
          <div class="avatar">{{ userInitial }}</div>
          <div class="user-info"><strong>{{ displayName }}</strong><small>{{ user?.email }}</small></div>
        </div>
        <button class="logout-button" @click="signOutUser">ログアウト ↗</button>
      </div>
    </aside>

    <main class="main-content">
      <header class="topbar">
        <div class="breadcrumb">ワークスペース <span>/</span> <strong>ダッシュボード</strong></div>
        <div class="topbar-right"><span class="status-dot"></span> 接続済み <div class="avatar small">{{ userInitial }}</div></div>
      </header>

      <section class="welcome-row">
        <div>
          <p class="eyebrow">チームの進捗をひと目で</p>
          <h1>おかえりなさい、{{ displayName }}さん <span>👋</span></h1>
          <p class="subheading">今日もチームのタスクを整理して、プロジェクトを前に進めましょう。</p>
        </div>
        <button class="primary-button" @click="startCreate">＋ 新しいタスク</button>
      </section>

      <p v-if="apiMessage" class="notice" :class="{ warning: apiError }">{{ apiMessage }}</p>

      <section class="stats-grid" aria-label="タスクの概要">
        <article class="stat-card">
          <div class="stat-top"><span>すべてのタスク</span><span class="stat-icon purple">▦</span></div>
          <div class="stat-value">{{ tasks.length }}<small>件</small></div>
          <div class="stat-foot">登録されているタスク</div>
        </article>
        <article class="stat-card">
          <div class="stat-top"><span>未着手</span><span class="stat-icon blue">◷</span></div>
          <div class="stat-value">{{ countByStatus('TODO') }}<small>件</small></div>
          <div class="stat-foot">これから取り組むタスク</div>
        </article>
        <article class="stat-card">
          <div class="stat-top"><span>進行中</span><span class="stat-icon orange">↗</span></div>
          <div class="stat-value">{{ countByStatus('IN_PROGRESS') }}<small>件</small></div>
          <div class="stat-foot">現在進めているタスク</div>
        </article>
        <article class="stat-card">
          <div class="stat-top"><span>完了</span><span class="stat-icon green">✓</span></div>
          <div class="stat-value">{{ countByStatus('DONE') }}<small>件</small></div>
          <div class="stat-foot">完了したタスク</div>
        </article>
      </section>

      <section v-if="showForm" class="task-form-panel" id="task-form">
        <div class="section-heading">
          <div><h2>{{ editingTaskId ? 'タスクを編集' : '新しいタスクを作成' }}</h2><p>内容を入力して保存してください。</p></div>
          <button class="icon-button" aria-label="フォームを閉じる" @click="cancelForm">×</button>
        </div>
        <form class="task-form" @submit.prevent="saveTask">
          <label>タスク名<input v-model.trim="form.title" required maxlength="120" placeholder="例：ホーム画面のUIを作成する" /></label>
          <label>説明<textarea v-model.trim="form.description" rows="3" placeholder="タスクの目的や作業内容"></textarea></label>
          <label>ステータス
            <select v-model="form.status">
              <option value="TODO">未着手</option>
              <option value="IN_PROGRESS">進行中</option>
              <option value="DONE">完了</option>
            </select>
          </label>
          <div class="form-actions"><button type="button" class="secondary-button" @click="cancelForm">キャンセル</button><button type="submit" class="primary-button" :disabled="saving">{{ saving ? '保存中…' : '保存する' }}</button></div>
        </form>
      </section>

      <section class="board-section" id="task-board">
        <div class="section-heading board-heading">
          <div><h2>タスクボード</h2><p>チームの作業状況をステータス別に確認できます。</p></div>
          <span class="task-count">{{ tasks.length }} タスク</span>
        </div>
        <div class="kanban-board">
          <section v-for="column in columns" :key="column.key" class="kanban-column">
            <div class="column-heading">
              <span class="column-dot" :class="column.key.toLowerCase()"></span>
              <h3>{{ column.label }}</h3><span class="column-count">{{ tasksFor(column.key).length }}</span>
            </div>
            <div v-if="tasksFor(column.key).length" class="task-list">
              <article v-for="task in tasksFor(column.key)" :key="task.id" class="task-card">
                <div class="task-card-top"><span class="task-tag" :class="column.key.toLowerCase()">{{ column.label }}</span><button class="more-button" :aria-label="task.title + 'を編集'" @click="editTask(task)">•••</button></div>
                <h4>{{ task.title }}</h4>
                <p v-if="task.description" class="task-description">{{ task.description }}</p>
                <div class="task-card-footer">
                  <div class="assignee-avatar">{{ userInitial }}</div><span class="task-owner">自分のタスク</span>
                  <button class="edit-link" @click="editTask(task)">編集</button><button class="delete-link" @click="deleteTask(task)">削除</button>
                </div>
              </article>
            </div>
            <div v-else class="empty-column"><span class="empty-symbol">＋</span><p>タスクはありません</p><button @click="startCreate(column.key)">タスクを追加</button></div>
          </section>
        </div>
      </section>
      <footer class="page-footer">TeamFlow <span>•</span> チーム開発・進捗管理</footer>
    </main>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue';
import { signOut } from 'firebase/auth';
import { auth } from '../firebase';

const props = defineProps({ user: { type: Object, default: null } });
const emit = defineEmits(['logout']);
const tasks = ref([]);
const apiMessage = ref('');
const apiError = ref(false);
const showForm = ref(false);
const saving = ref(false);
const editingTaskId = ref(null);
const form = reactive({ title: '', description: '', status: 'TODO' });
const apiBase = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '');
const columns = [
  { key: 'TODO', label: '未着手' },
  { key: 'IN_PROGRESS', label: '進行中' },
  { key: 'DONE', label: '完了' },
];

const displayName = computed(() => props.user?.displayName || props.user?.email?.split('@')[0] || 'ユーザー');
const userInitial = computed(() => displayName.value.slice(0, 1).toUpperCase());

function normalizeStatus(status) {
  const value = String(status || 'TODO').toUpperCase();
  if (['IN_PROGRESS', 'INPROGRESS', 'IN PROGRESS'].includes(value)) return 'IN_PROGRESS';
  if (['DONE', 'COMPLETED', 'COMPLETE'].includes(value)) return 'DONE';
  return 'TODO';
}
function countByStatus(status) {
  return tasks.value.filter(task => normalizeStatus(task.status) === status).length;
}
function tasksFor(status) {
  return tasks.value.filter(task => normalizeStatus(task.status) === status);
}
async function request(path, options = {}) {
  const currentUser = auth.currentUser;
  if (!currentUser) throw new Error('ログイン状態を確認できません。もう一度ログインしてください。');
  const token = await currentUser.getIdToken();
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = '';
    try { detail = (await response.json()).detail || ''; } catch {}
    throw new Error(detail || `APIエラー (${response.status})`);
  }
  if (response.status === 204) return null;
  return response.json();
}
async function loadTasks() {
  apiMessage.value = '';
  apiError.value = false;
  try {
    // API呼び出しでユーザーを同期してからタスク一覧を取得
    await request('/api/me');
    tasks.value = (await request('/api/tasks')).map(task => ({ ...task, status: normalizeStatus(task.status) }));
  } catch (error) {
    apiError.value = true;
    apiMessage.value = `タスクを読み込めませんでした。バックエンドの起動状態とAPI設定を確認してください。（${error.message}）`;
  }
}
function resetForm() {
  form.title = '';
  form.description = '';
  form.status = 'TODO';
  editingTaskId.value = null;
}
function startCreate(status = 'TODO') {
  resetForm();
  form.status = status;
  showForm.value = true;
  requestAnimationFrame(() => document.getElementById('task-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
}
function editTask(task) {
  editingTaskId.value = task.id;
  form.title = task.title || '';
  form.description = task.description || '';
  form.status = normalizeStatus(task.status);
  showForm.value = true;
  requestAnimationFrame(() => document.getElementById('task-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' }));
}
function cancelForm() {
  showForm.value = false;
  resetForm();
}
async function saveTask() {
  if (!form.title || saving.value) return;
  saving.value = true;
  try {
    const payload = { title: form.title, description: form.description || null, status: form.status };
    if (editingTaskId.value !== null) {
      await request(`/api/tasks/${editingTaskId.value}`, { method: 'PUT', body: JSON.stringify(payload) });
    } else {
      await request('/api/tasks', { method: 'POST', body: JSON.stringify(payload) });
    }
    cancelForm();
    await loadTasks();
  } catch (error) {
    apiError.value = true;
    apiMessage.value = `保存できませんでした。(${error.message})`;
  } finally {
    saving.value = false;
  }
}
async function deleteTask(task) {
  if (!window.confirm(`「${task.title}」を削除しますか？`)) return;
  try {
    await request(`/api/tasks/${task.id}`, { method: 'DELETE' });
    await loadTasks();
  } catch (error) {
    apiError.value = true;
    apiMessage.value = `削除できませんでした。(${error.message})`;
  }
}
async function focusTasks() {
  document.getElementById('task-board')?.scrollIntoView({ behavior: 'smooth' });
}
async function signOutUser() {
  await signOut(auth);
  emit('logout');
}
onMounted(loadTasks);
</script>

<style scoped>
.dashboard-page{display:flex;min-height:100vh;width:100%;background:#f7f8fc;color:#20243a;text-align:left;font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;line-height:1.5}
.sidebar{width:230px;flex-shrink:0;background:#fff;border-right:1px solid #e9eaf2;padding:26px 16px;display:flex;flex-direction:column;box-sizing:border-box}
.brand{display:flex;align-items:center;gap:10px;padding:0 10px;margin-bottom:46px;font-size:19px;font-weight:800;letter-spacing:-.5px;color:#20243a}
.brand-mark{display:grid;place-items:center;width:32px;height:32px;background:#6558e8;color:#fff;border-radius:10px;font-weight:800;box-shadow:0 5px 12px #6558e833}
.nav-label{font-size:11px;color:#9a9db0;text-transform:uppercase;letter-spacing:1px;font-weight:700;padding:0 12px;margin:0 0 10px}
.nav-item{width:100%;border:0;background:transparent;color:#777b91;padding:12px;border-radius:9px;text-align:left;font:inherit;cursor:pointer;margin:2px 0}
.nav-item span{display:inline-block;width:27px;font-size:17px}
.nav-item.active{background:#f0efff;color:#5e52d8;font-weight:700}
.sidebar-bottom{margin-top:auto}
.user-chip{display:flex;align-items:center;gap:10px;padding:14px 7px;border-top:1px solid #eff0f5}
.avatar{display:grid;place-items:center;flex-shrink:0;width:35px;height:35px;border-radius:50%;background:#e9e6ff;color:#5b4ed0;font-weight:800}
.avatar.small{width:29px;height:29px;font-size:12px}
.user-info{min-width:0;display:flex;flex-direction:column}
.user-info strong{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.user-info small{font-size:10px;color:#9295a8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.logout-button{width:100%;background:#fff;border:1px solid #e8e9f0;border-radius:8px;padding:9px;color:#777b91;font:inherit;font-size:12px;cursor:pointer}
.main-content{min-width:0;flex:1;padding:0 36px 24px;max-width:1600px;margin:0 auto}
.topbar{height:66px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e9eaf2;color:#9295a8;font-size:12px}
.breadcrumb span{margin:0 10px;color:#c5c7d2}
.breadcrumb strong{color:#454961;font-weight:600}
.topbar-right{display:flex;align-items:center;gap:9px}
.status-dot{width:7px;height:7px;background:#32bd85;border-radius:50%}
.welcome-row{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:32px 0 25px}
.eyebrow{font-size:11px;color:#7669e9;font-weight:800;letter-spacing:.7px;margin:0 0 7px}
h1{font-size:25px;line-height:1.35;letter-spacing:-.8px;color:#242840;font-weight:750;margin:0 0 7px}
h1 span{font-size:22px}
.subheading{color:#8a8da1;font-size:12px;margin:0}
.primary-button{border:0;background:#6558e8;color:white;border-radius:8px;padding:11px 16px;font:inherit;font-size:12px;font-weight:700;white-space:nowrap;cursor:pointer;box-shadow:0 4px 10px #6558e82b}
.primary-button:hover{background:#5447d6}
.primary-button:disabled{opacity:.6;cursor:wait}
.notice{padding:12px 14px;background:#edfbf5;border:1px solid #c8f0df;border-radius:8px;color:#237d5a;font-size:12px;margin:0 0 18px;overflow-wrap:anywhere}
.notice.warning{background:#fff8eb;border-color:#f4dfb6;color:#8c6118}
.stats-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin-bottom:32px}
.stat-card{padding:18px 18px 16px;background:#fff;border:1px solid #ececf4;border-radius:12px;box-shadow:0 3px 10px #24284004}
.stat-top{display:flex;justify-content:space-between;align-items:center;color:#83869b;font-size:12px;font-weight:600}
.stat-icon{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;font-size:16px;font-weight:800}
.stat-icon.purple{background:#f0edff;color:#6b5ce7}.stat-icon.blue{background:#eaf4ff;color:#3684db}.stat-icon.orange{background:#fff2e7;color:#e99549}.stat-icon.green{background:#e8faf1;color:#24a875}
.stat-value{font-size:30px;font-weight:800;letter-spacing:-1px;margin:10px 0 2px;color:#282b43}
.stat-value small{font-size:12px;font-weight:500;letter-spacing:0;margin-left:5px;color:#888ba0}
.stat-foot{font-size:10px;color:#a0a2b3}
.task-form-panel{background:#fff;border:1px solid #e9e9f2;border-radius:12px;padding:20px;margin:0 0 28px;box-shadow:0 5px 18px #292d4408}
.section-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:18px}
.section-heading h2{font-size:17px;letter-spacing:-.3px;margin:0 0 4px;color:#292c43;font-weight:750}
.section-heading p{font-size:11px;color:#999bad;margin:0}
.icon-button{border:0;background:#f4f4f9;border-radius:7px;width:30px;height:30px;font-size:21px;color:#7c8095;cursor:pointer}
.task-form{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.task-form label{display:flex;flex-direction:column;gap:6px;font-size:11px;font-weight:700;color:#666a81}
.task-form label:nth-child(2){grid-column:1/-1}
.task-form input,.task-form textarea,.task-form select{width:100%;box-sizing:border-box;border:1px solid #e0e2ed;border-radius:7px;padding:10px 11px;font:inherit;font-size:12px;color:#32364e;background:#fff;outline:none}
.task-form input:focus,.task-form textarea:focus,.task-form select:focus{border-color:#867aef;box-shadow:0 0 0 3px #867aef1c}
.task-form textarea{resize:vertical}
.form-actions{grid-column:1/-1;display:flex;justify-content:flex-end;gap:8px}
.secondary-button{border:1px solid #e0e2ed;background:#fff;color:#777b91;border-radius:8px;padding:10px 14px;font:inherit;font-size:12px;cursor:pointer}
.board-section{margin-bottom:28px}
.board-heading{margin-bottom:14px}
.task-count{background:#efeff7;color:#7e8197;border-radius:6px;padding:5px 9px;font-size:10px;font-weight:700;white-space:nowrap}
.kanban-board{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;align-items:start}
.kanban-column{background:#f0f1f7;border-radius:11px;padding:12px;min-width:0;min-height:250px}
.column-heading{display:flex;align-items:center;gap:8px;padding:4px 3px 13px}
.column-heading h3{font-size:12px;font-weight:750;color:#555970;margin:0}
.column-dot{width:8px;height:8px;border-radius:50%}
.column-dot.todo{background:#7e8ba5}.column-dot.in_progress{background:#eaa24d}.column-dot.done{background:#2cb783}
.column-count{margin-left:auto;background:#e2e4ef;border-radius:5px;padding:2px 7px;font-size:10px;color:#777b91;font-weight:700}
.task-list{display:flex;flex-direction:column;gap:10px}
.task-card{background:#fff;border:1px solid #e8e9f1;border-radius:9px;padding:13px 12px;box-shadow:0 2px 5px #272b4205;min-width:0}
.task-card-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.task-tag{font-size:9px;font-weight:700;border-radius:4px;padding:4px 7px}
.task-tag.todo{color:#6e7892;background:#edf0f7}.task-tag.in_progress{color:#b8782f;background:#fff1df}.task-tag.done{color:#21855e;background:#e5f8ee}
.more-button{border:0;background:transparent;color:#a5a7b7;font-size:13px;letter-spacing:1px;cursor:pointer}
.task-card h4{font-size:12px;line-height:1.6;font-weight:750;color:#33374f;margin:0 0 6px;overflow-wrap:anywhere}
.task-description{font-size:10px;line-height:1.6;color:#8b8ea2;margin:0 0 12px;white-space:pre-wrap;overflow-wrap:anywhere}
.task-card-footer{display:flex;align-items:center;gap:7px;border-top:1px solid #f0f0f6;padding-top:10px;margin-top:12px}
.assignee-avatar{display:grid;place-items:center;width:23px;height:23px;background:#eeecff;border-radius:50%;color:#6659df;font-size:10px;font-weight:800}
.task-owner{font-size:9px;color:#a0a2b1}
.edit-link,.delete-link{border:0;background:transparent;font-size:10px;font-weight:700;cursor:pointer;padding:3px}.edit-link{margin-left:auto;color:#6558e8}.delete-link{color:#d45c69}
.empty-column{min-height:160px;display:flex;align-items:center;justify-content:center;flex-direction:column;color:#a0a3b4;text-align:center}
.empty-symbol{font-size:23px;color:#c0c2d0}
.empty-column p{font-size:10px;margin:5px 0 8px}
.empty-column button{border:0;background:transparent;color:#7569e7;font:inherit;font-size:10px;cursor:pointer}
.page-footer{border-top:1px solid #e9eaf2;padding-top:18px;color:#a2a4b4;font-size:10px}
.page-footer span{margin:0 6px}
@media(max-width:1100px){.sidebar{width:190px}.main-content{padding:0 22px 24px}.stats-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.kanban-board{gap:9px}.kanban-column{padding:9px}}
@media(max-width:760px){.sidebar{width:58px;padding:18px 8px}.brand{padding:0;justify-content:center;margin-bottom:35px}.brand>span:last-child,.nav-label,.nav-item:not(.active),.nav-item.active{font-size:0}.nav-item{padding:11px 8px;text-align:center}.nav-item span{width:auto;font-size:18px}.user-chip{justify-content:center;padding:12px 0}.user-info,.logout-button{display:none}.main-content{padding:0 14px 20px}.welcome-row{align-items:flex-start;flex-direction:column;padding:24px 0 20px}h1{font-size:22px}.stats-grid{gap:9px}.stat-card{padding:13px}.stat-value{font-size:26px}.kanban-board{grid-template-columns:1fr}.kanban-column{min-height:120px}.task-form{grid-template-columns:1fr}.task-form label:nth-child(2){grid-column:auto}.form-actions{grid-column:auto}.topbar{height:55px}.breadcrumb{font-size:10px}.topbar-right{font-size:0}}
</style>
