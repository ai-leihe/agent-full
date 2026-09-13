/* ============================================================
   Agent 平台工作台前端
   结构：工具 → HTTP → 状态 → 组件 → 路由 → 六个视图 → 事件 → 启动
   约定：所有变更操作完成后刷新受影响的 Store，再重新渲染当前视图。
   ============================================================ */

/* ------------------------------------------------------------ 工具 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

const num = (n) => Number(n ?? 0).toLocaleString('zh-CN');

function fmtTime(seconds) {
  if (!seconds) return '-';
  const d = new Date(seconds * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return '-';
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

const fmtMs = (ms) => (ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Number(ms ?? 0).toFixed(1)} ms`);

/** 字节数 → 可读大小（用于沙盒上传文件的体积展示）。 */
const fmtBytes = (n) => {
  const size = Number(n ?? 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(2)} MB`;
};

/* ------------------------------------------------------------ HTTP */

async function request(path, options = {}) {
  const { method = 'GET', body, form, auth = true } = options;
  const init = { method, headers: {} };
  if (auth && Auth.token) init.headers['Authorization'] = `Bearer ${Auth.token}`;
  if (form) {
    init.body = form;
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  const res = await fetch(path, init);
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { detail: text }; }

  if (!res.ok) {
    const detail = data && data.detail;
    const message = typeof detail === 'string' ? detail : (detail && detail.message) || `HTTP ${res.status}`;
    const error = new Error(message);
    error.status = res.status;
    error.detail = detail;
    // 令牌失效：清掉本地会话并回到登录门（登录/注册自身的 401 不在此列）
    if (res.status === 401 && auth && !options.silent401) onSessionExpired();
    throw error;
  }
  return data;
}

const GET = (p, opts) => request(p, opts);
const POST = (p, body, opts) => request(p, { method: 'POST', body: body || {}, ...opts });
const PUT = (p, body, opts) => request(p, { method: 'PUT', body, ...opts });
const PATCH = (p, body) => request(p, { method: 'PATCH', body });
const DEL = (p) => request(p, { method: 'DELETE' });
const UPLOAD = (p, form) => request(p, { method: 'POST', form });

/* ------------------------------------------------------------ 全局状态 */

const S = {
  overview: null,
  orch: null,
  skills: null,
  runs: null,
  knowledge: null,
  settings: null,
  users: null,
  pipelines: null,         // 流水线任务分页列表
  ui: {
    openParams: new Set(),
    addStepFor: null,
    selectedAgent: null,
    flowPlayed: false,       // 进入编排页的入场动画只播一次
    flowPlaying: false,      // 是否正在演示执行顺序
    flowTimes: null,         // 阶段 → 最近一次试跑的真实耗时（ms）
    runsFilter: { status: '', keyword: '' },
    activeRun: null,
    testSchema: null,        // 沙盒试跑：当前插件的 param_schema，按类型取值
    testFile: null,          // 沙盒试跑：在线文件验证选中的文件（name/content_type/data_base64/bytes）
    scriptRuntimes: [],      // 脚本验证：本机可用的解释器清单
    providerDraft: null,
    editingUser: '',
    profileDraft: null,
    sessions: null,
    pipelinePage: 1,         // 流水线任务列表当前页码
    listPage: { runs: 1, knowledge: 1, skills: 1 },   // 各列表页当前页码（每页 10 条）
  },
};

const PAGE_SIZE = 10;      // 运行记录 / 知识库 / 技能库 / 编排工作台 统一每页条数

/* ------------------------------------------------------------ 认证 / 主题 */

const TOKEN_KEY = 'agent-platform.token';
const THEME_KEY = 'agent-platform.theme';

/** 登录态：token 存 localStorage，user 为服务端返回的公开视图 */
const Auth = { token: '', user: null, config: null, started: false };

const DEFAULT_THEME = { mode: 'dark', accent: 'blue', radius: 'standard', density: 'standard', glass: 'on' };
const ACCENT_COLORS = {
  blue: '#5b8cff', violet: '#9b6bff', cyan: '#17a8bd',
  emerald: '#17a374', amber: '#d9860f', rose: '#ef4f7b',
};
const AVATAR_CHOICES = ['🛡️', '🚀', '🧠', '🦊', '🐼', '🐳', '🌱', '⚡', '🎯', '🔭', '🧩', '🪐'];
const COLOR_CHOICES = ['#5b8cff', '#9b6bff', '#17a8bd', '#17a374', '#d9860f', '#ef4f7b', '#64748b', '#e0a458'];

let theme = { ...DEFAULT_THEME };

function loadLocalTheme() {
  try { return { ...DEFAULT_THEME, ...(JSON.parse(localStorage.getItem(THEME_KEY) || '{}') || {}) }; }
  catch { return { ...DEFAULT_THEME }; }
}

function prefersLight() {
  return window.matchMedia('(prefers-color-scheme: light)').matches;
}

/** 主题全部落在 <html> 的 data-* 上；mode=auto 时解析为实际深浅。 */
function applyTheme(patch = {}, { persist = true } = {}) {
  theme = { ...theme, ...(patch || {}) };
  const root = document.documentElement;
  root.dataset.mode = theme.mode === 'auto' ? (prefersLight() ? 'light' : 'dark') : theme.mode;
  root.dataset.modePref = theme.mode;
  root.dataset.accent = theme.accent;
  root.dataset.radius = theme.radius;
  root.dataset.density = theme.density;
  root.dataset.glass = theme.glass;
  if (persist) localStorage.setItem(THEME_KEY, JSON.stringify(theme));
}

let themeSaveTimer = null;

/** 主题是账号偏好：登录后防抖同步到服务端，失败也不影响本地观感。 */
function persistTheme() {
  if (!Auth.user) return;
  clearTimeout(themeSaveTimer);
  themeSaveTimer = setTimeout(async () => {
    try {
      const data = await PUT('/api/auth/preferences', { theme });
      Auth.user = data.user;
    } catch { /* 静默失败 */ }
  }, 500);
}

/** 统一的主题修改入口：即时生效 → 本地留存 → 账号同步 → 刷新主题页预览。 */
function setTheme(patch) {
  applyTheme(patch);
  persistTheme();
  if (currentRoute === '#/theme') $('#view').innerHTML = viewTheme();
}

function refreshAuthConfig() {
  return GET('/api/auth/config', { auth: false })
    .then((cfg) => { Auth.config = cfg; return cfg; })
    .catch(() => Auth.config || null);
}

function clearSession() {
  Auth.token = '';
  Auth.user = null;
  localStorage.removeItem(TOKEN_KEY);
}

/** 请求返回 401 时的兜底：吊销服务端会话（含 Cookie）→ 清本地会话 → 回到登录门。 */
function onSessionExpired() {
  if (!Auth.token && !Auth.user) return;
  // 会话 Cookie 由服务端下发，必须调一次登出接口才能一起失效（silent401 避免递归）
  POST('/api/auth/logout', {}, { silent401: true }).catch(() => {});
  clearSession();
  renderChrome();
  if (Auth.config && Auth.config.require_login === false) return;
  showGate('会话已过期，请重新登录');
}

/**
 * 文档页（/docs 等）被服务端带回工作台时的提示：
 * ?auth=required（未登录）或 ?auth=forbidden（账号角色不足）。
 */
function popAuthRequiredNotice() {
  const params = new URLSearchParams(location.search || '');
  const reason = params.get('auth');
  if (reason !== 'required' && reason !== 'forbidden') return '';
  params.delete('auth');
  const query = params.toString();
  history.replaceState(null, '', (location.pathname || '/') + (query ? `?${query}` : '') + (location.hash || ''));
  return reason === 'forbidden'
    ? '当前账号无权查看接口文档：平台已限制为「仅管理员」可见，请联系管理员调整设置。'
    : '接口文档需要登录后查看：请先登录，登录后打开文档页会自动带上当前账号的会话。';
}

/** 当前账号能否打开接口文档：与后端 auth.docs_access（member/admin/public）保持一致。 */
function canViewDocs() {
  const cfg = Auth.config || {};
  if (cfg.require_login === false) return true;                     // 登录校验关闭：文档一并开放
  const access = cfg.docs_access || 'member';
  if (access === 'public') return true;                             // 匿名公开
  if (!Auth.user) return false;                                     // 需要登录
  return access !== 'admin' || Auth.user.role === 'admin';          // 仅管理员
}

function onAuthenticated() {
  hideGate();
  renderChrome();
  if (!Auth.started) startApp();
  else refreshOverview().catch(() => {}).then(() => route());
}

/** 登录后的正式启动：拉取平台状态、绑定路由与拖拽监听。 */
async function startApp() {
  Auth.started = true;
  const serverTheme = Auth.user && Auth.user.preferences && Auth.user.preferences.theme;
  if (serverTheme) applyTheme(serverTheme);                       // 账号偏好优先
  else if (Auth.user) applyTheme({}, { persist: false });         // 保持本地观感但不上报
  renderChrome();
  try { await refreshOverview(); }
  catch (err) { toast(`平台状态加载失败：${err.message}`, 'bad'); }
  await route();
  window.addEventListener('hashchange', route);
  const observer = new MutationObserver(() => bindDropZone());
  observer.observe($('#view'), { childList: true });
}

async function doLogout() {
  try { await POST('/api/auth/logout'); } catch { /* 令牌可能已失效 */ }
  clearSession();
  closeUserMenu();
  renderChrome();
  location.hash = '#/overview';
  if (Auth.config && Auth.config.require_login === false) {
    await refreshOverview().catch(() => {});
    await route();
  } else {
    showGate('已安全退出');
  }
}

/* ------------------------------------------------ 登录门 */

function needsGate() {
  return !!(Auth.config && Auth.config.require_login !== false && !Auth.user);
}

function showGate(message = '') {
  const gate = $('#gate');
  if (!gate) return;
  renderGate(message);
  gate.classList.remove('hidden');
}

function hideGate() {
  const gate = $('#gate');
  if (!gate) return;
  gate.classList.add('hidden');
  gate.innerHTML = '';
}

function renderGate(message = '') {
  const gate = $('#gate');
  if (!gate) return;
  const cfg = Auth.config || {};
  const allowReg = cfg.allow_registration !== false;
  const tab = gate.dataset.tab === 'register' && allowReg ? 'register' : 'login';
  gate.dataset.tab = tab;
  const name = (S.overview && S.overview.platform && S.overview.platform.name) || 'Agent 平台';

  gate.innerHTML = `
    <div class="gate-card">
      <aside class="gate-aside">
        <div class="brand-lg">
          <div class="logo-lg">A</div>
          <div>
            <h2>${esc(name)}</h2>
            <div class="lead">多 Agent 串联 · 插拔式 Skill</div>
          </div>
        </div>
        <p class="lead">登录后即可编排流水线、管理技能与检索知识库；主题外观随账号同步。</p>
        <div class="gate-feats">
          <div class="gate-feat"><span class="dot"></span><span>可视化编排：阶段 / 步骤 / 契约诊断一站完成</span></div>
          <div class="gate-feat"><span class="dot"></span><span>插拔式技能：外部插件热加载，随时启停</span></div>
          <div class="gate-feat"><span class="dot"></span><span>知识库检索：切片、向量化与相似度验证</span></div>
          <div class="gate-feat"><span class="dot"></span><span>个人中心与主题：配色 / 深浅 / 密度自由组合</span></div>
        </div>
      </aside>
      <div class="gate-main">
        <div class="gate-tabs">
          <button type="button" data-act="gate-tab" data-tab="login" class="${tab === 'login' ? 'active' : ''}">登录</button>
          ${allowReg ? `<button type="button" data-act="gate-tab" data-tab="register" class="${tab === 'register' ? 'active' : ''}">注册</button>` : ''}
        </div>
        <form id="gateForm" autocomplete="off">
          ${gateFields(tab)}
          <button class="btn primary" type="submit" style="width:100%;justify-content:center">
            ${tab === 'login' ? '登录' : '创建账号并登录'}
          </button>
        </form>
        <div class="gate-error ${message ? '' : 'hidden'}" id="gateError">${esc(message)}</div>
        ${cfg.first_run_hint && tab === 'login'
          ? '<div class="gate-hint">检测到默认管理员 <code>admin / admin123</code> 尚未改密，登录后请尽快在「个人中心」修改。</div>' : ''}
        ${allowReg && tab === 'register'
          ? `<div class="gate-hint">${cfg.has_users
              ? `平台已有 ${cfg.user_count || 0} 个账号，新注册的为普通用户；管理员请直接登录。密码至少 ${cfg.password_min || 6} 位。`
              : `平台还没有账号时，第一个注册的用户将成为管理员。密码至少 ${cfg.password_min || 6} 位。`}</div>` : ''}
        <div class="gate-foot">
          <span class="spacer"></span>
          ${cfg.require_login === false ? '<button class="btn xs ghost" type="button" data-act="gate-close">返回工作台</button>' : ''}
          <span>会话有效期 ${cfg.session_hours ?? 72} 小时</span>
        </div>
      </div>
    </div>`;
}

function gateFields(tab) {
  const cfg = Auth.config || {};
  if (tab === 'register') {
    return `
      <div class="field"><label class="lbl">用户名</label>
        <input class="input" id="gateUser" placeholder="3-32 位字母 / 数字 / _ - ." /></div>
      <div class="field"><label class="lbl">密码</label>
        <input class="input" id="gatePass" type="password" placeholder="至少 ${cfg.password_min || 6} 位" /></div>
      <div class="field"><label class="lbl">昵称（可选）</label>
        <input class="input" id="gateNick" placeholder="展示用的名字" /></div>
      <div class="field"><label class="lbl">邮箱（可选）</label>
        <input class="input" id="gateEmail" placeholder="name@example.com" /></div>`;
  }
  return `
    <div class="field"><label class="lbl">用户名</label>
      <input class="input" id="gateUser" placeholder="用户名" autofocus /></div>
    <div class="field"><label class="lbl">密码</label>
      <input class="input" id="gatePass" type="password" placeholder="密码" /></div>
    <label class="check"><input type="checkbox" id="gateRemember" /><span>记住我（会话至少保留 30 天）</span></label>`;
}

async function submitGate() {
  const gate = $('#gate');
  if (!gate) return;
  const tab = gate.dataset.tab || 'login';
  const userEl = $('#gateUser');
  const passEl = $('#gatePass');
  const username = userEl ? userEl.value.trim() : '';
  const password = passEl ? passEl.value : '';
  const errBox = $('#gateError');
  errBox.classList.add('hidden');

  if (!username || !password) {
    errBox.textContent = '请输入用户名与密码';
    errBox.classList.remove('hidden');
    return;
  }

  const payload = tab === 'register'
    ? {
        username,
        password,
        nickname: ($('#gateNick') ? $('#gateNick').value.trim() : ''),
        email: ($('#gateEmail') ? $('#gateEmail').value.trim() : ''),
      }
    : { username, password, remember: !!($('#gateRemember') && $('#gateRemember').checked) };

  const button = $('#gateForm button[type="submit"]');
  if (button) { button.disabled = true; button.textContent = '处理中…'; }

  try {
    const data = await POST(tab === 'register' ? '/api/auth/register' : '/api/auth/login', payload, { auth: false });
    Auth.token = data.token;
    Auth.user = data.user;
    localStorage.setItem(TOKEN_KEY, data.token);
    if (Auth.user.preferences && Auth.user.preferences.theme) applyTheme(Auth.user.preferences.theme);
    toast(`欢迎回来，${Auth.user.display_name}`, 'ok');
    onAuthenticated();
  } catch (err) {
    const msg = err.message || '操作失败';
    // 用户名已被占用：通常是已有账号却走了注册流程，引导回登录并保留用户名
    if (tab === 'register' && /已被占用|已存在/.test(msg)) {
      gate.dataset.tab = 'login';
      renderGate();
      const userEl2 = $('#gateUser');
      if (userEl2) userEl2.value = username;
      const passEl2 = $('#gatePass');
      if (passEl2) passEl2.focus();
      const box = $('#gateError');
      box.textContent = `用户名 '${username}' 已存在，请直接登录；忘记密码请联系管理员重置。`;
      box.classList.remove('hidden');
      return;
    }
    errBox.textContent = msg;
    errBox.classList.remove('hidden');
    if (button) { button.disabled = false; button.textContent = tab === 'login' ? '登录' : '创建账号并登录'; }
  }
}

/* ------------------------------------------------ 用户卡 / 菜单 */

function avatarHtml(u, cls = 'sm') {
  const bg = (u && u.color) || 'var(--accent)';
  const inner = u && u.avatar ? esc(u.avatar) : esc((u && u.initial) || '?');
  return `<span class="avatar ${cls}" style="background:${bg}">${inner}</span>`;
}

function renderChrome() {
  const u = Auth.user;
  const av = $('#userAvatar');
  const nm = $('#userName');
  const rl = $('#userRole');
  if (av) {
    av.style.background = u ? (u.color || 'var(--accent)') : 'var(--muted-2)';
    av.textContent = u ? (u.avatar || u.initial || '?') : '?';
  }
  if (nm) {
    nm.textContent = u ? u.display_name
      : (Auth.config && Auth.config.require_login === false ? '未登录（校验已关闭）' : '未登录');
  }
  if (rl) rl.textContent = u ? (u.role === 'admin' ? '管理员' : '普通用户') : '点击登录';
  $$('[data-admin-only]').forEach((el) => el.classList.toggle('hidden', !(u && u.role === 'admin')));
  renderDocsNav();
  renderUserMenu();
}

/** 「API 文档」入口按角色放开：不可见时置灰并给出说明。 */
function renderDocsNav() {
  const nav = $('[data-nav="docs"]');
  if (!nav) return;
  const allowed = canViewDocs();
  nav.style.opacity = allowed ? '' : '0.45';
  nav.title = allowed
    ? '在独立页面查看接口文档（需登录，页面会绑定当前账号会话）'
    : '当前设置仅允许管理员查看接口文档';
}

function renderUserMenu() {
  const menu = $('#userMenu');
  if (!menu) return;
  const u = Auth.user;
  const items = [];
  if (u) items.push('<button class="menu-item" type="button" data-act="goto" data-hash="#/profile"><span class="ic">☺</span>个人中心</button>');
  items.push('<button class="menu-item" type="button" data-act="goto" data-hash="#/theme"><span class="ic">◐</span>主题外观</button>');
  if (u && u.role === 'admin') {
    items.push('<button class="menu-item" type="button" data-act="goto" data-hash="#/users"><span class="ic">☷</span>用户管理</button>');
  }
  if (u) {
    items.push('<div style="height:1px;background:var(--border-soft);margin:5px 4px"></div>');
    items.push('<button class="menu-item danger" type="button" data-act="logout"><span class="ic">⏻</span>退出登录</button>');
  } else {
    items.push('<button class="menu-item" type="button" data-act="open-gate"><span class="ic">→</span>登录账号</button>');
  }
  const head = u
    ? `<div class="row" style="gap:9px;padding:7px 9px;flex-wrap:nowrap">
         ${avatarHtml(u, 'sm')}
         <div style="min-width:0">
           <div style="font-weight:600;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(u.display_name)}</div>
           <div class="desc" style="font-size:10.5px">@${esc(u.username)}</div>
         </div>
       </div>
       <div style="height:1px;background:var(--border-soft);margin:4px 4px 5px"></div>`
    : '';
  menu.innerHTML = head + items.join('');
}

function toggleUserMenu() {
  const menu = $('#userMenu');
  if (menu) menu.classList.toggle('hidden');
}

function closeUserMenu() {
  const menu = $('#userMenu');
  if (menu) menu.classList.add('hidden');
}

/* ------------------------------------------------------------ 提示 / 弹层 */

function toast(message, kind = '') {
  const box = $('#toasts');
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = message;
  box.appendChild(el);
  setTimeout(() => el.remove(), kind === 'bad' ? 6000 : 3200);
}

function openModal({ title, body, footer = '', wide = false }) {
  const mask = $('#modal');
  mask.innerHTML = `
    <div class="modal ${wide ? 'wide' : ''}" role="dialog">
      <header><h3>${title}</h3><div class="spacer"></div>
        <button class="btn ghost" data-act="modal-close">关闭</button></header>
      <div class="body">${body}</div>
      ${footer ? `<footer>${footer}</footer>` : ''}
    </div>`;
  mask.classList.remove('hidden');
}

function closeModal() {
  const mask = $('#modal');
  mask.classList.add('hidden');
  mask.innerHTML = '';
}

/* ------------------------------------------------------------ 状态徽标 */

function contractChip() {
  const o = S.overview;
  if (!o) return '';
  const d = o.diagnosis;
  return d.ok
    ? '<span class="chip ok"><i class="dot"></i>契约完整</span>'
    : `<span class="chip bad"><i class="dot"></i>契约断裂 ${d.issues.length} 处</span>`;
}

function dirtyChip() {
  const o = S.overview;
  if (!o) return '';
  return o.health.dirty
    ? '<span class="chip warn"><i class="dot"></i>草稿未生效</span>'
    : '<span class="chip ok"><i class="dot"></i>已生效</span>';
}

function refreshTopbar() {
  const o = S.overview;
  $('#brandName').textContent = o ? o.platform.name : 'Agent 平台';
  $('#brandVer').textContent = o ? `v${o.platform.version}` : '';
  $('#topChips').innerHTML = [
    contractChip(),
    dirtyChip(),
    o ? `<span class="chip">流水线 ${esc(o.health.pipeline || '-')}</span>` : '',
    o ? `<span class="chip">技能 ${o.counts.skills}/${o.counts.skills_total}</span>` : '',
  ].filter(Boolean).join('');

  const canApply = !!o && o.health.dirty && o.diagnosis.ok;
  $('#btnApply').disabled = !canApply;
  $('#btnApply').title = o && !o.diagnosis.ok ? '契约断裂，先修复再接续' : '把草稿编译为生效流水线';

  // 导航角标
  const badges = {
    '#/orchestration': o ? `${o.counts.agents} 阶段` : '',
    '#/skills': o ? `${o.counts.skills}` : '',
    '#/runs': o ? `${o.runs.total}` : '',
    '#/knowledge': o ? `${o.counts.indexed_runs}` : '',
  };
  $$('.nav-item').forEach((item) => {
    const tag = $('.tag', item);
    const text = badges[item.dataset.hash] || '';
    if (tag) tag.textContent = text;
    else if (text) item.insertAdjacentHTML('beforeend', `<span class="tag">${text}</span>`);
  });
}

async function refreshOverview() {
  S.overview = await GET('/api/platform/overview');
  refreshTopbar();
}

/* ------------------------------------------------------------ 路由 */

const ROUTES = {
  '#/overview': { title: '概览', sub: '平台健康度与运行概况', load: async () => { await refreshOverview(); }, render: viewOverview },
  '#/orchestration': { title: '编排工作台', sub: '按阶段增删功能，实时校验任务联动', load: async () => { S.orch = await GET('/api/orchestration'); await loadPipelines(); await refreshOverview(); }, render: viewOrchestration },
  '#/skills': { title: '技能库', sub: '插件注册中心：启停、参数、沙盒试跑', load: async () => { S.skills = await GET('/api/skills'); }, render: viewSkills },
  '#/runs': { title: '运行记录', sub: '执行历史、轨迹与重放', load: async () => { await loadRuns(); await refreshOverview(); }, render: viewRuns },
  '#/knowledge': { title: '知识库', sub: '向量索引浏览与相似度检索', load: async () => { await loadIndexes(); }, render: viewKnowledge },
  '#/settings': { title: '系统设置', sub: '平台基础配置与模型供应商', load: async () => { S.settings = await GET('/api/settings'); }, render: viewSettings },
  '#/profile': {
    title: '个人中心', sub: '资料 / 密码 / 登录设备',
    load: async () => {
      if (Auth.user) {
        const me = await GET('/api/auth/me');
        Auth.user = me.user;
      }
      S.ui.profileDraft = Auth.user ? {
        avatar: Auth.user.avatar || '',
        color: Auth.user.color || '#5b8cff',
        nickname: Auth.user.nickname || '',
        email: Auth.user.email || '',
        bio: Auth.user.bio || '',
      } : null;
      S.ui.sessions = Auth.user ? await GET('/api/auth/sessions') : null;
    },
    render: viewProfile,
  },
  '#/theme': { title: '主题外观', sub: '配色 / 深浅 / 圆角 / 密度 / 光晕', load: async () => {}, render: viewTheme },
  '#/users': {
    title: '用户管理', sub: '账号的创建、角色与状态',
    load: async () => { S.users = await GET('/api/auth/users'); }, render: viewUsers,
  },
};

let currentRoute = '';

async function route() {
  const hash = ROUTES[location.hash] ? location.hash : '#/overview';
  const spec = ROUTES[hash];
  currentRoute = hash;
  clearFlowTimers();                    // 切换视图时先停掉上一轮的演示动效
  S.ui.flowPlaying = false;
  if (hash !== '#/orchestration') S.ui.flowPlayed = false;   // 下次进入重新依次点亮

  $$('.nav-item').forEach((el) => el.classList.toggle('active', el.dataset.hash === hash));
  document.title = `${spec.title} · Agent 平台`;
  $('#viewTitle').textContent = spec.title;
  $('#viewSub').textContent = spec.sub;
  $('#view').innerHTML = '<div class="loading"><span class="spin"></span> 加载中…</div>';

  try {
    await spec.load();
    $('#view').innerHTML = spec.render();
  } catch (err) {
    $('#view').innerHTML = `<div class="panel"><div class="issue">加载失败：${esc(err.message)}</div></div>`;
  }
}

function goto(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

async function reloadView() {
  await route();
}

/* ============================================================ 视图：概览 */

function viewOverview() {
  const o = S.overview;
  if (!o) return '<div class="empty">无数据</div>';
  const c = o.counts;

  const kpis = [
    ['Agent 阶段', c.agents, `共 ${c.steps} 个功能步骤`],
    ['可用技能', `${c.skills}/${c.skills_total}`, c.disabled_skills ? `已停用 ${c.disabled_skills} 个` : '全部启用'],
    ['槽位', c.slots, '能力接口分类'],
    ['编排方案', c.profiles, '可加载的存档'],
    ['运行次数', o.runs.total, `成功率 ${(o.runs.success_rate * 100).toFixed(1)}%`],
    ['索引切片', num(c.indexed_chunks), `${c.indexed_runs} 个索引`],
    ['平均耗时', fmtMs(o.runs.avg_elapsed_ms), '基于历史运行'],
    ['运行时长', fmtDuration(o.platform.uptime_seconds), `启动于 ${fmtTime(o.platform.started_at)}`],
  ];

  const issues = [
    ...o.diagnosis.issues.map((i) => `<div class="issue">${esc(i)}</div>`),
    ...o.diagnosis.suggestions.map((i) => `<div class="issue sug">${esc(i)}</div>`),
  ].join('') || '<div class="chip ok">✓ 编排链路完整</div>';

  const recent = o.recent_runs.length
    ? `<table class="tbl"><thead><tr>
         <th>文件</th><th>状态</th><th>切片</th><th>耗时</th><th>时间</th><th></th>
       </tr></thead><tbody>${o.recent_runs.map((r) => `
         <tr>
           <td class="mono">${esc(r.filename)}</td>
           <td>${statusChip(r.status)}</td>
           <td>${num(r.chunk_count)}</td>
           <td class="mono">${fmtMs(r.elapsed_ms)}</td>
           <td class="mono">${fmtTime(r.started_at)}</td>
           <td><button class="btn xs ghost" data-act="run-detail" data-run="${esc(r.run_id)}">详情</button></td>
         </tr>`).join('')}</tbody></table>`
    : '<div class="empty">还没有运行记录，去「编排工作台」试跑一次</div>';

  const slotCards = Object.entries(o.slots).map(([slot, names]) => `
    <div class="kpi">
      <div class="k"><span class="slot-badge">${esc(slot)}</span></div>
      <div class="v" style="font-size:14px;font-family:ui-monospace,monospace">${names.map(esc).join('<br>')}</div>
    </div>`).join('');

  return `
    <div class="grid c4">${kpis.map(([k, v, x]) => `
      <div class="kpi"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="x">${esc(x)}</div></div>
    `).join('')}</div>

    <div class="panel" style="margin-top:16px">
      <header><h2>编排健康度</h2><div class="spacer"></div>
        <span class="chip">生效流水线：${esc(o.health.pipeline || '-')}</span>
        <button class="btn xs" data-act="goto" data-hash="#/orchestration">去编排</button>
      </header>
      ${issues}
    </div>

    <div class="panel">
      <header><h2>最近运行</h2><div class="spacer"></div>
        <button class="btn xs" data-act="goto" data-hash="#/runs">全部记录</button></header>
      ${recent}
    </div>

    <div class="panel">
      <header><h2>能力矩阵</h2><div class="desc">每个槽位下的可用实现</div></header>
      <div class="grid c3">${slotCards}</div>
    </div>`;
}

function statusChip(status) {
  return status === 'success'
    ? '<span class="chip ok">成功</span>'
    : '<span class="chip bad">失败</span>';
}

/* ============================================================ 视图：编排工作台 */

function paginate(items, page = 1, size = PAGE_SIZE) {
  const list = items || [];
  const total = list.length;
  const pages = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, page || 1), pages);
  const start = (current - 1) * size;
  return { items: list.slice(start, start + size), total, page: current, size, pages };
}

function renderPager(page, pages, act, label = '') {
  if (!label && (!pages || pages <= 1)) return '';
  const buttons = [];
  for (let i = 1; i <= pages; i += 1) {
    buttons.push(`<button class="btn xs ${i === page ? 'primary' : 'ghost'}" data-act="${act}" data-page="${i}">${i}</button>`);
  }
  return `<div class="pager"><span class="pager-info">${label}</span>
    <div class="row" style="gap:6px">${buttons.join('')}</div></div>`;
}

function pagerLabel(page, total) {
  return `共 ${total} 条 · 第 ${page.page}/${page.pages} 页`;
}

function renderPipelinePagination() {
  const p = S.pipelines;
  if (!p) return '';
  return renderPager(p.page, p.pages, 'pipeline-page', pagerLabel(p, p.total));
}

function viewOrchestration() {
  const s = S.orch;
  if (!s) return '<div class="empty">无数据</div>';
  if (S.ui.flowPlaying) stopFlow();   // 重渲染会重建节点，先收掉上一轮的演示定时器
  const agents = s.orchestration.agents;
  ensureSelectedAgent(agents);
  const current = agents.find((a) => a.id === S.ui.selectedAgent) || null;
  const currentIndex = current ? agents.indexOf(current) : -1;

  const html = `
  <div class="workbench">
    <div>
      <div class="panel">
        <header>
          <h2>${esc(s.orchestration.name)}</h2>
          <div class="desc">${esc(s.orchestration.description || '')}</div>
          <div class="spacer"></div>
          <button class="btn xs" data-act="validate">重新校验</button>
          <button class="btn xs" data-act="autofill">自动补全</button>
          <button class="btn xs ghost" data-act="reset-draft">重置草稿</button>
        </header>
        <div class="flow-head">
          <div class="flow-title">执行链路</div>
          <div class="spacer"></div>
          <span class="flow-status" id="flowStatus"></span>
          <button class="btn xs ghost" id="flowPlayBtn" data-act="play-flow">▶ 演示执行顺序</button>
        </div>
        ${renderFlow(agents, s.diagnosis)}
        <div class="row" style="margin-top:12px">
          <button class="btn" data-act="add-agent">+ 添加 Agent 阶段</button>
          <span class="desc" style="font-size:11.5px">点击链路中的任意阶段，即可在下方修改它的名称、职责与功能步骤</span>
        </div>
      </div>

      ${current
        ? renderStageEditor(current, currentIndex)
        : '<div class="panel"><div class="empty">还没有阶段，点击上方「添加 Agent 阶段」开始编排</div></div>'}
    </div>

    <aside>
      <div class="panel">
        <header><h2>契约诊断</h2></header>
        ${renderDiagnosis(s.diagnosis)}
      </div>

      <div class="panel">
        <header><h2>试跑当前编排</h2></header>
        <div class="drop" id="drop">
          <div class="big">点击或拖拽文件</div>
          <div class="hint">使用当前草稿执行，不影响已生效流水线</div>
        </div>
        <input type="file" id="fileInput" class="hidden" style="display:none" />
        <div id="uploadStatus" class="desc" style="margin-top:8px;min-height:16px;font-size:11.5px"></div>
      </div>

      <div class="panel">
        <header>
          <h2>流水线任务</h2>
          <div class="spacer"></div>
          <button class="btn xs primary" data-act="pipeline-create">+ 新建流水线</button>
        </header>
        <div class="grid" style="gap:8px">
          ${S.pipelines && S.pipelines.items.length
            ? S.pipelines.items.map((p) => `
              <div class="profile-card ${p.active ? 'active' : ''}">
                <span class="chip ${p.ok ? 'ok' : 'bad'}">${p.ok ? '可用' : '断裂'}</span>
                <div style="flex:1;min-width:80px">
                  <div style="font-weight:600">${esc(p.name)}</div>
                  <div class="desc" style="font-size:11px">${p.agents ?? '?'} 阶段 / ${p.steps ?? '?'} 步骤 · ${esc(p.description || '无描述')}</div>
                </div>
                ${p.active
                  ? '<span class="chip" style="margin-left:auto">当前</span>'
                  : `<button class="btn xs" data-act="pipeline-switch" data-name="${esc(p.name)}">切换</button>`}
                <button class="btn xs ghost danger" data-act="pipeline-del" data-name="${esc(p.name)}">删除</button>
              </div>`).join('')
            : '<div class="empty">暂无流水线任务</div>'}
        </div>
        ${renderPipelinePagination()}
      </div>
    </aside>
  </div>`;

  S.ui.flowPlayed = true;   // 之后的重渲染不再重放入场动画
  return html;
}

/** 选中的阶段始终有效：首次进入或被删除后自动回落到第一个 */
function ensureSelectedAgent(agents) {
  if (!agents.length) { S.ui.selectedAgent = null; return; }
  if (!agents.some((a) => a.id === S.ui.selectedAgent)) S.ui.selectedAgent = agents[0].id;
}

/** 执行链路：每个阶段一个状态节点，点击即可在下方展开它的配置 */
function renderFlow(agents, diagnosis) {
  if (!agents.length) return '<div class="flow flow-empty">还没有阶段</div>';
  const reports = {};
  ((diagnosis && diagnosis.agents) || []).forEach((r) => { reports[r.id] = r; });
  const parts = [];
  agents.forEach((a, i) => {
    if (i) parts.push(`<span class="flow-arrow" style="--delay:${(i * 0.3).toFixed(2)}s"></span>`);
    parts.push(renderFlowNode(a, i, reports[a.id]));
  });
  return `<div class="flow ${S.ui.flowPlayed ? 'static' : ''}" id="flowMap">${parts.join('')}</div>`;
}

function renderFlowNode(agent, index, report) {
  const r = report || { ok: true, steps: [] };
  const state = !agent.steps.length ? 'empty' : (r.ok ? 'ready' : 'broken');
  const selected = S.ui.selectedAgent === agent.id;
  const mark = state === 'ready' ? '✓' : state === 'broken' ? '!' : String(index + 1);

  const brokenSteps = (r.steps || []).filter((x) => (x.issues || []).length).length;
  const meta = state === 'empty'
    ? '等待配置'
    : state === 'broken'
      ? `${brokenSteps || (r.steps || []).length} 项待修复`
      : `${agent.steps.length} 个步骤`;

  // 试跑过就显示真实耗时（同阶段的多个步骤累加）
  const spent = S.ui.flowTimes ? S.ui.flowTimes[agent.id] : undefined;
  const time = spent === undefined ? '' : `<span class="node-time">${fmtMs(spent)}</span>`;

  return `
  <button class="flow-node ${state} ${selected ? 'selected' : ''}" type="button" style="--i:${index}"
    data-act="select-agent" data-agent="${agent.id}" data-order="${index + 1}" title="${esc(agent.role || agent.name)}">
    <span class="node-mark">${mark}</span>
    <span class="node-name">${esc(agent.name)}</span>
    <span class="node-meta">${meta}${time}</span>
  </button>`;
}

/** 当前选中阶段的配置面板：改名 / 改职责 / 步骤增删改 / 参数编辑 */
function renderStageEditor(agent, index) {
  const s = S.orch;
  const total = s.orchestration.agents.length;
  const report = (s.diagnosis.agents || []).find((a) => a.id === agent.id) || { ok: true, steps: [] };
  const stepReports = {};
  (report.steps || []).forEach((r) => { stepReports[r.id] = r; });

  const steps = agent.steps.map((step, si) => renderStep(agent, step, si, stepReports[step.id])).join('');

  const addRow = S.ui.addStepFor === agent.id
    ? `<div class="row" style="margin-top:9px">
         <select class="input sm" data-role="new-skill" data-agent="${agent.id}" style="flex:1">${skillOptions(guessSkill(agent.id, agent.steps.length))}</select>
         <button class="btn xs primary" data-act="add-step-confirm" data-agent="${agent.id}">确认</button>
         <button class="btn xs ghost" data-act="add-step-cancel">取消</button>
       </div>`
    : `<div class="row" style="margin-top:9px">
         <button class="btn xs ghost" data-act="add-step" data-agent="${agent.id}">+ 添加功能（推荐 ${guessSkill(agent.id, agent.steps.length)}）</button>
       </div>`;

  return `
  <div class="panel stage-editor ${report.ok ? '' : 'bad'}">
    <header>
      <span class="stage-tag">阶段 ${index + 1}</span>
      <h2>${esc(agent.name)}</h2>
      <div class="desc">${agent.steps.length} 个功能步骤${report.ok ? '' : ' · 存在待修复问题'}</div>
      <div class="spacer"></div>
      <button class="btn xs ghost" data-act="add-agent-after" data-agent="${agent.id}">插入阶段</button>
      <button class="btn xs ghost" data-act="agent-up" data-agent="${agent.id}" ${index === 0 ? 'disabled' : ''}>← 前移</button>
      <button class="btn xs ghost" data-act="agent-down" data-agent="${agent.id}" ${index === total - 1 ? 'disabled' : ''}>后移 →</button>
      <button class="btn xs ghost danger" data-act="agent-del" data-agent="${agent.id}">删除阶段</button>
    </header>
    <div class="stage-fields">
      <div>
        <label class="lbl">阶段名称</label>
        <input class="input" value="${esc(agent.name)}" data-role="agent-name" data-agent="${agent.id}" />
      </div>
      <div>
        <label class="lbl">阶段职责</label>
        <input class="input" value="${esc(agent.role || '')}" placeholder="这个阶段负责做什么（可选）" data-role="agent-role" data-agent="${agent.id}" />
      </div>
    </div>
    <div class="stage-steps">
      ${steps || '<div class="desc" style="font-size:11.5px;padding:2px 0">该阶段暂无功能步骤，点击下方按钮添加</div>'}
    </div>
    ${addRow}
  </div>`;
}

function renderStep(agent, step, si, report) {
  const catalog = S.orch.catalog;
  const def = catalog.skills[step.skill] || { slot: '?', params: {}, consumes: [], produces: [] };
  const r = report || { ok: true, consumes: def.consumes, produces: def.produces, missing: [], issues: [], overrides: [] };
  const missing = r.missing || [];
  const bad = step.enabled && (!r.ok || missing.length);
  const open = S.ui.openParams.has(step.id);
  const hasParams = Object.keys(def.params || {}).length > 0;

  const params = open ? `<div class="params">${Object.entries(def.params).map(([key, spec]) =>
    renderParamField(agent.id, step, key, spec)).join('')}</div>` : '';

  return `
  <div class="step ${bad ? 'bad' : ''} ${step.enabled ? '' : 'off'}">
    <div class="step-head">
      <span class="slot-badge">${esc(r.slot || def.slot)}</span>
      <select class="input sm" style="max-width:200px" data-role="skill" data-agent="${agent.id}" data-step="${step.id}">
        ${skillOptions(step.skill)}
      </select>
      ${def.optional ? '<span class="slot-badge" style="color:var(--warn);border-color:#57431c">可选</span>' : ''}
      ${def.origin === 'ext' ? '<span class="tag-mini">外部</span>' : ''}
      <div class="spacer"></div>
      ${hasParams ? `<button class="btn xs ghost" data-act="step-params" data-agent="${agent.id}" data-step="${step.id}">参数${open ? ' ▾' : ' ▸'}</button>` : ''}
      <button class="btn xs ghost" data-act="step-up" data-agent="${agent.id}" data-step="${step.id}" ${si === 0 ? 'disabled' : ''}>↑</button>
      <button class="btn xs ghost" data-act="step-down" data-agent="${agent.id}" data-step="${step.id}" ${si === agent.steps.length - 1 ? 'disabled' : ''}>↓</button>
      <button class="btn xs ghost" data-act="step-toggle" data-agent="${agent.id}" data-step="${step.id}">${step.enabled ? '停用' : '启用'}</button>
      <button class="btn xs ghost danger" data-act="step-del" data-agent="${agent.id}" data-step="${step.id}">✕</button>
    </div>
    <div class="io">${(r.consumes || []).join(', ') || '∅'} → <b>${(r.produces || []).join(', ') || '∅'}</b>${
      missing.length ? `<span class="miss">缺少上游产物：${esc(missing.join(', '))}</span>` : ''
    }</div>
    ${(r.issues || []).length ? `<div class="step-issues">${r.issues.map(esc).join('；')}</div>` : ''}
    ${params}
  </div>`;
}

function renderParamField(agentId, step, key, spec) {
  const value = step.options && key in step.options ? step.options[key] : spec.default;
  const attrs = `data-role="param" data-agent="${agentId}" data-step="${step.id}" data-key="${esc(key)}"`;
  const label = esc(spec.label || key);

  if (spec.type === 'bool') {
    return `<div class="check" style="align-items:flex-start;padding-top:14px">
      <input type="checkbox" ${attrs} ${value ? 'checked' : ''} />
      <span>${label}</span></div>`;
  }

  let control;
  if (spec.type === 'list') {
    const text = Array.isArray(value) ? value.join(', ') : (value || '');
    control = `<input class="input sm" type="text" ${attrs} value="${esc(text)}" placeholder="逗号分隔" />`;
  } else if (spec.choices) {
    control = `<select class="input sm" ${attrs}>${spec.choices.map((c) =>
      `<option value="${esc(c)}" ${String(c) === String(value) ? 'selected' : ''}>${c === '' ? '(空)' : esc(c)}</option>`).join('')}</select>`;
  } else if (spec.type === 'int' || spec.type === 'float') {
    control = `<input class="input sm" type="number" ${attrs} value="${value ?? ''}"
      ${spec.min !== undefined ? `min="${spec.min}"` : ''} ${spec.max !== undefined ? `max="${spec.max}"` : ''}
      ${spec.type === 'float' ? 'step="0.1"' : ''} />`;
  } else {
    control = `<input class="input sm" type="text" ${attrs} value="${esc(value ?? '')}" />`;
  }
  return `<div><label class="lbl">${label}</label>${control}${spec.help ? `<div class="help">${esc(spec.help)}</div>` : ''}</div>`;
}

const SLOT_ORDER = ['loader', 'cleaner', 'splitter', 'embedder', 'vector_store'];

function skillOptions(selected) {
  const slots = S.orch.catalog.slots;
  const ordered = Object.keys(slots).sort((a, b) => SLOT_ORDER.indexOf(a) - SLOT_ORDER.indexOf(b));
  return ordered.map((slot) => `<optgroup label="${esc(slot)}">${slots[slot].map((name) =>
    `<option value="${esc(name)}" ${name === selected ? 'selected' : ''}>${esc(name)}</option>`).join('')}</optgroup>`).join('');
}

/** 前端本地推演产物集，用于给出「接得上且能推进」的插件推荐 */
function guessSkill(agentId, stepIndex) {
  const available = new Set(['raw_file']);
  outer:
  for (const agent of S.orch.orchestration.agents) {
    for (let i = 0; i < agent.steps.length; i += 1) {
      if (agent.id === agentId && i === stepIndex) break outer;
      const step = agent.steps[i];
      if (!step.enabled) continue;
      const def = S.orch.catalog.skills[step.skill];
      if (!def) continue;
      if (def.consumes.every((k) => available.has(k))) def.produces.forEach((k) => available.add(k));
    }
  }
  const all = Object.values(S.orch.catalog.skills)
    .sort((a, b) => SLOT_ORDER.indexOf(a.slot) - SLOT_ORDER.indexOf(b.slot));
  const pick = all.find((m) => m.consumes.every((k) => available.has(k)) && !m.produces.every((k) => available.has(k)));
  return (pick || all[0] || { name: '' }).name;
}

function renderDiagnosis(d) {
  if (d.ok && !d.issues.length) {
    return `<div class="chip ok">✓ 链路完整</div>
      <div class="desc" style="margin-top:8px;font-size:11.5px">
        产物可从 ${d.start_artifacts.map(esc).join(', ')} 一路流转到 ${d.final_artifacts.map(esc).join(', ')}
      </div>`;
  }
  return [
    ...d.issues.map((i) => `<div class="issue">${esc(i)}</div>`),
    ...d.suggestions.map((i) => `<div class="issue sug">${esc(i)}</div>`),
  ].join('') || '<div class="chip ok">✓ 无阻断问题</div>';
}

/* ============================================================ 视图：技能库 */

function slotRank(slot) {
  const i = SLOT_ORDER.indexOf(slot);
  return i === -1 ? SLOT_ORDER.length : i;
}

function viewSkills() {
  const data = S.skills;
  if (!data) return '<div class="empty">无数据</div>';

  // 先按槽位顺序摊平，再按每页 10 条分页；本页内仍按槽位分组展示
  const all = [...data.skills].sort((a, b) => {
    const d = slotRank(a.slot) - slotRank(b.slot);
    return d !== 0 ? d : String(a.name).localeCompare(String(b.name));
  });
  const page = paginate(all, S.ui.listPage.skills);
  S.ui.listPage.skills = page.page;

  const bySlot = {};
  page.items.forEach((m) => { (bySlot[m.slot] = bySlot[m.slot] || []).push(m); });

  const sections = Object.keys(bySlot)
    .sort((a, b) => slotRank(a) - slotRank(b))
    .map((slot) => `
      <div class="panel">
        <header>
          <span class="slot-badge">${esc(slot)}</span>
          <h2>${esc(slot)} 槽位</h2>
          <div class="desc">本页 ${bySlot[slot].length} 个实现，同槽位可互换</div>
        </header>
        <div class="grid c3">${bySlot[slot].map(skillCard).join('')}</div>
      </div>`).join('');

  return `
    <div class="panel">
      <header><h2>注册中心</h2>
        <div class="desc">共 ${data.total} 个插件，当前可用 ${data.count} 个</div>
        <div class="spacer"></div>
        <button class="btn xs" data-act="script-console">脚本验证</button>
        <button class="btn xs" data-act="reload-plugins">重新扫描插件目录</button>
      </header>
      <div class="desc" style="font-size:11.5px">
        把 <code>.py</code> 放进 <code>ext_plugins/</code> 后点「重新扫描」即可热插拔；停用的插件会从编排下拉框中消失，
        已有编排会立即显示契约断裂。写脚本时可以先在「脚本验证」里跑通再落盘。
      </div>
    </div>
    ${sections}
    ${renderPager(page.page, page.pages, 'skills-page', pagerLabel(page, page.total))}`;
}

function skillCard(m) {
  const disabled = m.enabled === false;
  return `
  <div class="skill-card ${disabled ? 'disabled' : ''}">
    <div class="row">
      <h4>${esc(m.name)}</h4>
      <div class="spacer"></div>
      <label class="switch" title="${disabled ? '已停用' : '已启用'}">
        <input type="checkbox" ${disabled ? '' : 'checked'} data-role="skill-toggle" data-name="${esc(m.name)}" />
        <span></span>
      </label>
    </div>
    <div class="row" style="gap:4px">
      ${m.origin === 'ext' ? '<span class="tag-mini">外部插件</span>' : '<span class="tag-mini">内置</span>'}
      ${m.optional ? '<span class="tag-mini" style="color:var(--warn)">可选依赖</span>' : ''}
      <span class="tag-mini">v${esc(m.version)}</span>
    </div>
    <div class="desc">${esc(m.description)}</div>
    <div class="row" style="gap:4px">
      ${m.consumes.map((c) => `<span class="io-pill">${esc(c)}</span>`).join('')}
      <span style="color:var(--muted-2)">→</span>
      ${m.produces.map((c) => `<span class="io-pill out">${esc(c)}</span>`).join('')}
    </div>
    <div class="foot">
      <button class="btn xs" data-act="skill-detail" data-name="${esc(m.name)}">详情</button>
      <button class="btn xs" data-act="skill-test-open" data-name="${esc(m.name)}" ${disabled ? 'disabled' : ''}>沙盒试跑</button>
    </div>
  </div>`;
}

async function showSkillDetail(name) {
  const m = await GET(`/api/skills/${encodeURIComponent(name)}`);
  const params = Object.entries(m.params || {}).map(([k, v]) => `
    <tr><td class="mono">${esc(k)}</td><td>${esc(v.type)}</td><td class="mono">${esc(JSON.stringify(v.default))}</td>
    <td>${esc(v.label || '')}</td></tr>`).join('') || '<tr><td colspan="4" class="desc">无参数</td></tr>';

  const usage = m.usage.length
    ? m.usage.map((u) => `<span class="tag-mini">${esc(u.agent)}</span>`).join(' ')
    : '<span class="desc">当前草稿未使用</span>';

  const peers = m.peers.length
    ? m.peers.map((p) => `<div class="row" style="gap:6px"><span class="io-pill">${esc(p.name)}</span>
        <span class="desc" style="font-size:11px">${esc(p.description)}</span></div>`).join('')
    : '<div class="desc">同槽位暂无其他实现</div>';

  openModal({
    title: `技能详情 · ${esc(m.name)}`,
    wide: true,
    body: `
      <div class="kv" style="margin-bottom:14px">
        <div class="k">槽位</div><div class="v"><span class="slot-badge">${esc(m.slot)}</span></div>
        <div class="k">状态</div><div class="v">${m.enabled ? '<span class="chip ok">已启用</span>' : '<span class="chip bad">已停用</span>'}</div>
        <div class="k">来源</div><div class="v">${m.origin === 'ext' ? '外部插件目录' : '内置插件包'}</div>
        <div class="k">产物契约</div><div class="v mono">${m.consumes.join(', ') || '∅'} → ${m.produces.join(', ') || '∅'}</div>
        <div class="k">说明</div><div class="v">${esc(m.description)}</div>
        <div class="k">被引用</div><div class="v">${usage}</div>
      </div>
      <h3 style="font-size:12.5px;color:var(--muted);margin:0 0 8px">参数 Schema</h3>
      <table class="tbl"><thead><tr><th>参数</th><th>类型</th><th>默认值</th><th>说明</th></tr></thead>
      <tbody>${params}</tbody></table>
      <h3 style="font-size:12.5px;color:var(--muted);margin:16px 0 8px">同槽位可替换实现</h3>
      ${peers}`,
    footer: `<button class="btn xs" data-act="skill-test-open" data-name="${esc(m.name)}" ${m.enabled ? '' : 'disabled'}>沙盒试跑</button>
             <button class="btn xs primary" data-act="modal-close">关闭</button>`,
  });
}

async function openSkillTest(name) {
  const m = await GET(`/api/skills/${encodeURIComponent(name)}`);
  S.ui.testSchema = m.params || {};   // 取值时按 param_schema 的类型转换，不能靠「值里有没有逗号」猜
  S.ui.testFile = null;               // 每次打开都清空上一次选择的文件
  const fields = Object.entries(S.ui.testSchema).map(([key, spec]) => testField(key, spec)).join('');

  openModal({
    title: `沙盒试跑 · ${esc(name)}`,
    wide: true,
    body: `
      <div class="desc" style="margin-bottom:12px;font-size:11.5px">
        沙盒会自动合成满足该插件 <code>${esc(m.consumes.join(', ') || '∅')}</code> 的上游产物，因此可以单独调试。
        真实链路中这些产物来自上游插件。
      </div>
      <div class="field">
        <label class="lbl">在线文件验证（可选）</label>
        <div class="row" style="gap:8px;align-items:center">
          <input class="input sm" type="file" id="testFile" data-role="test-file" style="flex:1;min-width:140px" />
          <button class="btn xs" data-act="skill-test-file-clear">移除文件</button>
        </div>
        <div class="help">选择文件后，其原始字节会作为 <code>raw_file</code> 送进沙盒：loader 等「吃文件」的插件可直接在线验证；文本类文件的内容会自动填入下方输入框。</div>
        <div class="desc" id="testFileInfo" style="font-size:11px;margin-top:4px"></div>
      </div>
      <div class="field"><label class="lbl">输入文本</label>
        <textarea class="input" id="testText" rows="4">这是一段用于试跑插件的示例文本。Agent 平台支持多 Agent 串联与插拔式插件，内部机密 内容应当被掩码。</textarea>
      </div>
      <h3 style="font-size:12.5px;color:var(--muted);margin:12px 0 8px">参数配置</h3>
      <div class="params" style="border:0;padding:0">${fields || '<div class="desc">该插件无参数</div>'}</div>
      <div id="testResult" style="margin-top:14px"></div>`,
    footer: `<button class="btn xs" data-act="skill-test-reset" data-name="${esc(name)}">恢复默认</button>
             <button class="btn xs primary" data-act="skill-test-run" data-name="${esc(name)}">运行</button>
             <button class="btn xs" data-act="modal-close">关闭</button>`,
  });
  renderTestFileInfo();
}

/** 文本类文件：选中后把内容读进输入框，方便清洗/切片类插件也能拿到文件内容。 */
const TEST_TEXT_FILE_RE = /\.(txt|md|markdown|csv|tsv|json|jsonl|log|xml|html?|xhtml|ya?ml|ini|conf|cfg|properties|py|js|ts|java|go|rs|sql|sh|bash|bat|cmd|ps1)$/i;

function isTextUpload(file) {
  const type = (file.type || '').toLowerCase();
  if (type.startsWith('text/')) return true;
  if (/json|xml|javascript|csv|yaml/.test(type)) return true;
  return TEST_TEXT_FILE_RE.test(file.name || '');
}

/** Uint8Array → base64，分块拼接避免大文件撑爆调用栈。 */
function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/** 读取沙盒文件到内存（base64 用于回传，文本类顺带填进输入框）。 */
async function loadTestFile(file) {
  if (!file) return;
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    S.ui.testFile = {
      name: file.name,
      content_type: file.type || '',
      data_base64: bytesToBase64(bytes),
      bytes: bytes.length,
    };
    if (isTextUpload(file)) {
      const box = $('#testText');
      if (box) box.value = new TextDecoder('utf-8').decode(bytes);
    }
    renderTestFileInfo();
    toast(`已载入 ${file.name}`, 'ok');
  } catch (err) {
    clearTestFile();
    toast(`读取文件失败：${err.message}`, 'bad');
  }
}

function clearTestFile() {
  S.ui.testFile = null;
  const input = $('#testFile');
  if (input) input.value = '';
  renderTestFileInfo();
}

function renderTestFileInfo() {
  const el = $('#testFileInfo');
  if (!el) return;
  const f = S.ui.testFile;
  el.innerHTML = f
    ? `已选择：<b>${esc(f.name)}</b>（${fmtBytes(f.bytes)}${f.content_type ? ` · ${esc(f.content_type)}` : ''}）`
    : '未选择文件，将以「输入文本」编码为 raw_file 送入沙盒。';
}

/**
 * 按 param_schema 生成沙盒参数控件，语义与编排工作台的参数表单保持一致：
 * bool → 勾选框；带 choices 的 str → 下拉框；list → 逗号分隔输入；数值 → number（带上下限）。
 */
function testField(key, spec) {
  const attrs = `data-testkey="${esc(key)}"`;
  const value = spec.default;
  const label = esc(spec.label || key);
  const help = spec.help ? `<div class="help">${esc(spec.help)}</div>` : '';

  if (spec.type === 'bool') {
    return `<div class="check" style="align-items:flex-start;padding-top:14px">
      <input type="checkbox" ${attrs} ${value ? 'checked' : ''}/><span>${label}</span></div>`;
  }

  let control;
  if (spec.type === 'list') {
    const text = Array.isArray(value) ? value.join(', ') : (value || '');
    control = `<input class="input sm" type="text" ${attrs} value="${esc(text)}" placeholder="逗号分隔"/>`;
  } else if (spec.choices) {
    control = `<select class="input sm" ${attrs}>${spec.choices.map((c) =>
      `<option value="${esc(c)}" ${String(c) === String(value) ? 'selected' : ''}>${c === '' ? '(空)' : esc(c)}</option>`).join('')}</select>`;
  } else if (spec.type === 'int' || spec.type === 'float') {
    control = `<input class="input sm" type="number" ${attrs} value="${value ?? ''}"
      ${spec.min !== undefined ? `min="${spec.min}"` : ''} ${spec.max !== undefined ? `max="${spec.max}"` : ''}
      ${spec.type === 'float' ? 'step="0.1"' : ''}/>`;
  } else {
    control = `<input class="input sm" type="text" ${attrs} value="${esc(value ?? '')}"/>`;
  }
  return `<div><label class="lbl">${label}</label>${control}${help}</div>`;
}

/** 把沙盒参数表单恢复成 param_schema 声明的默认值。 */
function resetTestOptions() {
  $$('#modal [data-testkey]').forEach((el) => {
    const spec = (S.ui.testSchema || {})[el.dataset.testkey] || {};
    if (el.type === 'checkbox') el.checked = !!spec.default;
    else if (spec.type === 'list') el.value = Array.isArray(spec.default) ? spec.default.join(', ') : (spec.default || '');
    else el.value = spec.default ?? '';
  });
}

/**
 * 按 param_schema 声明的类型取值。
 * 不能靠「值里有没有逗号」猜类型：单词敏感词表会退化成字符串（插件再 list() 就成单字符），
 * 而本身含逗号的 str（如 DSN）又会被误拆成数组。
 */
function collectTestOptions() {
  const schema = S.ui.testSchema || {};
  const options = {};
  $$('#modal [data-testkey]').forEach((el) => {
    const key = el.dataset.testkey;
    const spec = schema[key] || {};
    if (spec.type === 'bool') {
      options[key] = el.checked;
    } else if (spec.type === 'list') {
      options[key] = el.value.split(',').map((s) => s.trim()).filter(Boolean);
    } else if (spec.type === 'int' || spec.type === 'float') {
      const num = spec.type === 'int' ? parseInt(el.value, 10) : parseFloat(el.value);
      options[key] = Number.isNaN(num) ? spec.default : num;
    } else {
      options[key] = el.value;
    }
  });
  return options;
}

async function runSkillTest(name) {
  const box = $('#testResult');
  const btn = $('#modal [data-act="skill-test-run"]');
  if (btn) btn.disabled = true;
  box.innerHTML = '<div class="loading"><span class="spin"></span> 运行中…</div>';
  try {
    const payload = { options: collectTestOptions(), text: $('#testText').value };
    const f = S.ui.testFile;   // 有文件时带上：真字节当 raw_file，文本仍作为合成上游产物的来源
    if (f) {
      payload.filename = f.name;
      payload.content_type = f.content_type;
      payload.data_base64 = f.data_base64;
    }
    const result = await POST(`/api/skills/${encodeURIComponent(name)}/test`, payload);
    box.innerHTML = renderTestResult(result);
  } catch (err) {
    box.innerHTML = `<div class="issue">${esc(err.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderTestResult(result) {
  const outputs = result.outputs.length
    ? result.outputs.map((o) => `
      <div class="chunk">
        <div class="meta">${esc(o.kind)} · ${esc(o.size)} · 由 ${esc(o.producer)} 产出</div>
        <div class="txt">${esc(typeof o.preview === 'string' ? o.preview : JSON.stringify(o.preview))}</div>
      </div>`).join('')
    : '<div class="desc">该插件未产出任何总线产物</div>';

  const used = Object.entries(result.options || {});
  const options = used.length
    ? used.map(([k, v]) => `<span class="tag-mini">${esc(k)} = ${esc(JSON.stringify(v))}</span>`).join(' ')
    : '<span class="desc">无参数</span>';

  return `
    <div class="row" style="margin-bottom:8px">
      <span class="chip ${result.ok ? 'ok' : 'bad'}">${result.ok ? '执行成功' : '执行失败'}</span>
      <span class="chip">${fmtMs(result.duration_ms)}</span>
      <span class="chip">输入：${esc(result.inputs.join(', ') || '∅')}</span>
      ${result.file ? `<span class="chip">文件：${esc(result.file.name)} · ${fmtBytes(result.file.bytes)}</span>` : ''}
    </div>
    ${result.error ? `<div class="issue">${esc(result.error)}</div>` : ''}
    <div style="font-size:11.5px;color:var(--muted);margin:8px 0 4px">生效参数</div>
    <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:8px">${options}</div>
    ${outputs}`;
}

/* ------------------------------------------------------------ 脚本验证 */

/** 各语言的示例脚本：让「验证」打开就有东西可跑，同时示范插件脚本的正确写法。 */
const SCRIPT_SAMPLES = {
  python: `from app.core.context import CLEAN_TEXT, TEXT
from app.core.skill import Skill, skill


@skill
class UppercaseCleaner(Skill):
    """把文本统一转大写的最小插件：验证「导入 + 注册 + run」三件事。"""

    name = "uppercase_cleaner"
    slot = "cleaner"
    description = "在线验证示例：将文本转为大写"
    consumes = (TEXT,)
    produces = (CLEAN_TEXT,)

    def run(self, ctx) -> None:
        ctx.put(CLEAN_TEXT, ctx.require(TEXT).upper(), producer=self.name)


print("插件已注册：", UppercaseCleaner.name, "/", UppercaseCleaner.slot)
print("参数 Schema：", UppercaseCleaner.default_options())`,
  sh: `#!/bin/sh
set -e
echo "== 环境探测 =="
echo "工作目录：$(pwd)"
echo "系统    ：$(uname -s 2>/dev/null || echo unknown)"
python -V 2>&1 || echo "python 不在 PATH"`,
  powershell: `Write-Output "== 环境探测 =="
Write-Output ("工作目录：" + (Get-Location).Path)
Write-Output ("PS 版本 ：" + $PSVersionTable.PSVersion.ToString())`,
};

async function openScriptConsole() {
  let info;
  try {
    info = await GET('/api/scripts/runtimes');
  } catch (err) {
    toast(err.message, 'bad');
    return;
  }
  S.ui.scriptRuntimes = info.runtimes || [];

  const options = S.ui.scriptRuntimes.map((r) => `
    <option value="${esc(r.key)}" ${r.available ? '' : 'disabled'}>${esc(r.label)}${r.available ? '' : '（本机不可用）'}</option>`).join('');
  const first = S.ui.scriptRuntimes.find((r) => r.available) || S.ui.scriptRuntimes[0] || { key: 'python' };

  openModal({
    title: '脚本验证 · Python / Shell',
    wide: true,
    body: `
      <div class="desc" style="font-size:11.5px;margin-bottom:10px">
        会在<b>本机真实执行</b>这段脚本，等价于把文件存下来再手动运行——请只验证你自己信任的代码。
        单次执行超时 ${info.limits.timeout_sec} 秒、输出上限 ${info.limits.max_output_kb}KB，
        均可在「系统设置 → 脚本验证」中调整。
        ${info.admin_only ? '<span style="color:var(--warn)">当前已限制仅管理员可执行。</span>' : ''}
      </div>
      <div class="grid c2" style="gap:10px">
        <div><label class="lbl">语言</label>
          <select class="input sm" id="scriptLang" data-role="script-lang">${options}</select>
          <div class="help" id="scriptRuntime"></div></div>
        <div><label class="lbl">单次超时（秒）</label>
          <input class="input sm" type="number" id="scriptTimeout" min="1" max="120" value="${info.limits.timeout_sec}"/>
          <div class="help">留空则用平台设置里的 ${info.limits.timeout_sec} 秒</div></div>
      </div>
      <div class="field" style="margin-top:10px">
        <label class="lbl">脚本内容</label>
        <textarea class="input script-code" id="scriptCode" rows="16" spellcheck="false"></textarea>
        <div class="row" style="margin-top:6px;gap:8px">
          <input type="file" id="scriptFile" class="script-file" data-role="script-file"
                 accept=".py,.sh,.bash,.ps1,.txt"/>
          <span class="desc" style="font-size:11px">只把本地文件读进编辑器，不会上传或保存</span>
        </div>
      </div>
      <div class="field">
        <label class="lbl">标准输入（可选）</label>
        <textarea class="input script-code" id="scriptStdin" rows="2" spellcheck="false"></textarea>
      </div>
      <div id="scriptResult"></div>`,
    footer: `<button class="btn xs" data-act="script-sample">载入示例</button>
             <button class="btn xs" data-act="script-clear">清空</button>
             <button class="btn xs primary" data-act="script-run">验证运行</button>
             <button class="btn xs" data-act="modal-close">关闭</button>`,
  });

  $('#scriptLang').value = first.key;
  applyScriptSample(true);
  syncScriptRuntime();
}

/** 填充示例：force 为 true 时无条件覆盖，否则只在编辑器为空或是别的示例时替换。 */
function applyScriptSample(force = false) {
  const box = $('#scriptCode');
  if (!box) return;
  const known = Object.values(SCRIPT_SAMPLES);
  if (force || !box.value.trim() || known.includes(box.value)) {
    box.value = SCRIPT_SAMPLES[$('#scriptLang').value] || '';
  }
}

function onScriptLangChange() {
  applyScriptSample(false);
  syncScriptRuntime();
}

/** 把当前语言解析到的解释器路径展示出来：验证「不可用」到底是缺哪个命令。 */
function syncScriptRuntime() {
  const el = $('#scriptRuntime');
  if (!el) return;
  const key = $('#scriptLang').value;
  const rt = (S.ui.scriptRuntimes || []).find((r) => r.key === key);
  if (!rt || !rt.available) {
    el.innerHTML = `<span style="color:var(--danger)">本机不可用</span>${rt && rt.hint ? ` · ${esc(rt.hint)}` : ''}`;
    return;
  }
  el.textContent = `${rt.command}${rt.hint ? ` · ${rt.hint}` : ''}`;
}

function loadScriptFile(file) {
  if (!file) return;
  const name = (file.name || '').toLowerCase();
  const rt = (S.ui.scriptRuntimes || []).find((r) => (r.extensions || []).some((x) => name.endsWith(x)));
  if (rt && rt.available) $('#scriptLang').value = rt.key;
  const reader = new FileReader();
  reader.onload = () => {
    $('#scriptCode').value = String(reader.result || '');
    syncScriptRuntime();
    toast(`已载入 ${file.name}`, 'ok');
  };
  reader.onerror = () => toast('读取文件失败', 'bad');
  reader.readAsText(file, 'utf-8');
}

async function runScriptValidate() {
  const box = $('#scriptResult');
  const btn = $('#modal [data-act="script-run"]');
  const code = $('#scriptCode').value;
  if (!code.trim()) {
    toast('脚本内容为空', 'bad');
    return;
  }

  const timeout = parseFloat($('#scriptTimeout').value);
  if (btn) btn.disabled = true;
  box.innerHTML = '<div class="loading"><span class="spin"></span> 执行中…</div>';
  try {
    const result = await POST('/api/scripts/validate', {
      language: $('#scriptLang').value,
      code,
      stdin: $('#scriptStdin').value,
      timeout_sec: Number.isFinite(timeout) ? timeout : null,
    });
    box.innerHTML = renderScriptResult(result);
  } catch (err) {
    box.innerHTML = `<div class="issue">${esc(err.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderScriptResult(r) {
  const stage = { syntax: '静态语法检查', unavailable: '解释器不可用', run: '已执行' }[r.stage] || r.stage;
  const stdout = r.stdout
    ? `<pre class="script-out">${esc(r.stdout)}</pre>`
    : '<div class="desc">（stdout 为空）</div>';
  const stderr = r.stderr ? `<pre class="script-err">${esc(r.stderr)}</pre>` : '';
  const hints = (r.hints || []).map((h) => `<div class="issue sug">${esc(h)}</div>`).join('');

  return `
    <div class="row" style="margin:10px 0 8px">
      <span class="chip ${r.ok ? 'ok' : 'bad'}">${r.ok ? '验证通过' : '未通过'}</span>
      <span class="chip">${esc(stage)}</span>
      ${r.exit_code === null || r.exit_code === undefined ? '' : `<span class="chip">退出码 ${esc(r.exit_code)}</span>`}
      <span class="chip">${fmtMs(r.duration_ms)}</span>
      ${r.truncated ? '<span class="chip warn">输出已截断</span>' : ''}
    </div>
    ${r.error ? `<div class="issue">${esc(r.error)}</div>` : ''}
    ${r.syntax ? `<div class="issue">第 ${esc(r.syntax.line)} 行第 ${esc(r.syntax.column)} 列 · ${esc(r.syntax.message)}</div>` : ''}
    <div class="script-label">stdout</div>${stdout}
    ${stderr ? `<div class="script-label">stderr</div>${stderr}` : ''}
    ${hints}`;
}

/* ============================================================ 视图：运行记录 */

async function loadRuns(page = S.ui.listPage.runs) {
  const f = S.ui.runsFilter;
  const params = new URLSearchParams({ page: String(page), size: String(PAGE_SIZE) });
  if (f.status) params.set('status', f.status);
  if (f.keyword) params.set('keyword', f.keyword);
  S.runs = await GET(`/api/runs?${params}`);
  // 删除末页最后一条后会落到空页，自动回退一页
  if (!S.runs.runs.length && (S.runs.page || 1) > 1) {
    params.set('page', String(S.runs.page - 1));
    S.runs = await GET(`/api/runs?${params}`);
  }
  S.ui.listPage.runs = S.runs.page || 1;
}

function viewRuns() {
  const data = S.runs;
  if (!data) return '<div class="empty">无数据</div>';
  const f = S.ui.runsFilter;
  const st = data.stats;

  const kpis = [
    ['总运行', st.total, ''],
    ['成功', st.success, `成功率 ${(st.success_rate * 100).toFixed(1)}%`],
    ['失败', st.error, ''],
    ['平均耗时', fmtMs(st.avg_elapsed_ms), ''],
    ['平均切片', st.avg_chunks, ''],
  ];

  const rows = data.runs.length
    ? data.runs.map((r) => `
      <tr class="clickable" data-act="run-detail" data-run="${esc(r.run_id)}">
        <td class="mono">${esc(r.filename)}</td>
        <td>${statusChip(r.status)}</td>
        <td><span class="tag-mini">${esc(r.source)}</span></td>
        <td>${num(r.chunk_count)}</td>
        <td>${r.failed_steps ? `<span style="color:var(--danger)">${r.failed_steps}</span>` : '0'}</td>
        <td class="mono">${fmtMs(r.elapsed_ms)}</td>
        <td class="mono">${fmtTime(r.started_at)}</td>
        <td><button class="btn xs ghost danger" data-act="run-delete" data-run="${esc(r.run_id)}">删除</button></td>
      </tr>`).join('')
    : '<tr><td colspan="8"><div class="empty">没有匹配的运行记录</div></td></tr>';

  return `
    <div class="grid c4">${kpis.map(([k, v, x]) => `
      <div class="kpi"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="x">${esc(x)}</div></div>`).join('')}</div>

    <div class="panel" style="margin-top:16px">
      <header><h2>执行历史</h2>
        <div class="spacer"></div>
        <select class="input sm" style="width:110px" data-role="runs-status">
          <option value="" ${f.status === '' ? 'selected' : ''}>全部状态</option>
          <option value="success" ${f.status === 'success' ? 'selected' : ''}>仅成功</option>
          <option value="error" ${f.status === 'error' ? 'selected' : ''}>仅失败</option>
        </select>
        <input class="input sm" style="width:170px" placeholder="按文件名 / run_id 搜索"
               data-role="runs-keyword" value="${esc(f.keyword)}" />
        <button class="btn xs" data-act="runs-refresh">刷新</button>
        <button class="btn xs ghost danger" data-act="runs-clear">清空</button>
      </header>
      <table class="tbl"><thead><tr>
        <th>文件</th><th>状态</th><th>来源</th><th>切片</th><th>失败步</th><th>耗时</th><th>时间</th><th></th>
      </tr></thead><tbody>${rows}</tbody></table>
      ${renderPager(data.page, data.pages, 'runs-page', pagerLabel(data, data.total))}
    </div>`;
}

async function showRunDetail(runId) {
  S.ui.activeRun = runId;
  openModal({ title: '运行详情', wide: true, body: '<div class="loading"><span class="spin"></span> 加载中…</div>' });
  try {
    const data = await GET(`/api/runs/${encodeURIComponent(runId)}`);
    const rec = data.record;
    const s = data.stats || rec.stats;

    const trace = rec.trace.length
      ? rec.trace.map((t) => `
        <div class="trace-item ${t.status === 'error' ? 'err' : ''}">
          <span class="who">${esc(t.agent)} / ${esc(t.skill)}</span>
          <span class="detail">${esc(t.detail)}</span>
          <span class="ms">${fmtMs(t.duration_ms)}</span>
        </div>`).join('')
      : '<div class="desc">无轨迹</div>';

    const artifacts = rec.artifacts.length
      ? rec.artifacts.map((a) => `<span class="io-pill out">${esc(a.kind)} · ${esc(a.size)}</span>`).join(' ')
      : '<span class="desc">无产物</span>';

    const chunks = data.chunks.length
      ? data.chunks.map((c) => `
        <div class="chunk">
          <div class="meta">#${c.index} · ${esc(c.id)} · ${c.chars} 字符</div>
          <div class="txt">${esc(c.text)}</div>
        </div>`).join('')
      : `<div class="desc">${data.replayable ? '无切片' : '原始内容已不在内存中（服务重启或被淘汰）'}</div>`;

    openModal({
      title: `运行详情 · ${esc(rec.filename)}`,
      wide: true,
      body: `
        <div class="row" style="margin-bottom:12px">
          ${statusChip(rec.status)}
          <span class="chip">${esc(rec.source)}</span>
          <span class="chip">流水线 ${esc(rec.pipeline || '-')}</span>
          <span class="chip">${fmtTime(rec.started_at)}</span>
          <span class="chip">${fmtMs(rec.elapsed_ms)}</span>
        </div>
        <div class="grid c4" style="margin-bottom:14px">
          <div class="kpi"><div class="k">原始字符</div><div class="v">${num(s.chars)}</div></div>
          <div class="kpi"><div class="k">清洗后</div><div class="v">${num(s.clean_chars)}</div></div>
          <div class="kpi"><div class="k">切片</div><div class="v">${num(s.chunk_count)}</div></div>
          <div class="kpi"><div class="k">向量维度</div><div class="v">${num(s.embedding_dim)}</div></div>
        </div>
        ${rec.errors.length ? `<div class="issue">${rec.errors.map(esc).join('<br>')}</div>` : ''}
        <h3 style="font-size:12.5px;color:var(--muted);margin:10px 0 8px">产物总线</h3>
        <div class="row" style="gap:5px">${artifacts}</div>
        <h3 style="font-size:12.5px;color:var(--muted);margin:16px 0 8px">执行轨迹</h3>
        ${trace}
        <h3 style="font-size:12.5px;color:var(--muted);margin:16px 0 8px">切片内容</h3>
        ${chunks}
        <h3 style="font-size:12.5px;color:var(--muted);margin:16px 0 8px">检索验证</h3>
        <div class="row">
          <input class="input sm" id="runQuery" placeholder="输入问题验证检索链路" style="flex:1" />
          <button class="btn xs primary" data-act="run-search" data-run="${esc(rec.run_id)}">检索</button>
        </div>
        <div id="runHits"></div>`,
      footer: `
        <button class="btn xs" data-act="run-replay" data-run="${esc(rec.run_id)}" ${data.replayable ? '' : 'disabled'}>重放</button>
        <button class="btn xs danger" data-act="run-delete" data-run="${esc(rec.run_id)}">删除记录</button>
        <button class="btn xs primary" data-act="modal-close">关闭</button>`,
    });
  } catch (err) {
    openModal({ title: '运行详情', body: `<div class="issue">${esc(err.message)}</div>` });
  }
}

async function runSearch(runId) {
  const query = $('#runQuery').value.trim();
  if (!query) return;
  const box = $('#runHits');
  box.innerHTML = '<div class="loading"><span class="spin"></span> 检索中…</div>';
  try {
    const data = await POST('/api/search', { run_id: runId, query });
    box.innerHTML = `
      <div class="desc" style="font-size:11.5px;margin-top:8px">
        embedder=${esc(data.embedder)} · dim=${data.dimension} · top_k=${data.top_k} · 阈值=${data.score_threshold}
      </div>
      ${data.hits.map((h) => `
        <div class="hit">
          <span class="score">${h.score}</span>
          <div class="cid">#${h.index} · ${esc(h.chunk_id)}</div>
          <div>${esc(h.text)}</div>
        </div>`).join('') || '<div class="desc">无命中</div>'}`;
  } catch (err) {
    box.innerHTML = `<div class="issue">${esc(err.message)}</div>`;
  }
}

/* ============================================================ 视图：知识库 */

async function loadIndexes(page = S.ui.listPage.knowledge) {
  let current = page;
  S.knowledge = await GET(`/api/knowledge/indexes?page=${current}&size=${PAGE_SIZE}`);
  // 删除末页最后一条后会落到空页，自动回退一页
  if (!S.knowledge.indexes.length && (S.knowledge.page || 1) > 1) {
    current = S.knowledge.page - 1;
    S.knowledge = await GET(`/api/knowledge/indexes?page=${current}&size=${PAGE_SIZE}`);
  }
  S.ui.listPage.knowledge = S.knowledge.page || 1;
}

function viewKnowledge() {
  const data = S.knowledge;
  if (!data) return '<div class="empty">无数据</div>';

  const rows = data.indexes.length
    ? data.indexes.map((i) => `
      <tr>
        <td class="mono">${esc(i.run_id)}</td>
        <td class="mono">${esc(i.collection || 'default')}</td>
        <td class="mono">${esc(i.embedder || '-')}</td>
        <td>${num(i.dimension)}</td>
        <td>${num(i.size)}</td>
        <td class="mono">${fmtTime(i.created_at)}</td>
        <td>
          <button class="btn xs" data-act="kb-search" data-run="${esc(i.run_id)}">检索</button>
          <button class="btn xs ghost danger" data-act="kb-delete" data-run="${esc(i.run_id)}">删除</button>
        </td>
      </tr>`).join('')
    : '<tr><td colspan="7"><div class="empty">还没有向量索引，先上传一个文件</div></td></tr>';

  return `
    <div class="grid c3">
      <div class="kpi"><div class="k">索引数量</div><div class="v">${num(data.count)}</div></div>
      <div class="kpi"><div class="k">向量总数</div><div class="v">${num(data.total_vectors)}</div></div>
      <div class="kpi"><div class="k">存储后端</div><div class="v" style="font-size:15px">内存向量仓库</div></div>
    </div>

    <div class="panel" style="margin-top:16px">
      <header><h2>向量索引</h2><div class="spacer"></div>
        <button class="btn xs" data-act="goto" data-hash="#/orchestration">去上传文件</button></header>
      <table class="tbl"><thead><tr>
        <th>run_id</th><th>集合</th><th>向量化插件</th><th>维度</th><th>向量数</th><th>创建时间</th><th></th>
      </tr></thead><tbody>${rows}</tbody></table>
      ${renderPager(data.page, data.pages, 'kb-page', pagerLabel(data, data.total ?? data.count))}
    </div>

    <div class="panel" id="kbSearchPanel" style="display:none">
      <header><h2>检索</h2><div class="desc" id="kbSearchMeta"></div></header>
      <div class="row">
        <input class="input sm" id="kbQuery" placeholder="输入查询语句" style="flex:1;min-width:200px" />
        <button class="btn xs primary" data-act="kb-search-run">检索</button>
      </div>
      <div id="kbHits"></div>
    </div>`;
}

async function kbSearch(runId) {
  const panel = $('#kbSearchPanel');
  panel.style.display = '';
  panel.dataset.run = runId;
  $('#kbSearchMeta').textContent = `索引 ${runId}`;
  $('#kbHits').innerHTML = '';
  $('#kbQuery').focus();
  $('#kbQuery').scrollIntoView({ behavior: 'smooth', block: 'center' });
}

async function kbSearchRun() {
  const runId = $('#kbSearchPanel').dataset.run;
  const query = $('#kbQuery').value.trim();
  if (!query) return;
  const box = $('#kbHits');
  box.innerHTML = '<div class="loading"><span class="spin"></span> 检索中…</div>';
  try {
    const data = await POST('/api/knowledge/search', { run_id: runId, query });
    box.innerHTML = `
      <div class="desc" style="font-size:11.5px;margin-top:8px">
        embedder=${esc(data.embedder)} · dim=${data.dimension} · top_k=${data.top_k} · 阈值=${data.score_threshold}
        ${data.filtered_out ? ` · 被阈值过滤 ${data.filtered_out} 条` : ''}
      </div>
      ${data.hits.map((h) => `
        <div class="hit"><span class="score">${h.score}</span>
          <div class="cid">#${h.index} · ${esc(h.chunk_id)}</div>
          <div>${esc(h.text)}</div></div>`).join('') || '<div class="desc">无命中</div>'}`;
  } catch (err) {
    box.innerHTML = `<div class="issue">${esc(err.message)}</div>`;
  }
}

/* ============================================================ 视图：系统设置 */

function viewSettings() {
  const data = S.settings;
  if (!data) return '<div class="empty">无数据</div>';

  const groups = Object.entries(data.schema).map(([group, schema]) => `
    <div class="panel">
      <header><h2>${esc(schema.label)}</h2><div class="desc">${esc(schema.description)}</div></header>
      <div class="grid c2">
        ${Object.entries(schema.fields).map(([key, spec]) =>
          settingsField(group, key, spec, data.values[group] ? data.values[group][key] : undefined)).join('')}
      </div>
    </div>`).join('');

  const providers = data.providers.length
    ? data.providers.map((p) => `
      <div class="profile-card">
        <span class="chip ${p.enabled ? 'ok' : ''}">${p.enabled ? '启用' : '停用'}</span>
        <div style="flex:1;min-width:120px">
          <div style="font-weight:600">${esc(p.name)}</div>
          <div class="desc" style="font-size:11px">${esc(p.kind)} · ${esc(p.base_url || '默认地址')} · ${p.models.length} 个模型</div>
        </div>
        <span class="tag-mini">${p.has_key ? '已配置密钥' : '无密钥'}</span>
        <button class="btn xs" data-act="provider-edit" data-id="${esc(p.id)}">编辑</button>
        <button class="btn xs ghost danger" data-act="provider-del" data-id="${esc(p.id)}">删除</button>
      </div>`).join('')
    : '<div class="empty">还没有配置模型供应商</div>';

  return `
    <div class="panel">
      <header><h2>配置文件</h2><div class="spacer"></div>
        <button class="btn xs" data-act="export-config">导出配置</button>
        <button class="btn xs ghost danger" data-act="settings-reset">恢复默认</button>
        <button class="btn xs primary" data-act="settings-save">保存设置</button>
      </header>
      <div class="kv">
        <div class="k">设置文件</div><div class="v mono">${esc(data.meta.settings_path || '-')}</div>
        <div class="k">运行记录</div><div class="v mono">${esc(data.meta.runs_path || '-')}</div>
        <div class="k">流水线配置</div><div class="v mono">${esc(data.meta.config_path || '-')}</div>
        <div class="k">外部插件目录</div><div class="v mono">${esc(data.meta.ext_plugins_dir || '-')}</div>
        <div class="k">方案目录</div><div class="v mono">${esc(data.meta.profiles_dir || '-')}</div>
      </div>
    </div>

    ${groups}

    <div class="panel">
      <header><h2>模型供应商</h2>
        <div class="desc">API Key 仅存储于服务端，读取时统一掩码</div>
        <div class="spacer"></div>
        <button class="btn xs" data-act="provider-add">+ 添加供应商</button>
      </header>
      <div class="grid" style="gap:8px">${providers}</div>
    </div>`;
}

function settingsField(group, key, spec, value) {
  const attrs = `data-setgroup="${esc(group)}" data-setkey="${esc(key)}"`;
  const label = esc(spec.label || key);
  const help = spec.help ? `<div class="help">${esc(spec.help)}</div>` : '';
  let control;

  if (spec.type === 'bool') {
    return `<div class="check" style="padding-top:4px">
      <input type="checkbox" ${attrs} ${value ? 'checked' : ''}/><span>${label}</span>
      ${help}</div>`;
  }
  if (spec.type === 'list') {
    const text = Array.isArray(value) ? value.join(', ') : (value || '');
    control = `<input class="input" ${attrs} value="${esc(text)}" placeholder="逗号分隔"/>`;
  } else if (spec.choices) {
    const labels = spec.choice_labels || {};   // 可选的中文选项名（如「仅管理员」）
    control = `<select class="input" ${attrs}>${spec.choices.map((c) =>
      `<option value="${esc(c)}" ${String(c) === String(value) ? 'selected' : ''}>${esc(labels[c] || c)}</option>`).join('')}</select>`;
  } else if (spec.type === 'int' || spec.type === 'float') {
    control = `<input class="input" type="number" ${attrs} value="${value ?? ''}"
      ${spec.min !== undefined ? `min="${spec.min}"` : ''} ${spec.max !== undefined ? `max="${spec.max}"` : ''}/>`;
  } else {
    control = `<input class="input" ${attrs} value="${esc(value ?? '')}"/>`;
  }
  return `<div><label class="lbl">${label}</label>${control}${help}</div>`;
}

function collectSettings() {
  const payload = {};
  $$('#view [data-setgroup]').forEach((el) => {
    const group = el.dataset.setgroup;
    const key = el.dataset.setkey;
    payload[group] = payload[group] || {};
    if (el.type === 'checkbox') payload[group][key] = el.checked;
    else if (el.type === 'number') payload[group][key] = el.value.includes('.') ? parseFloat(el.value) : parseInt(el.value, 10);
    else if (key === 'disabled' || key === 'allowed_extensions') {
      payload[group][key] = el.value.split(',').map((s) => s.trim()).filter(Boolean);
    } else payload[group][key] = el.value;
  });
  return payload;
}

async function saveSettings() {
  try {
    S.settings = await PUT('/api/settings', collectSettings());
    await refreshAuthConfig();          // 「登录与安全」可能刚被改动
    renderChrome();
    await refreshOverview();
    toast('设置已保存', 'ok');
    if (needsGate()) { showGate(); return; }
    await route();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

function providerForm(provider) {
  const p = provider || { name: '', kind: 'openai', base_url: '', api_key: '', models: [], enabled: true };
  return `
    <div class="field"><label class="lbl">名称</label>
      <input class="input" id="pvName" value="${esc(p.name)}" placeholder="例如：OpenAI 官方"/></div>
    <div class="field"><label class="lbl">类型</label>
      <select class="input" id="pvKind">${['openai', 'azure_openai', 'ollama', 'custom'].map((k) =>
        `<option value="${k}" ${k === p.kind ? 'selected' : ''}>${k}</option>`).join('')}</select></div>
    <div class="field"><label class="lbl">Base URL</label>
      <input class="input" id="pvBase" value="${esc(p.base_url)}" placeholder="留空使用官方地址"/>
      <div class="help">必须以 http:// 或 https:// 开头</div></div>
    <div class="field"><label class="lbl">API Key</label>
      <input class="input" id="pvKey" value="${esc(p.api_key)}" placeholder="sk-..."/>
      <div class="help">${p.has_key ? '留空或保持掩码表示不修改现有密钥' : '仅存于服务端'}</div></div>
    <div class="field"><label class="lbl">模型列表</label>
      <input class="input" id="pvModels" value="${esc((p.models || []).join(', '))}" placeholder="逗号分隔"/>
    </div>
    <label class="check"><input type="checkbox" id="pvEnabled" ${p.enabled ? 'checked' : ''}/><span>启用该供应商</span></label>`;
}

function openProviderModal(provider) {
  S.ui.providerDraft = provider ? provider.id : '';
  openModal({
    title: provider ? `编辑供应商 · ${esc(provider.name)}` : '新增模型供应商',
    body: providerForm(provider),
    footer: `<button class="btn xs primary" data-act="provider-save">保存</button>
             <button class="btn xs" data-act="modal-close">取消</button>`,
  });
}

async function saveProvider() {
  const payload = {
    id: S.ui.providerDraft || undefined,
    name: $('#pvName').value.trim(),
    kind: $('#pvKind').value,
    base_url: $('#pvBase').value.trim(),
    api_key: $('#pvKey').value,
    models: $('#pvModels').value.split(',').map((s) => s.trim()).filter(Boolean),
    enabled: $('#pvEnabled').checked,
  };
  try {
    const result = await POST('/api/settings/providers', payload);
    S.settings = await GET('/api/settings');
    S.settings.providers = result.providers;
    closeModal();
    toast('供应商已保存', 'ok');
    await route();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

/* ============================================================ 视图：个人中心 */

function viewProfile() {
  const u = Auth.user;
  if (!u) {
    return `<div class="panel"><div class="issue">当前未登录。请点击左下角用户卡或「登录账号」进入。</div></div>`;
  }
  const draft = S.ui.profileDraft || {
    avatar: u.avatar || '', color: u.color || '#5b8cff',
    nickname: u.nickname || '', email: u.email || '', bio: u.bio || '',
  };
  const sessions = S.ui.sessions;
  const roleChip = u.role === 'admin' ? '<span class="badge-role">管理员</span>' : '<span class="tag-mini">普通用户</span>';

  const sessionRows = (sessions && sessions.sessions && sessions.sessions.length)
    ? sessions.sessions.map((s) => `
      <div class="session-row">
        <span class="chip ${s.current ? 'ok' : ''}">${s.current ? '当前设备' : '其他设备'}</span>
        <div style="flex:1;min-width:120px">
          <div class="mono" style="font-size:11px">${esc(String(s.jti || '').slice(0, 14))}…</div>
          <div class="desc" style="font-size:10.5px">最近活动 ${fmtTime(s.last_seen)} · 过期 ${fmtTime(s.expires_at)}</div>
        </div>
        ${s.current ? '<span class="tag-mini">本次登录</span>' : '<span class="desc" style="font-size:10.5px">可被强制下线</span>'}
      </div>`).join('')
    : '<div class="empty">暂无活跃会话</div>';

  return `
    <div class="panel">
      <div class="profile-hero">
        ${avatarHtml(u, 'lg')}
        <div class="who">
          <h2>${esc(u.display_name)} ${roleChip}</h2>
          <div class="handle">@${esc(u.username)}</div>
          <div class="bio">${esc(u.bio || '这个人很低调，还没有填写简介。')}</div>
          <div class="stat-row">
            <div class="item"><div class="k">邮箱</div><div class="v" style="font-size:13px">${esc(u.email || '-')}</div></div>
            <div class="item"><div class="k">最近登录</div><div class="v" style="font-size:13px">${fmtTime(u.last_login_at)}</div></div>
            <div class="item"><div class="k">注册时间</div><div class="v" style="font-size:13px">${fmtTime(u.created_at)}</div></div>
            <div class="item"><div class="k">活跃会话</div><div class="v">${sessions ? sessions.count : 1}</div></div>
          </div>
        </div>
        ${u.must_change_password ? '<span class="chip warn"><i class="dot"></i>请尽快修改默认密码</span>' : ''}
      </div>
    </div>

    <div class="grid c2">
      <div class="panel">
        <header><h2>个人资料</h2><div class="desc">昵称与头像会展示在所有界面</div></header>
        <div class="field">
          <label class="lbl">头像</label>
          <div class="avatar-picker">
            ${AVATAR_CHOICES.map((a) => `
              <button class="avatar-opt ${draft.avatar === a ? 'active' : ''}" type="button"
                data-act="pick-avatar" data-value="${esc(a)}">${esc(a)}</button>`).join('')}
          </div>
        </div>
        <div class="field">
          <label class="lbl">主题色</label>
          <div class="avatar-picker">
            ${COLOR_CHOICES.map((c) => `
              <button class="color-opt ${draft.color === c ? 'active' : ''}" type="button"
                style="background:${c}" title="${c}" data-act="pick-color" data-value="${esc(c)}"></button>`).join('')}
          </div>
        </div>
        <div class="field"><label class="lbl">昵称</label>
          <input class="input" id="pfNick" value="${esc(draft.nickname || '')}" placeholder="展示用的名字" /></div>
        <div class="field"><label class="lbl">邮箱</label>
          <input class="input" id="pfEmail" value="${esc(draft.email || '')}" placeholder="name@example.com" /></div>
        <div class="field"><label class="lbl">简介</label>
          <textarea class="input" id="pfBio" rows="3" placeholder="一句话介绍自己">${esc(draft.bio || '')}</textarea></div>
        <div class="row"><button class="btn primary" data-act="profile-save">保存资料</button></div>
      </div>

      <div class="panel">
        <header><h2>修改密码</h2><div class="desc">建议定期更换，改密后可强制下线其他设备</div></header>
        <div class="field"><label class="lbl">当前密码</label>
          <input class="input" id="pwCurrent" type="password" autocomplete="current-password" /></div>
        <div class="field"><label class="lbl">新密码</label>
          <input class="input" id="pwNew" type="password" autocomplete="new-password" placeholder="至少 6 位" /></div>
        <div class="field"><label class="lbl">确认新密码</label>
          <input class="input" id="pwConfirm" type="password" autocomplete="new-password" /></div>
        <label class="check"><input type="checkbox" id="pwLogout" checked /><span>同时退出其他设备的登录</span></label>
        <div class="row" style="margin-top:12px"><button class="btn primary" data-act="password-save">更新密码</button></div>
      </div>
    </div>

    <div class="panel">
      <header><h2>登录会话</h2><div class="desc">共 ${sessions ? sessions.count : 0} 个活跃会话</div>
        <div class="spacer"></div>
        <button class="btn xs" data-act="sessions-refresh">刷新</button>
        <button class="btn xs ghost danger" data-act="sessions-revoke-others">退出其他设备</button>
      </header>
      ${sessionRows}
    </div>`;
}

async function saveProfile() {
  const draft = S.ui.profileDraft || {};
  try {
    const data = await PUT('/api/auth/profile', {
      nickname: $('#pfNick').value.trim(),
      email: $('#pfEmail').value.trim(),
      bio: $('#pfBio').value.trim(),
      avatar: draft.avatar || '',
      color: draft.color || '',
    });
    Auth.user = data.user;
    renderChrome();
    toast('资料已更新', 'ok');
    await route();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

async function savePassword() {
  const current = $('#pwCurrent').value;
  const next = $('#pwNew').value;
  const confirmPass = $('#pwConfirm').value;
  if (!current || !next) { toast('请填写当前密码与新密码', 'bad'); return; }
  if (next !== confirmPass) { toast('两次输入的新密码不一致', 'bad'); return; }
  try {
    const data = await PUT('/api/auth/password', {
      current_password: current,
      new_password: next,
      logout_others: $('#pwLogout').checked,
    });
    Auth.user = data.user;
    renderChrome();
    toast('密码已更新', 'ok');
    S.ui.sessions = await GET('/api/auth/sessions');
    await route();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

/* ============================================================ 视图：主题外观 */

function themePill(axis, value, label) {
  return `<button class="pill-opt ${theme[axis] === value ? 'active' : ''}" type="button"
    data-act="theme-set" data-axis="${axis}" data-value="${esc(value)}">${esc(label)}</button>`;
}

function modeSwatch(mode) {
  const pair = { dark: ['#0d1424', '#111a2e'], light: ['#eef1f8', '#ffffff'], auto: ['#0d1424', '#ffffff'] }[mode];
  return `<span class="preview"><i style="background:${pair[0]}"></i><i style="background:${pair[1]}"></i></span>`;
}

function viewTheme() {
  const modes = [['dark', '深色'], ['light', '浅色'], ['auto', '跟随系统']];
  const radii = [['sharp', '紧凑'], ['standard', '标准'], ['round', '圆润']];
  const densities = [['compact', '紧凑'], ['standard', '标准'], ['relaxed', '宽松']];
  const glasses = [['on', '开启'], ['off', '关闭']];

  return `
  <div class="theme-grid">
    <div>
      <div class="panel">
        <header><h2>深浅模式</h2><div class="desc">「跟随系统」会随操作系统外观自动切换</div></header>
        <div class="theme-option">
          ${modes.map(([v, label]) => `
            <button class="swatch ${theme.mode === v ? 'active' : ''}" type="button"
              data-act="theme-set" data-axis="mode" data-value="${v}">
              ${modeSwatch(v)}<span>${label}</span>
            </button>`).join('')}
        </div>
      </div>

      <div class="panel">
        <header><h2>强调色</h2><div class="desc">按钮、选中态与背景光晕的基准色</div></header>
        <div class="theme-option" style="align-items:center">
          ${Object.entries(ACCENT_COLORS).map(([v, c]) => `
            <button class="accent-dot ${theme.accent === v ? 'active' : ''}" type="button"
              style="background:${c}" title="${v}" data-act="theme-set" data-axis="accent" data-value="${v}"></button>`).join('')}
          <span class="desc" style="font-size:11.5px">当前：${esc(theme.accent)}</span>
        </div>
      </div>

      <div class="panel">
        <header><h2>圆角</h2></header>
        <div class="theme-option">${radii.map(([v, l]) => themePill('radius', v, l)).join('')}</div>
      </div>

      <div class="panel">
        <header><h2>界面密度</h2></header>
        <div class="theme-option">${densities.map(([v, l]) => themePill('density', v, l)).join('')}</div>
      </div>

      <div class="panel">
        <header><h2>背景光晕</h2><div class="desc">开启后使用毛玻璃与强调色渐晕作为背景</div></header>
        <div class="theme-option">${glasses.map(([v, l]) => themePill('glass', v, l)).join('')}</div>
      </div>

      <div class="panel">
        <header><h2>同步与重置</h2>
          <div class="desc">${Auth.user ? '偏好会跟随账号保存，换设备登录同样生效' : '当前为访客模式，偏好只保存在本机浏览器'}</div>
          <div class="spacer"></div>
          <button class="btn xs ghost danger" data-act="theme-reset">恢复默认</button>
        </header>
        <div class="row" style="gap:8px">
          ${Auth.user
            ? '<span class="chip ok"><i class="dot"></i>已绑定账号</span>'
            : '<span class="chip"><i class="dot"></i>仅本地</span>'}
          <span class="desc" style="font-size:11.5px">默认：深色 / 蓝色 / 标准圆角 / 标准密度 / 光晕开启</span>
        </div>
      </div>
    </div>

    <aside>
      <div class="panel">
        <header><h2>实时预览</h2></header>
        <div class="preview-card">
          <div class="mini-nav">
            <span class="on">概览</span><span>编排工作台</span><span>技能库</span>
          </div>
          <div class="mini-kpis">
            <div class="mini-kpi"><div class="k" style="font-size:10px;color:var(--muted-2)">运行次数</div><b>128</b></div>
            <div class="mini-kpi"><div class="k" style="font-size:10px;color:var(--muted-2)">成功率</div><b>96.4%</b></div>
          </div>
          <div class="row" style="gap:6px">
            <span class="mini-btn">应用生效</span>
            <span class="btn xs">次要按钮</span>
          </div>
          <div class="row" style="gap:6px;margin-top:10px">
            <span class="chip ok"><i class="dot"></i>契约完整</span>
            <span class="chip warn"><i class="dot"></i>草稿未生效</span>
          </div>
        </div>
      </div>
      <div class="panel">
        <header><h2>说明</h2></header>
        <div class="desc" style="font-size:11.5px;line-height:1.7">
          主题通过 <code>&lt;html&gt;</code> 上的 <code>data-*</code> 属性驱动 CSS 变量，切换即时生效。
          ${Auth.user ? '登录状态下的改动会写入你的账号偏好。' : '登录后即可让偏好跟随账号同步。'}
        </div>
      </div>
    </aside>
  </div>`;
}

/* ============================================================ 视图：用户管理 */

function viewUsers() {
  const data = S.users;
  if (!data) return '<div class="empty">无数据</div>';
  const me = Auth.user && Auth.user.id;

  const rows = data.users.map((u) => `
    <tr>
      <td>
        <div class="row" style="gap:8px;flex-wrap:nowrap">
          ${avatarHtml(u, 'sm')}
          <div style="min-width:0">
            <div style="font-weight:600">${esc(u.display_name)}</div>
            <div class="desc mono" style="font-size:10.5px">@${esc(u.username)}</div>
          </div>
        </div>
      </td>
      <td>${u.role === 'admin' ? '<span class="badge-role">管理员</span>' : '<span class="tag-mini">普通用户</span>'}</td>
      <td>${u.active ? '<span class="chip ok">正常</span>' : '<span class="chip bad">已停用</span>'}</td>
      <td class="mono">${fmtTime(u.last_login_at)}</td>
      <td class="mono">${fmtTime(u.created_at)}</td>
      <td>
        <button class="btn xs" data-act="user-edit" data-id="${esc(u.id)}">编辑</button>
        <button class="btn xs ghost danger" data-act="user-del" data-id="${esc(u.id)}" ${u.id === me ? 'disabled' : ''}>删除</button>
      </td>
    </tr>`).join('');

  return `
    <div class="grid c3">
      <div class="kpi"><div class="k">账号总数</div><div class="v">${num(data.counts.total)}</div></div>
      <div class="kpi"><div class="k">启用中</div><div class="v">${num(data.counts.active)}</div></div>
      <div class="kpi"><div class="k">管理员</div><div class="v">${num(data.counts.admins)}</div></div>
    </div>

    <div class="panel" style="margin-top:16px">
      <header><h2>账号列表</h2><div class="desc">管理员可创建、编辑、停用或删除账号</div>
        <div class="spacer"></div>
        <button class="btn xs" data-act="users-refresh">刷新</button>
        <button class="btn xs primary" data-act="user-add">+ 新建账号</button>
      </header>
      <table class="tbl"><thead><tr>
        <th>用户</th><th>角色</th><th>状态</th><th>最近登录</th><th>创建时间</th><th></th>
      </tr></thead><tbody>${rows || '<tr><td colspan="6"><div class="empty">暂无账号</div></td></tr>'}</tbody></table>
    </div>`;
}

function userForm(user) {
  if (user) {
    return `
      <div class="field"><label class="lbl">用户名</label>
        <input class="input" value="${esc(user.username)}" disabled /></div>
      <div class="field"><label class="lbl">昵称</label>
        <input class="input" id="ufNick" value="${esc(user.nickname || '')}" /></div>
      <div class="field"><label class="lbl">邮箱</label>
        <input class="input" id="ufEmail" value="${esc(user.email || '')}" /></div>
      <div class="field"><label class="lbl">角色</label>
        <select class="input" id="ufRole">
          <option value="user" ${user.role === 'user' ? 'selected' : ''}>普通用户</option>
          <option value="admin" ${user.role === 'admin' ? 'selected' : ''}>管理员</option>
        </select></div>
      <div class="field"><label class="lbl">重置密码（留空表示不修改）</label>
        <input class="input" id="ufPass" type="password" placeholder="至少 6 位" />
        <div class="help">重置后该账号会被强制重新登录</div></div>
      <label class="check"><input type="checkbox" id="ufActive" ${user.active ? 'checked' : ''} /><span>账号启用</span></label>`;
  }
  return `
    <div class="field"><label class="lbl">用户名</label>
      <input class="input" id="ufUser" placeholder="3-32 位字母 / 数字 / _ - ." /></div>
    <div class="field"><label class="lbl">初始密码</label>
      <input class="input" id="ufPass" type="password" placeholder="至少 6 位" /></div>
    <div class="field"><label class="lbl">昵称</label>
      <input class="input" id="ufNick" placeholder="展示用的名字" /></div>
    <div class="field"><label class="lbl">邮箱</label>
      <input class="input" id="ufEmail" placeholder="name@example.com" /></div>
    <div class="field"><label class="lbl">角色</label>
      <select class="input" id="ufRole">
        <option value="user">普通用户</option>
        <option value="admin">管理员</option>
      </select></div>
    <label class="check"><input type="checkbox" id="ufMust" /><span>要求首次登录后修改密码</span></label>`;
}

function openUserModal(user) {
  S.ui.editingUser = user ? user.id : '';
  openModal({
    title: user ? `编辑账号 · ${esc(user.username)}` : '新建账号',
    body: userForm(user),
    footer: `<button class="btn xs primary" data-act="user-save">保存</button>
             <button class="btn xs" data-act="modal-close">取消</button>`,
  });
}

async function saveUser() {
  const id = S.ui.editingUser;
  try {
    let result;
    if (id) {
      const patch = {
        nickname: $('#ufNick').value.trim(),
        email: $('#ufEmail').value.trim(),
        role: $('#ufRole').value,
        active: $('#ufActive').checked,
      };
      const pass = $('#ufPass').value;
      if (pass) patch.new_password = pass;
      result = await PATCH(`/api/auth/users/${encodeURIComponent(id)}`, patch);
    } else {
      result = await POST('/api/auth/users', {
        username: $('#ufUser').value.trim(),
        password: $('#ufPass').value,
        nickname: $('#ufNick').value.trim(),
        email: $('#ufEmail').value.trim(),
        role: $('#ufRole').value,
        must_change_password: $('#ufMust').checked,
      });
    }
    S.users = result;
    closeModal();
    toast('账号已保存', 'ok');
    if (currentRoute === '#/users') $('#view').innerHTML = viewUsers();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

/* ============================================================ 全局动作 */

async function loadPipelines(page = 1) {
  S.pipelines = await GET(`/api/pipelines?page=${page}&size=10`);
  S.ui.pipelinePage = page;
}

function openPipelineCreateModal() {
  openModal({
    title: '新建流水线任务',
    body: `
      <div class="field">
        <label class="lbl" for="pcName">任务名称</label>
        <input class="input" id="pcName" placeholder="字母 / 数字 / _ / -，如 data-cleaning" maxlength="40" />
        <div class="help">保存后即可在该任务下添加专属阶段（Agent）与功能步骤。</div>
      </div>
      <div class="field">
        <label class="lbl" for="pcDesc">描述（可选）</label>
        <input class="input" id="pcDesc" placeholder="一句话说明这条流水线的业务用途" maxlength="255" />
      </div>
      <div class="field">
        <label class="lbl">创建方式</label>
        <label class="check" style="margin-bottom:6px">
          <input type="radio" name="pcMode" value="clone" checked />
          <span>复制当前流水线的阶段结构作为起点</span>
        </label>
        <label class="check">
          <input type="radio" name="pcMode" value="blank" />
          <span>创建空白任务，从零开始编排</span>
        </label>
      </div>
    `,
    footer: `<button class="btn xs primary" data-act="pipeline-create-submit">创建</button>
             <button class="btn xs" data-act="modal-close">取消</button>`,
  });
}

async function submitPipelineCreate() {
  const name = $('#pcName').value.trim();
  const description = $('#pcDesc').value.trim();
  const blank = $('[name="pcMode"]:checked').value === 'blank';
  if (!name) {
    toast('请输入任务名称', 'bad');
    return;
  }
  try {
    await POST('/api/pipelines', { name, description, blank });
    closeModal();
    toast(`流水线任务 ${name} 已创建`, 'ok');
    await refreshOrch();
  } catch (err) {
    toast(err.message, 'bad');
  }
}

async function refreshOrch() {
  S.orch = await GET('/api/orchestration');
  await loadPipelines(S.ui.pipelinePage || 1);
  await refreshOverview();
  if (currentRoute === '#/orchestration') $('#view').innerHTML = viewOrchestration();
}

async function orchAction(path, body, method) {
  try {
    const data = await request(`/api/orchestration${path}`, { method: method || 'POST', body: body === undefined ? undefined : body });
    await refreshOrch();
    return data;
  } catch (err) {
    toast(err.message, 'bad');
    return null;
  }
}

/** 切换当前配置的阶段（如新建阶段后自动聚焦到它） */
function selectAgent(agentId) {
  if (agentId) S.ui.selectedAgent = agentId;
  S.ui.addStepFor = null;
  if (currentRoute === '#/orchestration' && S.orch) $('#view').innerHTML = viewOrchestration();
}

/* ------------------------------------------------------------ 执行链路：顺序演示动效 */

let flowTimers = [];

/** 清掉全部演示定时器（切换视图 / 重渲染前调用） */
function clearFlowTimers() {
  flowTimers.forEach((t) => clearTimeout(t));
  flowTimers = [];
}

function flowDom() {
  const map = $('#flowMap');
  if (!map) return null;
  return {
    map,
    nodes: Array.from(map.querySelectorAll('.flow-node')),
    arrows: Array.from(map.querySelectorAll('.flow-arrow')),
  };
}

function setFlowStatus(text) {
  const el = $('#flowStatus');
  if (el) el.textContent = text || '';
}

function setFlowButton() {
  const btn = $('#flowPlayBtn');
  if (!btn) return;
  btn.textContent = S.ui.flowPlaying ? '■ 停止演示' : '▶ 演示执行顺序';
  btn.dataset.act = S.ui.flowPlaying ? 'flow-stop' : 'play-flow';
}

/** 复位演示状态（节点上的真实耗时标注保留） */
function stopFlow() {
  clearFlowTimers();
  S.ui.flowPlaying = false;
  const dom = flowDom();
  if (dom) {
    dom.map.classList.remove('playing');
    dom.nodes.forEach((n) => n.classList.remove('running', 'done'));
    dom.arrows.forEach((a) => a.classList.remove('lit'));
  }
  setFlowStatus('');
  setFlowButton();
}

/**
 * 按执行顺序演示整条链路：节点依次进入 running → done，连线同步点亮。
 * realtime=true 且已有试跑耗时时，每段停留时长按真实耗时比例缩放（340~1000ms）。
 */
function playFlow(opts = {}) {
  const dom = flowDom();
  if (!dom || !dom.nodes.length) return;
  clearFlowTimers();
  const { map, nodes, arrows } = dom;
  S.ui.flowPlaying = true;
  setFlowButton();
  map.classList.add('playing');
  nodes.forEach((n) => n.classList.remove('running', 'done'));
  arrows.forEach((a) => a.classList.remove('lit'));

  const times = S.ui.flowTimes || {};
  const values = nodes.map((n) => Number(times[n.dataset.agent]) || 0);
  const maxValue = Math.max(0, ...values);
  const realtime = !!opts.realtime && maxValue > 0;
  const stepMs = (i) => (realtime ? Math.round(340 + (values[i] / maxValue) * 660) : 620);

  let at = 200;
  nodes.forEach((node, i) => {
    flowTimers.push(setTimeout(() => {
      nodes.forEach((n, j) => {
        n.classList.toggle('done', j < i);
        n.classList.toggle('running', j === i);
      });
      arrows.forEach((a, j) => a.classList.toggle('lit', j < i));
      const name = node.querySelector('.node-name');
      setFlowStatus(`执行中 ${i + 1}/${nodes.length} · ${name ? name.textContent : ''}`);
    }, at));
    at += stepMs(i);
  });

  flowTimers.push(setTimeout(() => {
    nodes.forEach((n) => { n.classList.remove('running'); n.classList.add('done'); });
    arrows.forEach((a) => a.classList.add('lit'));
    setFlowStatus(`链路执行完成 · ${nodes.length} 个阶段`);
  }, at));

  flowTimers.push(setTimeout(() => {
    nodes.forEach((n) => n.classList.remove('done'));
    arrows.forEach((a) => a.classList.remove('lit'));
    S.ui.flowPlaying = false;
    setFlowStatus('');
    setFlowButton();
  }, at + 1100));
}

/** 从运行轨迹聚合「阶段 → 耗时」，用于按真实节奏回放 */
function flowTimesFromRun(run) {
  if (!run || !Array.isArray(run.trace)) return null;
  const times = {};
  run.trace.forEach((t) => {
    if (!t || !t.agent) return;
    times[t.agent] = (times[t.agent] || 0) + Number(t.duration_ms || 0);
  });
  return Object.keys(times).length ? times : null;
}

async function doUpload(file) {
  const status = $('#uploadStatus');
  if (status) status.innerHTML = `<span class="spin"></span> 正在处理 ${esc(file.name)} …`;
  const form = new FormData();
  form.append('file', file);
  try {
    const result = await UPLOAD('/api/upload?use=draft', form);
    if (status) status.innerHTML = `✅ 完成，耗时 ${fmtMs(result.elapsed_ms)}，切片 ${result.stats.chunk_count}`;
    toast(`试跑成功：${result.stats.chunk_count} 个切片`, 'ok');

    // 把真实耗时标注到执行链路上，并按真实节奏回放一遍执行顺序
    const times = flowTimesFromRun(result);
    if (times) S.ui.flowTimes = times;
    if (currentRoute === '#/orchestration' && S.orch) {
      S.ui.flowPlayed = true;
      $('#view').innerHTML = viewOrchestration();
      playFlow({ realtime: true });
    }

    await refreshOverview();
    await showRunDetail(result.run_id);
  } catch (err) {
    if (status) status.innerHTML = `<span style="color:var(--danger)">❌ ${esc(err.message)}</span>`;
    toast(err.message, 'bad');
    if (err.detail && err.detail.diagnosis) {
      S.orch.diagnosis = err.detail.diagnosis;
      $('#view').innerHTML = viewOrchestration();
    }
  }
}

function bindDropZone() {
  const drop = $('#drop');
  const input = $('#fileInput');
  if (!drop || !input) return;
  drop.onclick = () => input.click();
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('over'); };
  drop.ondragleave = () => drop.classList.remove('over');
  drop.ondrop = (e) => {
    e.preventDefault();
    drop.classList.remove('over');
    if (e.dataTransfer && e.dataTransfer.files[0]) doUpload(e.dataTransfer.files[0]);
  };
  input.onchange = (e) => { if (e.target.files[0]) doUpload(e.target.files[0]); };
}

/* ------------------------------------------------------------ 事件委托 */

document.addEventListener('click', async (e) => {
  const hit = e.target && e.target.closest ? e.target : null;
  if (hit && !hit.closest('#userMenu') && !hit.closest('#userCard')) closeUserMenu();

  const el = hit ? hit.closest('[data-act]') : null;
  if (!el) return;
  const { act } = el.dataset;

  try {
    switch (act) {
      /* 通用 */
      case 'modal-close': closeModal(); return;
      case 'goto': closeUserMenu(); goto(el.dataset.hash); return;
      case 'open-docs': {
        e.preventDefault();                       // 先拦下 <a> 的默认跳转，才能按角色放开
        if (!canViewDocs()) { toast('当前设置仅允许管理员查看接口文档', 'bad'); return; }
        window.open('/docs', '_blank', 'noopener');
        return;
      }

      /* 账号 / 主题 */
      case 'user-menu': toggleUserMenu(); return;
      case 'logout': await doLogout(); return;
      case 'open-gate': showGate(); return;
      case 'gate-close': hideGate(); return;
      case 'gate-tab': {
        $('#gate').dataset.tab = el.dataset.tab;
        renderGate();
        return;
      }
      case 'theme-set': setTheme({ [el.dataset.axis]: el.dataset.value }); return;
      case 'theme-reset': setTheme({ ...DEFAULT_THEME }); toast('已恢复默认主题', 'ok'); return;
      case 'pick-avatar':
      case 'pick-color': {
        // 先把表单里未保存的输入收进草稿，避免重渲染时丢失
        const inputs = {
          nickname: $('#pfNick') ? $('#pfNick').value : undefined,
          email: $('#pfEmail') ? $('#pfEmail').value : undefined,
          bio: $('#pfBio') ? $('#pfBio').value : undefined,
        };
        S.ui.profileDraft = {
          ...(S.ui.profileDraft || {}),
          ...inputs,
          [act === 'pick-avatar' ? 'avatar' : 'color']: el.dataset.value,
        };
        $('#view').innerHTML = viewProfile();
        return;
      }
      case 'profile-save': await saveProfile(); return;
      case 'password-save': await savePassword(); return;
      case 'sessions-refresh': {
        S.ui.sessions = await GET('/api/auth/sessions');
        $('#view').innerHTML = viewProfile();
        return;
      }
      case 'sessions-revoke-others': {
        const revoked = await POST('/api/auth/sessions/revoke-others');
        toast(`已退出 ${revoked.revoked} 个其他会话`, 'ok');
        S.ui.sessions = await GET('/api/auth/sessions');
        $('#view').innerHTML = viewProfile();
        return;
      }
      case 'users-refresh': {
        S.users = await GET('/api/auth/users');
        $('#view').innerHTML = viewUsers();
        return;
      }
      case 'user-add': openUserModal(null); return;
      case 'user-edit': {
        const target = (S.users ? S.users.users : []).find((x) => x.id === el.dataset.id);
        if (target) openUserModal(target);
        return;
      }
      case 'user-del': {
        const target = (S.users ? S.users.users : []).find((x) => x.id === el.dataset.id);
        if (!target) return;
        if (!confirm(`删除账号「${target.display_name}」？该操作不可撤销。`)) return;
        S.users = await DEL(`/api/auth/users/${encodeURIComponent(target.id)}`);
        toast('账号已删除', 'ok');
        if (currentRoute === '#/users') $('#view').innerHTML = viewUsers();
        return;
      }
      case 'user-save': await saveUser(); return;

      /* 顶栏 */
      case 'apply-draft': {
        await POST('/api/orchestration/apply');
        toast('已应用生效', 'ok');
        await refreshOrch();
        await route();
        return;
      }
      case 'reload-plugins': {
        const r = await POST('/api/reload');
        toast(`插件已重新扫描（${r.loaded_modules.length} 个模块）`, 'ok');
        S.skills = await GET('/api/skills');
        await refreshOverview();
        await route();
        return;
      }

      /* 概览 / 运行记录 */
      case 'run-detail': await showRunDetail(el.dataset.run); return;
      case 'run-search': await runSearch(el.dataset.run); return;
      case 'run-replay': {
        toast('正在重放…');
        const result = await POST(`/api/runs/${encodeURIComponent(el.dataset.run)}/replay`);
        toast(`重放完成：${result.stats.chunk_count} 个切片`, 'ok');
        closeModal();
        await loadRuns();
        await refreshOverview();
        await route();
        return;
      }
      case 'run-delete': {
        if (!confirm('删除这条运行记录？')) return;
        await DEL(`/api/runs/${encodeURIComponent(el.dataset.run)}`);
        toast('已删除', 'ok');
        closeModal();
        await loadRuns();
        await refreshOverview();
        await route();
        return;
      }
      case 'runs-refresh': await loadRuns(); await route(); return;
      case 'runs-page': await loadRuns(parseInt(el.dataset.page, 10)); $('#view').innerHTML = viewRuns(); return;
      case 'runs-clear': {
        if (!confirm('清空全部运行记录？此操作不可撤销。')) return;
        await DEL('/api/runs');
        toast('已清空', 'ok');
        await loadRuns();
        await refreshOverview();
        await route();
        return;
      }

      /* 编排 */
      case 'validate': {
        await refreshOrch();
        toast(S.orch.diagnosis.ok ? '契约完整' : `发现 ${S.orch.diagnosis.issues.length} 处问题`, S.orch.diagnosis.ok ? 'ok' : 'bad');
        return;
      }
      case 'autofill': {
        const r = await POST('/api/orchestration/autofill');
        toast(r.inserted.length
          ? `已补全 ${r.inserted.length} 处：\n${r.inserted.map((i) => `· ${i.agent} ← ${i.skill}`).join('\n')}`
          : '没有可自动补全的环节', r.inserted.length ? 'ok' : '');
        await refreshOrch();
        return;
      }
      case 'reset-draft': {
        if (!confirm('丢弃当前编辑，回到已生效流水线的结构？')) return;
        await orchAction('/reset');
        toast('已重置草稿', 'ok');
        return;
      }
      case 'add-agent': {
        const created = await orchAction('/agents', { name: `stage_${S.orch.orchestration.agents.length + 1}`, role: '' });
        selectAgent(created && created.created);
        return;
      }
      case 'add-agent-after': {
        const created = await orchAction('/agents', { after: el.dataset.agent });
        selectAgent(created && created.created);
        return;
      }
      case 'select-agent': {
        S.ui.addStepFor = null;
        S.ui.selectedAgent = el.dataset.agent;
        $('#view').innerHTML = viewOrchestration();
        return;
      }
      case 'play-flow': playFlow({ realtime: true }); return;
      case 'flow-stop': stopFlow(); return;
      case 'agent-up': await orchAction(`/agents/${el.dataset.agent}/move`, { direction: 'up' }); return;
      case 'agent-down': await orchAction(`/agents/${el.dataset.agent}/move`, { direction: 'down' }); return;
      case 'agent-del': {
        if (!confirm('删除该阶段及其全部功能步骤？')) return;
        await orchAction(`/agents/${el.dataset.agent}`, undefined, 'DELETE');
        return;
      }
      case 'add-step': S.ui.addStepFor = el.dataset.agent; $('#view').innerHTML = viewOrchestration(); return;
      case 'add-step-cancel': S.ui.addStepFor = null; $('#view').innerHTML = viewOrchestration(); return;
      case 'add-step-confirm': {
        const sel = $(`[data-role="new-skill"][data-agent="${el.dataset.agent}"]`);
        const skill = sel ? sel.value : '';
        S.ui.addStepFor = null;
        if (skill) await orchAction(`/agents/${el.dataset.agent}/steps`, { skill });
        return;
      }
      case 'step-up': await orchAction(`/agents/${el.dataset.agent}/steps/${el.dataset.step}/move`, { direction: 'up' }); return;
      case 'step-down': await orchAction(`/agents/${el.dataset.agent}/steps/${el.dataset.step}/move`, { direction: 'down' }); return;
      case 'step-del': await orchAction(`/agents/${el.dataset.agent}/steps/${el.dataset.step}`, undefined, 'DELETE'); return;
      case 'step-toggle': {
        const agent = S.orch.orchestration.agents.find((a) => a.id === el.dataset.agent);
        const step = agent.steps.find((s) => s.id === el.dataset.step);
        await orchAction(`/agents/${el.dataset.agent}/steps/${el.dataset.step}`, { enabled: !step.enabled }, 'PATCH');
        return;
      }
      case 'step-params': {
        const id = el.dataset.step;
        if (S.ui.openParams.has(id)) S.ui.openParams.delete(id); else S.ui.openParams.add(id);
        $('#view').innerHTML = viewOrchestration();
        return;
      }
      case 'pipeline-create':
        openPipelineCreateModal();
        return;
      case 'pipeline-create-submit':
        await submitPipelineCreate();
        return;
      case 'pipeline-switch': {
        await POST(`/api/pipelines/${encodeURIComponent(el.dataset.name)}/activate`);
        toast(`已切换到流水线 ${el.dataset.name}`, 'ok');
        await refreshOrch();
        return;
      }
      case 'pipeline-del': {
        if (!confirm(`删除流水线 ${el.dataset.name}？`)) return;
        await DEL(`/api/pipelines/${encodeURIComponent(el.dataset.name)}`);
        await refreshOrch();
        return;
      }
      case 'pipeline-page': {
        await loadPipelines(parseInt(el.dataset.page, 10));
        $('#view').innerHTML = viewOrchestration();
        return;
      }

      /* 技能库 */
      case 'skill-detail': await showSkillDetail(el.dataset.name); return;
      case 'skills-page': S.ui.listPage.skills = parseInt(el.dataset.page, 10); $('#view').innerHTML = viewSkills(); return;
      case 'skill-test-open': closeModal(); await openSkillTest(el.dataset.name); return;
      case 'skill-test-run': await runSkillTest(el.dataset.name); return;
      case 'skill-test-reset': resetTestOptions(); return;
      case 'skill-test-file-clear': clearTestFile(); return;
      case 'script-console': await openScriptConsole(); return;
      case 'script-run': await runScriptValidate(); return;
      case 'script-sample': applyScriptSample(true); return;
      case 'script-clear': {
        $('#scriptCode').value = '';
        $('#scriptResult').innerHTML = '';
        return;
      }

      /* 知识库 */
      case 'kb-search': await kbSearch(el.dataset.run); return;
      case 'kb-search-run': await kbSearchRun(); return;
      case 'kb-page': await loadIndexes(parseInt(el.dataset.page, 10)); $('#view').innerHTML = viewKnowledge(); return;
      case 'kb-delete': {
        if (!confirm('删除该向量索引？')) return;
        await DEL(`/api/knowledge/indexes/${encodeURIComponent(el.dataset.run)}`);
        toast('索引已删除', 'ok');
        await loadIndexes();
        await route();
        return;
      }

      /* 设置 */
      case 'settings-save': await saveSettings(); return;
      case 'settings-reset': {
        if (!confirm('恢复默认设置？将重置全部平台配置（不影响编排方案）。')) return;
        S.settings = await POST('/api/settings/reset');
        await refreshAuthConfig();
        renderChrome();
        await refreshOverview();
        toast('已恢复默认设置', 'ok');
        if (needsGate()) { showGate(); return; }
        await route();
        return;
      }
      case 'export-config': {
        const bundle = await GET('/api/platform/export');
        const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `agent-platform-export-${Date.now()}.json`;
        a.click();
        URL.revokeObjectURL(url);
        toast('配置已导出（密钥已掩码）', 'ok');
        return;
      }
      case 'provider-add': openProviderModal(null); return;
      case 'provider-edit': {
        const p = S.settings.providers.find((x) => x.id === el.dataset.id);
        openProviderModal(p);
        return;
      }
      case 'provider-save': await saveProvider(); return;
      case 'provider-del': {
        if (!confirm('删除该供应商？')) return;
        const r = await DEL(`/api/settings/providers/${encodeURIComponent(el.dataset.id)}`);
        S.settings.providers = r.providers;
        toast('已删除', 'ok');
        await route();
        return;
      }
      default: return;
    }
  } catch (err) {
    toast(err.message, 'bad');
  }
});

document.addEventListener('change', async (e) => {
  const el = e.target;
  const role = el.dataset.role;
  if (!role) return;

  try {
    if (role === 'script-lang') {
      onScriptLangChange();
    } else if (role === 'script-file') {
      loadScriptFile(el.files && el.files[0]);
    } else if (role === 'test-file') {
      await loadTestFile(el.files && el.files[0]);
    } else if (role === 'skill') {
      await orchAction(`/agents/${el.dataset.agent}/steps/${el.dataset.step}`, { skill: el.value }, 'PATCH');
    } else if (role === 'agent-name') {
      await orchAction(`/agents/${el.dataset.agent}`, { name: el.value }, 'PATCH');
    } else if (role === 'agent-role') {
      await orchAction(`/agents/${el.dataset.agent}`, { role: el.value }, 'PATCH');
    } else if (role === 'param') {
      const agent = S.orch.orchestration.agents.find((a) => a.id === el.dataset.agent);
      const step = agent.steps.find((s) => s.id === el.dataset.step);
      const spec = S.orch.catalog.skills[step.skill].params[el.dataset.key];
      let value;
      if (spec.type === 'bool') value = el.checked;
      else if (spec.type === 'int') value = parseInt(el.value, 10) || 0;
      else if (spec.type === 'float') value = parseFloat(el.value) || 0;
      else if (spec.type === 'list') value = el.value.split(',').map((s) => s.trim()).filter(Boolean);
      else value = el.value;
      await orchAction(`/agents/${el.dataset.agent}/steps/${el.dataset.step}`, { options: { ...step.options, [el.dataset.key]: value } }, 'PATCH');
    } else if (role === 'skill-toggle') {
      const name = el.dataset.name;
      const result = await POST(`/api/skills/${encodeURIComponent(name)}/toggle`, { enabled: el.checked });
      toast(`插件 ${name} 已${el.checked ? '启用' : '停用'}`, 'ok');
      S.skills = await GET('/api/skills');
      await refreshOverview();
      await route();
      void result;
    } else if (role === 'runs-status') {
      S.ui.runsFilter.status = el.value;
      await loadRuns(1);
      $('#view').innerHTML = viewRuns();
    } else if (role === 'runs-keyword') {
      S.ui.runsFilter.keyword = el.value.trim();
      await loadRuns(1);
      $('#view').innerHTML = viewRuns();
    }
  } catch (err) {
    toast(err.message, 'bad');
    await route();
  }
});

// 登录门表单提交
document.addEventListener('submit', (e) => {
  if (e.target.id === 'gateForm') {
    e.preventDefault();
    submitGate();
  }
});

// 检索输入框回车
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  if (e.target.id === 'runQuery' && S.ui.activeRun) runSearch(S.ui.activeRun);
  if (e.target.id === 'kbQuery') kbSearchRun();
});

/* ------------------------------------------------------------ 启动 */

(async function boot() {
  $('#modal').classList.add('hidden');

  const authNotice = popAuthRequiredNotice();                 // 文档页跳回时的提示
  Auth.token = localStorage.getItem(TOKEN_KEY) || '';
  applyTheme(loadLocalTheme(), { persist: false });          // 先用本地偏好，避免闪烁
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', () => {
    if (theme.mode === 'auto') applyTheme({}, { persist: false });
  });

  await refreshAuthConfig();

  // 尝试用已存令牌恢复会话
  if (Auth.token) {
    try {
      const me = await GET('/api/auth/me', { silent401: true });
      Auth.user = me.user;
    } catch {
      clearSession();
    }
  }

  renderChrome();

  if (needsGate()) { showGate(authNotice); return; }          // 未登录：停在登录门
  await startApp();
  if (authNotice) toast(authNotice, 'ok');
})();
