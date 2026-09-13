# Agent 平台 —— 多 Agent 串联 + 插拔式 Skill 流水线

> 当前版本：**v1.0.0** ｜ FastAPI + 原生前端（无构建步骤）

一套可运行、可运营的 Agent 平台内核：**多个 Agent 串联**完成复杂任务，每个 Agent 持有若干
**可插拔 Skill**，通过统一的数据总线实现任务联动。

内置场景：**文件上传 → 文件清洗 → 文本切片 → 向量化 → 向量入库 → 相似度检索**，
其中每个环节都是同槽位可互换的插件。

编排既可以在 YAML 里**静态声明**，也可以在前端「编排工作台」里**运行时手动调整**：
按阶段增删功能、跨槽位换实现、改参数、实时看契约是否接得上，先试跑再「应用生效」。

平台还自带一整套工作台：**概览看板、技能库、运行记录、知识库、系统设置、账号与权限**，
所有配置项 Schema 驱动，所有接口受统一的登录校验保护。

---

## 1. 整体架构

```
                   ┌──────────────── PipelineContext（数据总线）─────────────────┐
                   │  raw_file → text → clean_text → chunks → embeddings → index │
                   └───────▲───────────────▲──────────────▲───────────────▲───────┘
                           │               │              │               │
┌──────────────────┐  ┌────┴───────┐  ┌────┴───────┐  ┌────┴───────┐  ┌────┴───────┐
│  Web 工作台       │  │  Agent 1   │  │  Agent 2   │  │  Agent 3   │  │   检索      │
│  概览 / 编排      │─▶│ ingestion  │─▶│ chunking   │─▶│ indexing   │─▶│ /search    │
│  技能库 / 运行记录 │  │ text_loader│  │ recursive_ │  │ hash_      │  │ /knowledge │
│  知识库 / 设置     │  │ basic_...  │  │ splitter   │  │ embedder   │  │            │
└──────────────────┘  └────────────┘  └────────────┘  └────────────┘  └────────────┘
                        ↑                            ↑
                    槽位：loader/cleaner        槽位：embedder/vector_store
```

### 四个核心抽象

| 抽象 | 文件 | 职责 |
|---|---|---|
| `Skill` | `app/core/skill.py` | 最小能力单元。声明 `slot`/`consumes`/`produces`，实现 `run(ctx)` |
| `Agent` | `app/core/agent.py` | 一组 Skill 的编排单元，负责串联与每步契约校验 |
| `Pipeline` | `app/core/orchestrator.py` | 多 Agent 串联成链，编译期 + 运行期双重校验 |
| `PipelineContext` | `app/core/context.py` | 数据总线，承载全部产物与执行轨迹 |

### 配套基础设施

| 组件 | 文件 | 职责 |
|---|---|---|
| `Orchestration` | `app/core/orchestration.py` | 可变编排草稿：软校验诊断、自动补全、编译为 Pipeline |
| `Database` | `app/core/db.py` | 多方言数据库引擎（SQLite / MySQL / PostgreSQL）+ 密钥封装 + 审计 |
| `SettingsStore` | `app/core/settings.py` | 平台设置：Schema 驱动、落库、密钥掩码 |
| `RunRegistry` | `app/core/runs.py` | 运行记录：只留摘要（耗时/产物/轨迹），落库 |
| `AuthManager` / `UserStore` | `app/core/auth.py` | 账号库与会话令牌：签发、校验、吊销、多设备管理（落库） |
| `Warehouse` | `app/core/warehouse.py` | 向量仓库：内存缓存 + 落库元数据，重启后按工厂重建 |
| `DocumentStore` | `app/core/storage.py` | 原始文件对象存储（落盘 + 登记），支撑重启后重放 |
| `ProfileStore` | `app/core/profiles.py` | 编排方案：落库 + YAML 导入导出 |
| `RuntimeState` | `app/runtime.py` | 把以上部件装配成一份运行时状态，并对外提供快照 |

### 两个关键设计

**① 槽位（slot）解耦了「能力接口」与「具体实现」**

`cleaner` 是槽位，`basic_cleaner` / `markdown_normalizer` / `sensitive_word_cleaner` 是它的三种实现。
Agent 只关心「我要一个 cleaner」，YAML 决定「用哪个 cleaner」。

**② 产物契约（consumes / produces）保证任务联动正确**

每个 Skill 显式声明自己消费和产出哪些产物类型。`Pipeline.validate()` 会在**跑数据之前**
模拟整条链的产物流动，提前发现「上下游不匹配」。例如把 `hash_embedder` 放在
`recursive_splitter` 之前，启动阶段就会直接报错：

```
ContractError: [a1] 插件 'hash_embedder' 需要产物 ['chunks']，但上游只提供了 ['raw_file', 'text']。
```

---

## 2. 运行时可编排（编排工作台）

静态 YAML 描述的是**启动时**的编排；真实场景里更需要**在界面上随时调整**。为此平台引入了
`Orchestration`（编排草稿）：

|          | `Orchestration`（草稿） | `Pipeline`（执行体） |
|----------|------------------------|---------------------|
| 性质      | 可变，用户编辑           | 不可变，实际执行        |
| 校验      | `diagnose()` **软校验** | `validate()` **硬校验** |
| 校验失败   | 返回断裂点 + 补全建议     | 抛 `ContractError`    |
| 持久化    | 方案落库（可导出 YAML）   | 内存对象              |

### 能做什么

1. **按阶段增删功能** —— 每个阶段（Agent）里可以自由插入任意插件的实现；下拉框按槽位分组，允许跨槽位插入（比如在清洗阶段后补一道敏感词过滤）。
2. **换实现 / 改参数** —— 参数表单由插件的 `param_schema` 自动生成，无需前端硬编码。
3. **实时契约诊断** —— 每步显示 `consumes → produces`，接不上时标红，并给出「该在哪个槽位补哪个插件」的建议。
4. **自动补全** —— 一键在断裂点前插入能生产缺失产物的插件，反复修补直到链路完整。
5. **试跑 → 应用** —— `use=draft` 用草稿执行（不改生效配置），满意后点「应用生效」。
6. **方案存档** —— 把当前编排保存为 YAML 方案，随时加载、切换。
7. **执行链路可视化** —— 横向阶段节点展示执行顺序与契约状态，并带动态反馈（见下）。

### 执行链路的动态效果

| 效果 | 说明 |
|---|---|
| 连线流光 | 相邻阶段之间沿执行方向循环流动的光点，逐段错开 0.3s，直观表达数据流向 |
| 依次点亮 | 进入页面时阶段节点按执行顺序依次浮出（`--i` 序号驱动，编辑重渲染不重放） |
| 呼吸光环 | 契约完整的阶段持续呼吸，静态展示时链路也是「活的」 |
| 演示执行顺序 | 点「▶ 演示执行顺序」：阶段依次进入 `running`（旋转脉冲环）→ `done`（弹跳勾选），连线逐段点亮并加速 |
| 耗时回放 | 试跑成功后节点标注各阶段真实耗时，并按耗时比例回放（快的阶段一闪而过，慢的停留更久） |
| 降级 | 全部动效遵循 `prefers-reduced-motion`，系统开启「减少动态效果」时自动静态化 |

### 一个真实交互序列

```
① 添加功能        ingestion_agent ← sensitive_word_cleaner
                  → diag.ok = true，dirty = true

② 删掉切片步骤     chunking_agent.steps = []

③ 诊断            diag.ok = false
                  issue: [indexing_agent/hash_embedder] 需要产物 ['chunks']，但上游没有
                  suggest: 缺少产物 'chunks'：请在「indexing_agent」这一步之前插入
                           splitter 槽位的插件（recursive_splitter / fixed_splitter / markdown_splitter）

④ 试跑            422 当前编排无法执行：插件 'hash_embedder' 需要产物 ['chunks']…

⑤ 自动补全        inserted: [{agent: indexing_agent, skill: recursive_splitter, produces: chunks}]
                  → diag.ok = true

⑥ 试跑            成功，trace 显示 6 步全部执行，敏感词掩码生效

⑦ 保存 / 加载方案  demo-recipe.yaml ↔ markdown-rag.yaml

⑧ 应用生效        active_pipeline = markdown-rag（向量维度 256 → 128）
```

### 编排接口一览

所有**写操作**都会返回完整的编排状态（草稿 + 诊断 + 插件目录 + 方案列表），前端一次往返
即可刷新，无需自行维护状态。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/orchestration` | 完整状态：草稿 + 诊断 + 插件目录 + 方案列表 |
| `POST` | `/api/orchestration/validate` | 重新诊断 |
| `POST` | `/api/orchestration/agents` | 新增阶段（可指定插入位置） |
| `PATCH` | `/api/orchestration/agents/{id}` | 改阶段名 / 职责 |
| `DELETE` | `/api/orchestration/agents/{id}` | 删除阶段 |
| `POST` | `/api/orchestration/agents/{id}/move` | 上移 / 下移阶段 |
| `POST` | `/api/orchestration/agents/{id}/steps` | 阶段内添加功能（指定插件与参数） |
| `PATCH` | `/api/orchestration/agents/{id}/steps/{sid}` | 换插件 / 改参数 / 启停 |
| `DELETE` | `/api/orchestration/agents/{id}/steps/{sid}` | 删除功能 |
| `POST` | `/api/orchestration/agents/{id}/steps/{sid}/move` | 上移 / 下移功能 |
| `POST` | `/api/orchestration/autofill` | 按诊断自动补齐断裂环节 |
| `POST` | `/api/orchestration/apply` | 编译草稿为生效流水线（硬校验） |
| `POST` | `/api/orchestration/reset` | 丢弃编辑，回到已生效结构 |
| `POST` | `/api/orchestration/rename` | 改编排名称 / 描述 |
| `GET` `POST` `DELETE` | `/api/orchestration/profiles[/{name}]` | 方案列表 / 保存 / 删除 |
| `POST` | `/api/orchestration/profiles/{name}/load` | 加载方案到草稿 |

> 完整字段与响应结构见 **第 7 节 HTTP 接口**。

---

## 3. 工作台页面

前端是零构建的原生 SPA（`web/index.html` + `web/assets/app.js`），用 hash 路由切换视图：

| 路由 | 页面 | 内容 |
|---|---|---|
| `#/overview` | 概览 | 平台健康度、契约状态、阶段/插件/索引计数、运行统计、最近记录 |
| `#/orchestration` | 编排工作台 | 执行链路动效图、阶段与步骤编辑、契约诊断、自动补全、试跑上传、应用生效 |
| `#/skills` | 技能库 | 插件目录（按槽位分组）、启停开关、详情与同槽位对比、沙盒试跑 |
| `#/runs` | 运行记录 | 历史列表（状态/关键词筛选）、轨迹详情、重放、删除/清空 |
| `#/knowledge` | 知识库 | 向量索引列表、索引内相似度检索、删除索引 |
| `#/profile` | 个人中心 | 资料、头像/颜色、密码修改、活跃会话与「退出其他设备」 |
| `#/theme` | 主题外观 | 深浅模式、主色、圆角、密度、玻璃光晕（写入账号偏好） |
| `#/users` | 用户管理 | 仅管理员：账号列表、创建、改角色/状态/重置密码、删除 |
| `#/settings` | 系统设置 | Schema 表单、模型供应商凭据、导入导出、恢复默认 |

顶栏常驻两个动作：**重扫插件**（`POST /api/reload`）与**应用生效**（`POST /api/orchestration/apply`）。

### 登录门与接口文档鉴权

- 全局中间件 `auth_guard` 对**所有** `/api/*` 与**接口文档**（`/docs`、`/redoc`、`/openapi.json`）
  做登录校验，白名单仅：`/api/health`、`/api/auth/config`、`/api/auth/login`、`/api/auth/register`；
  静态页面与前端资源放行（未登录时由前端登录门接管）。
- 令牌解析顺序：`Authorization: Bearer <token>` → `X-Auth-Token` → 会话 Cookie
  `agent_platform_session`。前端把令牌存 `localStorage`；Cookie 由登录/注册接口下发
  （HttpOnly + SameSite=Lax），专供**浏览器直连文档页**的场景。
- 会话在服务端可吊销（退出登录 / 退出其他设备 / 改密后强制重登），吊销后 Cookie 立即失效。
- 关闭「启用登录校验」设置后接口与文档页都对匿名开放，**仅建议本地调试时使用**。

**接口文档的可见范围（按角色放开）**

设置项 `auth.docs_access`（系统设置 → 登录与安全）决定 `/docs`、`/redoc`、`/openapi.json` 对谁可见，
管理员在界面上直接切换，保存后立即生效：

| 取值 | 含义 | 效果 |
|---|---|---|
| `member`（默认） | 所有登录用户 | 登录后即可查看；未登录跳回登录门 |
| `admin` | 仅管理员 | 普通账号拿到 `403`（浏览器跳 `/?auth=forbidden`）；工作台侧边栏入口置灰并提示 |
| `public` | 匿名公开 | 无需登录即可查看（业务接口仍要求登录） |

**接口文档的鉴权行为**

| 场景 | 结果 |
|---|---|
| 未登录，浏览器打开 `/docs` / `/redoc` | `302` → `/?auth=required`，工作台弹出登录门并提示「接口文档需要登录后查看」 |
| 未登录，客户端请求 `/openapi.json`（或 `curl /docs`） | `401` `{"detail": "未登录或会话已过期，请重新登录", "code": "unauthorized"}` |
| 已登录但角色不足（`admin` 策略） | `403` `{"detail": "当前账号无权查看接口文档，请联系管理员", "code": "forbidden"}` |
| 已登录且角色满足 | 页面顶部显示**当前登录账号**（昵称 / 用户名 / 角色）与**文档可见范围**，Swagger 的 Try it out 自动携带当前会话 |
| 登出 / 会话过期 / 被管理员重置密码 | Cookie 失效，文档页重新回到登录门 |

文档页不是 FastAPI 内置版本（`docs_url` / `redoc_url` / `openapi_url` 全部关闭），改由
`app/api/docs_routes.py` 提供：模板下发前注入账号条，并在规范里补上 `BearerAuth` 安全方案
（Swagger 右上角 Authorize 可直接贴令牌调试）。放行决策集中在 `AuthManager.docs_gate()`，
中间件据此区分「未登录」（跳登录门）与「已登录但无权」（提示越权）。

### 附带的静态示例页

`web/ci-pipeline.html` 是一份独立的「CI 流水线运行详情」示例页面（复用 `app.css` 视觉体系），
可直接访问 `http://127.0.0.1:8000/ci-pipeline.html`。

---

## 4. 快速开始

```bash
pip install -r requirements.txt
copy .env.example .env                     # Linux/macOS：cp .env.example .env（填 Milvus 地址等）
cd deploy/milvus && docker compose up -d   # 启动 Milvus（默认入库后端，首次会拉镜像）
cd ../.. && python run.py                  # 打开 http://127.0.0.1:8000
python -m unittest discover -s tests -v    # 运行测试（Milvus 未启动时相关用例自动跳过）
```

- 前端工作台：<http://127.0.0.1:8000>
- OpenAPI 文档：<http://127.0.0.1:8000/docs>（**需登录**；未登录会被带回登录门，登录后页面绑定当前账号）

**首次运行**会自动播种一个管理员账号（数据库里还没有任何账号时）：

| 用户名 | 密码 | 说明 |
|---|---|---|
| `admin` | `admin123` | 带 `must_change_password` 标记，登录后请立即到「个人中心」改密 |

> 也可以在「系统设置 → 登录与安全」里调整登录校验、自助注册与会话有效期。

**数据落盘位置**

平台自身的状态（设置 / 账号 / 会话 / 运行记录 / 知识库索引 / 编排方案 / 审计 / 脚本执行记录）
统一落进**一个数据库**，默认是 SQLite 文件 `data/platform.db`；设置环境变量
`PLATFORM_DATABASE_URL`（或 `bootstrap(database_url=...)`）即可切到 MySQL / PostgreSQL，
`pymysql` / `psycopg2` 已在依赖清单里。上传的**原始文件正文**另存到 `data/uploads/`，
用于重启后重放。除此之外只剩静态配置在文件里：

| 位置 | 内容 |
|---|---|
| `data/platform.db` | 平台数据库（默认 SQLite；可用 `PLATFORM_DATABASE_URL` 换库） |
| `data/uploads/` | 上传的原始文件（按 `run_id` 存放，支撑「重放」） |
| `config/pipeline.yaml` | 静态编排（启动时的默认流水线） |
| `.env` | 公共环境变量（不入库，模板见 `.env.example`） |

> `config/settings.json`、`config/users.json`、`config/.auth_secret`、`data/runs.json`
> 是**旧版本的文件落盘格式**：只在首次启动、且数据库中还没有对应数据时作为**迁移来源**
> 导入一次，之后不再读写，可以安全删除。`config/orchestrations/*.yaml` 则是随仓库附带的
> **示例方案**，可随时用编排工作台的「保存 / 加载方案」导入导出。

**公共配置统一走 `.env`**

连接串、口令、API Key 这类「跨环境不同 + 敏感」的值，统一放项目根目录的 `.env`，
配置文件里用 `${VAR}` 引用：

```dotenv
# .env
MYSQL_USER=app
MYSQL_PASSWORD=app123456
MYSQL_DSN=mysql://${MYSQL_USER}:${MYSQL_PASSWORD}@127.0.0.1:3306/demo?charset=utf8mb4

# 向量库（milvus_store 插件）：地址 / 库名 / 度量 / 鉴权集中在这里
MILVUS_URI=http://localhost:19530
MILVUS_COLLECTION=default
MILVUS_METRIC_TYPE=COSINE
MILVUS_TOKEN=
```

```yaml
# config/orchestrations/mysql-kb.yaml
- skill: sql_loader
  options:
    dialect: mysql
    dsn: "${MYSQL_DSN}"

# config/pipeline.yaml
- use: milvus_store
  with:
    uri: ${MILVUS_URI}
    collection: ${MILVUS_COLLECTION}
    metric_type: ${MILVUS_METRIC_TYPE}
    token: ${MILVUS_TOKEN}
```

- **首次使用**：`copy .env.example .env`（Linux/macOS 用 `cp`），按本机情况填写。
  `.env` 已被 `.gitignore` 忽略，仓库里只提交 `.env.example`。默认流水线引用了
  `${MILVUS_*}`，所以**首次启动前必须先准备 `.env`**。
- **加载时机**：`app/main.py` 启动时读入，无需手动 source。文件不存在也能正常启动，
  只有**真正引用到缺失变量**时才报错，并指出是哪个文件引用了它。不经平台入口的
  调用方（单测、脚本）由 `app/core/env.py` 的 `ensure_dotenv()` 在首次展开时兜底读取。
- **优先级**：系统环境变量 > `.env`。`.env` 不会覆盖已存在的系统变量，方便 CI / 容器注入。
- **展开时机**：只在插件实例化（`SkillRegistry.create`）时、且在参数**副本**上展开。
  因此「保存方案」不会把密钥回写进 YAML，`GET /api/orchestration` 也不会把密钥
  返回给前端 —— 工作台里看到的始终是 `${MYSQL_DSN}` / `${MILVUS_URI}` 原文。
- **语法**：只识别 `${VAR}`，不认 `$VAR` 裸写（避免口令里的 `$` 被误判）；
  同文件内可互相引用且与书写顺序无关；值里含 `#` 请用引号包起来。
- **脚本共用**：`D:\mysql\*.bat` 启停脚本读的是同一个 `.env`（见 `D:\mysql\_env.bat`），
  口令只需维护一份。口令类变量**没有兜底默认值**，缺失时脚本直接报错退出。

---

## 5. 插件（Skill）清单

| 槽位 | 插件 | 说明 | 依赖 |
|---|---|---|---|
| `loader` | `text_loader` | txt/md/csv/json/log/html，自动探测编码；PDF/Word 按文件签名自动委派 | 无 |
| | `pdf_loader` | PDF 逐页抽取文本 | `pypdf` |
| | `docx_loader` | Word 段落 + 表格 | `python-docx` |
| | `sql_loader` | 连接 SQL 数据库执行查询，结果集渲染为文本（数据源起点） | SQLite 无；PG/MySQL 需驱动 |
| `cleaner` | `basic_cleaner` | 控制符清理、空白压缩、行去重、邮箱脱敏 | 无 |
| | `markdown_normalizer` | 剥离 front-matter / 代码块标记、链接还原 | 无 |
| | `sensitive_word_cleaner` | 敏感词掩码（外部插件示例） | 无 |
| `splitter` | `recursive_splitter` | 递归字符切片，按语义优先级回退（推荐） | 无 |
| | `fixed_splitter` | 定长硬切，吞吐最高 | 无 |
| | `markdown_splitter` | 按标题层级切片，标题路径写入 meta | 无 |
| `embedder` | `hash_embedder` | Hashing Trick，零依赖零网络 | 无 |
| | `openai_embedder` | OpenAI Embeddings API | `openai` |
| `vector_store` | `memory_store` | 进程内余弦相似度索引（零依赖，适合离线演示） | 无 |
| | `milvus_store` | 切片与向量写入 Milvus 集合，检索按 run 隔离（默认入库后端） | `pymilvus` |

插件可在「技能库」页面**启停**：被停用的插件不会出现在编排下拉框中，已有的编排会立即
显示契约断裂；若启停导致生效流水线契约失效，平台会**自动回滚**设置。

### 5.1 Milvus 向量库

上传的切片默认落库到 Milvus（`config/pipeline.yaml` 里 `indexing_agent` 用的是 `milvus_store`）：

```powershell
cd deploy/milvus
docker compose up -d          # 启动 Standalone，连接串 http://localhost:19530
docker compose logs -f standalone
docker compose down           # 停止（保留数据，数据在具名卷里）
docker compose down -v        # 停止并清空数据
```

- **连接信息来自 `.env`**：`${MILVUS_URI}` / `${MILVUS_COLLECTION}` / `${MILVUS_METRIC_TYPE}` /
  `${MILVUS_TOKEN}` 四个变量统一定义在项目根目录的 `.env`（模板见 `.env.example`）。
  换地址、换库、开鉴权都只改 `.env`；方案文件与接口里始终是占位符原文，
  真实地址与口令不会被写回 YAML 或返回给前端。
- **按 run 隔离**：一个集合（默认 `default`）容纳所有上传，用 `run_id` 字段区分；
  检索只返回该次上传的切片，删除索引时会同步清掉该 run 在 Milvus 里的实体。
- **切回内存索引**：把 `use: milvus_store` 改回 `memory_store` 即可，无需改代码。
- **维度/度量不一致会直接拒绝**：集合的向量维度与度量在首次创建时固定，
  更换 `embedder`（如 `hash_embedder` 的 `dim`）后请同时换一个集合名称。
- **强一致**：写入后立刻可检索（无需等 flush），符合「上传完马上问」的用法。
- **测试隔离**：跑单测时会把 `MILVUS_COLLECTION` 临时覆盖成 `agent_full_test_xxxx`，
  整组用例结束再删掉，不会把测试留下的切片堆进 `.env` 里配置的真实集合。
- **重启后的索引列表**：索引登记（run ↔ 集合 / 维度 / 度量）已落库，服务重启后按登记的
  连接参数自动重建 Milvus 句柄，「知识库索引」列表与检索照常可用，无需重新上传。

---

## 6. 如何插拔

### 6.1 替换插件：只改 YAML，不动代码

> 等价的**运行时**做法：在「编排工作台」里直接改下拉框选插件、改参数，无需动
> 文件也无需重启，见第 2 节。

`config/pipeline.yaml`：

```yaml
pipeline:
  name: doc-ingestion
  agents:
    - name: ingestion_agent
      role: 负责把上传文件解析为纯文本并完成清洗
      skills:
        - use: text_loader
        - use: markdown_normalizer     # ← 换掉 basic_cleaner
          with:
            keep_headings: true
    - name: chunking_agent
      skills:
        - use: markdown_splitter       # ← 换掉 recursive_splitter
          with: { max_level: 2 }
    - name: indexing_agent
      skills:
        - use: openai_embedder         # ← 换掉 hash_embedder
          with: { model: text-embedding-3-small }
        - use: memory_store
```

### 6.2 新增插件：两种方式

**方式 A：外部目录热插拔（推荐给业务团队）**

把 `.py` 丢进 `ext_plugins/`，重启或用 `POST /api/reload` 即可生效：

```python
from app.core.context import TEXT, CLEAN_TEXT, PipelineContext
from app.core.skill import Skill, skill

@skill
class MyCleaner(Skill):
    name = "my_cleaner"
    slot = "cleaner"                  # 声明槽位即可参与替换
    description = "自定义清洗逻辑"
    consumes = (TEXT,)                # 需要上游的 text 产物
    produces = (CLEAN_TEXT,)          # 产出 clean_text 给下游

    # 声明参数后，编排工作台会自动生成配置表单
    param_schema = {
        "threshold": {"type": "int", "default": 3, "label": "阈值", "min": 0, "max": 100},
    }

    def configure(self, options: dict) -> None:
        self.threshold = int(options.get("threshold", 3))

    def run(self, ctx: PipelineContext) -> None:
        text = ctx.require(TEXT)
        ctx.put(CLEAN_TEXT, text, producer=self.name)
```

插件重新加载后会自动出现在工作台的下拉框里，**无需改动平台任何代码**。

**方式 B：内置插件包** —— 在 `app/plugins/` 下新增模块，启动时自动扫描。

### 6.3 新增一条流水线

复制一份 YAML 改改即可，无需改任何代码：

```yaml
pipeline:
  name: my-pipeline
  agents:
    - name: a1
      skills: [text_loader, sensitive_word_cleaner]
    - name: a2
      skills:
        - use: fixed_splitter
          with: { chunk_size: 200, chunk_overlap: 20 }
    - name: a3
      skills: [hash_embedder, memory_store]
```

---

## 7. HTTP 接口

### 7.1 通用约定

- 所有业务接口都在 `/api` 前缀下，共 5 个路由模块：`routes.py`（核心）、`orchestration_routes.py`、
  `auth_routes.py`、`platform_routes.py`，以及 `docs_routes.py`（受保护的接口文档）。
- **认证**：默认开启登录校验，令牌按 `Authorization: Bearer <token>` → `X-Auth-Token` →
  会话 Cookie `agent_platform_session` 的顺序解析；白名单接口：`/api/health`、
  `/api/auth/config`、`/api/auth/login`、`/api/auth/register`。
- **接口文档**：`/docs`（Swagger UI）、`/redoc`、`/openapi.json` 的可见范围由 `auth.docs_access`
  （`member` / `admin` / `public`）决定；未登录时浏览器（`Accept: text/html`）拿到
  `302` 跳回 `/?auth=required`，角色不足跳 `/?auth=forbidden`，接口客户端分别拿到 `401` / `403`。
- **错误**：字段级错误统一返回 `{"message": "…", "errors": ["…"]}`；契约错误返回
  `{"message": "…", "diagnosis": {…}}`；参数非法为 `422`，未登录 `401`，越权 `403`，
  资源不存在 `404`，移动越界 `409`。
- 完整 OpenAPI 规范见 `/docs` 或 `/openapi.json`。

### 7.2 核心与元信息（`app/api/routes.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查：`{status, version, pipeline}`，公开 |
| `GET` | `/api/skills` | 插件目录：`{count, total, disabled, slots, skills}`（插拔视图） |
| `GET` | `/api/pipeline` | 当前生效流水线编排与产物契约（含插件清单、槽位、配置路径） |
| `POST` | `/api/reload` | 重新扫描插件并重建流水线（热插拔），返回加载模块与流水线描述 |
| `POST` | `/api/upload?use=active\|draft` | 上传文件跑完整条流水线；`draft` 用当前草稿试跑 |
| `POST` | `/api/search` | 在指定 run 的向量索引上检索：`{run_id, query, top_k?}` |

`/api/upload` 的准入规则来自平台设置：扩展名白名单（`415`）、单文件大小上限（`413`）、
空文件（`400`）；`use=draft` 受「允许草稿试跑」开关约束（`403`），契约不通过返回 `422` + 诊断。

### 7.3 编排接口（`app/api/orchestration_routes.py`）

见 **第 2 节** 的接口一览；所有写操作均返回完整 `state()`：

```json
{
  "orchestration": { "name": "...", "description": "...", "agents": [ { "id": "...", "name": "...", "role": "...", "steps": [ {"id":"...","skill":"...","options":{},"enabled":true} ] } ] },
  "diagnosis": { "ok": true, "issues": [] },
  "dirty": false,
  "active_pipeline": "doc-ingestion",
  "active_profile": null,
  "catalog": { "slots": {}, "skills": [] },
  "profiles": [],
  "source_config": "config/pipeline.yaml"
}
```

### 7.4 账号与会话（`app/api/auth_routes.py`）

**公开**

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/auth/config` | 登录页所需公开配置：是否强制登录、是否允许注册、会话时长、主题轴、默认账号提示 |
| `POST` | `/api/auth/login` | 登录并签发令牌：`{username, password, remember?}` → `{token, expires_at, user}` |
| `POST` | `/api/auth/register` | 自助注册（受开关约束）；**库中无任何账号时首个注册者成为管理员** |

**登录后（本人）**

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/auth/me` | 当前用户 + 会话信息（`jti`/签发/过期/活跃会话数） |
| `POST` | `/api/auth/logout` | 退出登录（吊销当前令牌） |
| `PUT` | `/api/auth/profile` | 更新资料：`nickname/email/avatar/color/bio`（颜色与头像会被规范化） |
| `PUT` | `/api/auth/password` | 改密：`{current_password, new_password, logout_others?}` |
| `PUT` | `/api/auth/preferences` | 更新偏好（主题，取值经白名单过滤） |
| `GET` | `/api/auth/sessions` | 活跃会话列表（标记 `current`） |
| `POST` | `/api/auth/sessions/revoke-others` | 退出其他设备 |

**仅管理员**

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/auth/users` | 账号列表 + 统计（总数 / 启用 / 管理员） |
| `POST` | `/api/auth/users` | 创建账号（可指定 `role`、`must_change_password`） |
| `PATCH` | `/api/auth/users/{user_id}` | 改资料 / 角色 / 状态；传 `new_password` 即重置密码并强制重登 |
| `DELETE` | `/api/auth/users/{user_id}` | 删除账号（不能删除自己；最后一个管理员受保护） |

### 7.5 平台管理（`app/api/platform_routes.py`）

**概览与导出**

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/platform/overview` | 工作台首页聚合视图（平台信息、健康度、计数、诊断、运行统计、最近记录） |
| `GET` | `/api/platform/export` | 导出平台配置包（设置 + 供应商 + 编排 + 方案，**密钥已掩码**） |

**设置中心**

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/settings` | 读取设置：`{values, schema, providers, meta}`（密钥掩码） |
| `GET` | `/api/settings/schema` | 仅 Schema + 当前值（前端渲染表单） |
| `PUT` | `/api/settings` | 局部更新设置；校验失败 `422` 且不落库 |
| `POST` | `/api/settings/reset` | 恢复默认设置并重建流水线 |
| `POST` | `/api/settings/providers` | 新增 / 更新模型供应商（回传掩码值时保留原密钥） |
| `DELETE` | `/api/settings/providers/{provider_id}` | 删除供应商 |

**技能库**

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/skills/{name}` | 插件详情 + 同槽位 `peers` + 「哪些编排步骤在用它」的 `usage` |
| `POST` | `/api/skills/{name}/toggle` | 启用 / 停用插件（契约失效自动回滚） |
| `POST` | `/api/skills/{name}/test` | 沙盒单独试跑：平台合成上游产物，返回耗时、输入、产出摘要 |

**运行记录**

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/runs?limit=&status=&keyword=` | 运行记录列表 + 统计 |
| `GET` | `/api/runs/{run_id}` | 详情：记录 + 切片 + 是否可重放（`replayable`） |
| `DELETE` | `/api/runs/{run_id}` | 删除一条记录 |
| `DELETE` | `/api/runs` | 清空记录 |
| `POST` | `/api/runs/{run_id}/replay` | 用同一份文件与当前生效编排重放（原始文件已被淘汰时返回 `410`） |

**知识库**

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/knowledge/indexes` | 已建立的向量索引列表（含向量总数） |
| `DELETE` | `/api/knowledge/indexes/{run_id}` | 删除某个向量索引 |
| `POST` | `/api/knowledge/search` | 在指定索引上检索（同 `/api/search`，默认值取自设置） |

### 7.6 平台设置项（Schema）

| 分组 | 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|---|
| `platform` | `name` / `locale` / `timezone` | str | `Agent 平台` / `zh-CN` / `Asia/Shanghai` | 展示用基础标识 |
| `upload` | `max_mb` | int | `20` | 单文件上限（MB），1–500 |
| | `allowed_extensions` | list | 常见文本/PDF/Word 后缀 | 逗号分隔；留空不限制 |
| | `max_chunks_returned` | int | `50` | 接口返回切片上限（不影响入库） |
| `runtime` | `max_cached_runs` | int | `200` | 内存中保留的运行上下文数 |
| | `enable_draft_trial` | bool | `true` | 关闭后 `/api/upload?use=draft` 返回 `403` |
| | `auto_apply_profile` | str | `""` | 启动即加载并生效的编排方案名 |
| `retrieval` | `default_top_k` | int | `5` | 检索接口未传参时的默认条数 |
| | `score_threshold` | float | `0.0` | 相似度下限，低于该分数的命中被过滤 |
| `observability` | `persist_runs` | bool | `true` | 运行记录是否写入数据库 |
| | `run_history_limit` | int | `200` | 历史记录条数上限 |
| `auth` | `require_login` | bool | `true` | 全局登录校验开关 |
| | `allow_registration` | bool | `true` | 自助注册开关 |
| | `session_hours` | int | `72` | 会话有效期（小时）；勾选「记住我」至少保留 30 天 |
| | `docs_access` | str | `member` | 接口文档可见范围：`member` 所有登录用户 / `admin` 仅管理员 / `public` 匿名公开 |
| `plugins` | `disabled` | list | `[]` | 已停用插件，由技能库页面管理 |
| `providers` | — | list | `[]` | 模型供应商凭据（Schema 外，走专用接口维护）：`openai` / `azure_openai` / `ollama` / `custom` |

**上传返回示例**

```json
{
  "run_id": "981f98f0ff1a",
  "stats": { "chars": 168, "clean_chars": 135, "chunk_count": 1, "embedding_dim": 256 },
  "artifacts": [
    { "kind": "raw_file", "producer": "upload", "size": "612 bytes" },
    { "kind": "text", "producer": "text_loader", "size": "168 chars" },
    { "kind": "clean_text", "producer": "basic_cleaner", "size": "135 chars" },
    { "kind": "chunks", "producer": "recursive_splitter", "size": "1 items" },
    { "kind": "embeddings", "producer": "hash_embedder", "size": "1 items" },
    { "kind": "vector_index", "producer": "milvus_store", "size": "1 items" }
  ],
  "trace": [
    { "agent": "ingestion_agent", "skill": "text_loader", "status": "ok", "duration_ms": 0.07 },
    { "agent": "ingestion_agent", "skill": "basic_cleaner", "status": "ok", "duration_ms": 0.14 },
    { "agent": "chunking_agent", "skill": "recursive_splitter", "status": "ok", "duration_ms": 0.05 },
    { "agent": "indexing_agent", "skill": "hash_embedder", "status": "ok", "duration_ms": 0.43 },
    { "agent": "indexing_agent", "skill": "milvus_store", "status": "ok", "duration_ms": 0.02 }
  ]
}
```

`trace` 会记录每一步的 Agent / Skill / 状态 / 耗时，失败时精确定位到环节。

---

## 8. 目录结构

```
agent-full/
├── run.py                        # 启动脚本（uvicorn 127.0.0.1:8000）
├── requirements.txt
├── .env.example                  # ★ 公共环境变量模板（复制为 .env；含 Milvus 连接配置）
├── config/
│   ├── pipeline.yaml             # ★ 静态编排（启动时的默认流水线）
│   └── orchestrations/           # ★ 示例方案 YAML（可导入 / 导出，非运行时存储）
├── data/
│   ├── platform.db               # ★ 平台数据库（设置/账号/运行记录/知识库/方案/审计）
│   └── uploads/                  # ★ 上传的原始文件（支撑重放）
├── deploy/
│   └── milvus/
│       └── docker-compose.yml    # 本地 Milvus Standalone（milvus_store 默认后端）
├── app/
│   ├── main.py                   # FastAPI 入口：装配 → 登录校验中间件 → 挂载静态页
│   ├── runtime.py                # 运行时：设置 / 流水线 / 草稿 / 方案 / 运行记录 / 账号
│   ├── core/
│   │   ├── context.py            # PipelineContext：数据总线 + 产物 + 轨迹
│   │   ├── skill.py              # Skill 协议 + 注册中心（槽位 / 启停 / 目录热插拔）
│   │   ├── agent.py              # Agent：Skill 串联 + 契约校验
│   │   ├── orchestrator.py       # Pipeline：多 Agent 串联（不可变执行体）
│   │   ├── orchestration.py      # ★ Orchestration：可变草稿 + 软校验 + 自动补全
│   │   ├── config.py             # YAML → Pipeline 装配
│   │   ├── db.py                 # ★ 数据库引擎（多方言）+ 密钥封装 + 审计
│   │   ├── settings.py           # ★ 设置中心：Schema 驱动 + 落库 + 密钥掩码
│   │   ├── runs.py               # ★ 运行记录：RunRecord 摘要 + 落库
│   │   ├── auth.py               # ★ 账号与会话：用户库 + 令牌签发 / 吊销（落库）
│   │   ├── storage.py            # ★ 原始文件对象存储（落盘 + 登记）
│   │   ├── profiles.py           # ★ 编排方案：落库 + YAML 导入导出
│   │   └── warehouse.py          # 向量仓库：内存缓存 + 落库元数据，重启可重建
│   ├── plugins/                  # 内置插件（按槽位分文件）
│   │   ├── loaders.py            # text / pdf / docx
│   │   ├── sql_loaders.py        # sql_loader
│   │   ├── cleaners.py
│   │   ├── splitters.py
│   │   ├── embedders.py
│   │   ├── stores.py             # memory_store
│   │   └── milvus_store.py       # ★ milvus_store：切片向量写入 Milvus，按 run 隔离检索
│   └── api/
│       ├── routes.py             # 健康检查 / 插件 / 流水线 / 上传 / 检索 / 热重载
│       ├── orchestration_routes.py  # ★ 编排接口
│       ├── auth_routes.py        # ★ 账号与会话接口（登录/注册下发会话 Cookie）
│       ├── platform_routes.py    # ★ 设置 / 技能库 / 运行记录 / 知识库
│       └── docs_routes.py        # ★ 需登录的接口文档：/docs /redoc /openapi.json
├── ext_plugins/                  # ★ 外部插件目录：丢 .py 进来即生效
│   └── example_sensitive_word_cleaner.py
├── web/
│   ├── index.html                # ★ 工作台外壳（导航 + 顶栏 + 视图容器 + 登录门）
│   ├── ci-pipeline.html          # 示例页：CI 流水线运行详情
│   └── assets/
│       ├── app.js                # ★ 前端：hash 路由 / 视图 / 状态 / 动效
│       └── app.css               # ★ 视觉体系：主题变量 / 组件 / 动画
└── tests/
    ├── test_pipeline.py          # 流水线端到端 + Milvus 落库（26）
    ├── test_orchestration.py     # 编排 / 诊断 / 补全 / 方案（20）
    ├── test_skill.py             # Skill 协议与注册中心（50）
    ├── test_platform.py          # 设置 / 运行记录 / 账号 / HTTP 接口（70）
    └── test_script.py            # 脚本执行器与 /api/scripts 准入（32）
```

---

## 9. 测试覆盖

共 **212 个用例**（`python -m unittest discover -s tests -v`）。2026-09-11 实测：**210 通过 / 2 跳过**
（跳过的是依赖本机 `sh` / `bash` 的脚本用例，Windows 上未安装这两个 shell）。

`tests/test_pipeline.py`（33 个）—— 流水线本身：

- **插件注册**：内置槽位齐全、外部目录插件被加载、同槽位多实现、`milvus_store` 与 `memory_store` 同槽位
- **契约校验**：故意打乱顺序会在编译期报错；默认流水线产物完整；生效流水线的入库环节是 `milvus_store`，
  且其连接参数来自 `.env` 的 `${MILVUS_*}`（装配后已是真实值）
- **端到端**：全链路产物齐全、清洗生效（去重 / 脱敏 / 保留链接）、切片与向量一一对应、轨迹完整
- **检索**：索引写入仓库、相似度检索命中关键词
- **Milvus 落库**：切片向量真的写进集合（实体数与切片数一致）、同一集合内不同 run 互不串库、
  删除索引会清掉该 run 的向量、维度 / 度量不一致与非法集合名直接拒绝（Milvus 未启动时整组跳过）
- **可插拔**：换 `markdown_splitter` / `fixed_splitter` / 外部 cleaner 后链路依然成立
- **数据库加载**：`sql_loader` 注册到 loader 槽位、建表查询渲染为文本、自定义 SQL 与行数上限、非法表名被拒、可直接替换 `text_loader`

`tests/test_orchestration.py`（20 个）—— 运行时编排：

- **软校验诊断**：健康编排无 issue；删步骤后精确定位断裂点并给出槽位级建议；
  无生产者时提示需新增插件；非法参数被识别；重复生产提示覆盖
- **自动补全**：断裂链路被修复且修复后真的能跑通；健康编排下为空操作
- **编译**：健康可编译；断裂抛 `ContractError`；停用步骤后其产物不再向下传播
- **草稿生命周期**：`touch → dirty → apply → clean`；试跑不影响生效配置；重置回到生效结构，
  且反推的草稿保留 `${MILVUS_URI}` 这类占位符原文（不把 `.env` 里的真实地址带进方案）
- **方案存档**：保存 / 列表 / 加载 / 删除往返一致；非法方案名被拒绝；随仓库附带的示例方案可跑通

`tests/test_skill.py`（50 个）—— Skill 协议与注册中心：

- **元数据**：抽象类不可实例化、`param_schema` 派生默认参数、`manifest` 内容与副本隔离
- **注册**：重名 / 空名 / 非 Skill 类被拒、装饰器注册、注销幂等、注册中心相互隔离
- **启停**：`disabled` 往返、未知插件行为、`names` 与 `all_names` 的区别
- **查询**：`slots` 分组、同槽位筛选排除停用项、生产者 / 消费者反查、`catalog` 结构
- **实例化**：默认参数、传参、每次返回新实例、停用 / 未注册时的友好报错
- **发现**：内置包扫描与外部目录加载（跳过 `_` 前缀文件、重复加载幂等、对文件路径与缺失目录的容错）

`tests/test_platform.py`（77 个）—— 平台管理与 HTTP 接口：

- **设置**：Schema 派生默认值、局部更新落库、非法值不落库、列表字段接受逗号串、恢复默认
- **密钥掩码**：掩码规则、供应商密钥永不明文返回、回传掩码值时保留原密钥
- **运行记录**：记录 / 列表 / 详情 / 统计、持久化重载、超限淘汰、删除与清空
- **账号**：首次运行播种管理员、密码哈希往返、令牌签发 / 篡改 / 过期 / 吊销、
  重启后会话可恢复但吊销状态不恢复、退出其他设备、用户 CRUD 与唯一性、
  最后一个管理员受保护、停用账号无法登录、主题白名单过滤
- **HTTP 接口**：匿名请求被拒、白名单放行、登录失败与坏令牌、关闭登录校验后可匿名访问、
  资料与偏好往返、改密流程、登出吊销、会话管理、注册与管理员增删改查、非管理员越权被拒、
  概览与设置接口、导出不含明文密钥、技能详情 / 沙盒试跑 / 启停与回滚、
  上传入库与知识库联动、上传准入（空文件 / 扩展名 / 大小上限 / 关闭草稿试跑）、
  检索默认值取自设置、重放与删除
- **接口文档鉴权**：未登录 `/docs` 重定向回登录门（`302` + `auth=required`）、`/openapi.json` 返回 401、
  登录后靠会话 Cookie 访问文档且页面展示当前账号、登出后 Cookie 立即失效、
  关闭登录校验后文档对匿名开放
- **文档按角色放开**：`docs_access` 取值必须在白名单内（非法值 422 且不落库）、
  `admin` 策略下普通账号 403 / 跳 `?auth=forbidden` 而管理员照常、
  `public` 策略下匿名可看文档但业务接口仍 401

---

## 10. 扩展路线

| 方向 | 做法 |
|---|---|
| 真实向量库 | ✅ `milvus_store` 已接入；再扩 `chroma_store` / `qdrant_store` 只需实现 `search()` 即可互换 |
| 语义切片 | 新增 `semantic_splitter`：先向量化再按相似度断点切分 |
| OCR | 新增 `ocr_loader`，与 `pdf_loader` 同槽位 |
| 异步/并行 | `Pipeline.run` 中按产物依赖构造 DAG，无依赖的 Agent 并行执行 |
| 持久化 | ✅ 平台状态已统一落库、`warehouse` 元数据重启可重建；可再替换为 Redis 等外部实现，保持 `SearchableIndex` 协议 |
| 分布式 | 把 `Skill.run` 换成 RPC 调用，Agent 即可跨进程编排 |
| 多租户 | 在 `User` 上扩展 `tenant` 字段，`Warehouse` / `RunRegistry` 按租户分片 |
| 审计日志 | ✅ 已有 `audit_log` 表与 `audit / list_audit / audit_stats`；可继续扩展为更细粒度的操作审计 |


## 11. 部署方案（Docker / Kubernetes）

三种形态按场景选，配置注入规则三处一致：**系统环境变量 > `.env` > 代码默认值**（见 5.1）。

| 形态 | 适用场景 | 前置条件 | 入口 |
|---|---|---|---|
| 本机直跑 | 开发 / 调试 | Python 3.10+、Milvus | `python run.py`（第 4 节） |
| Docker Compose | 单机交付 / 试点 / 演示 | Docker + Compose v2 | `deploy/docker-compose.yml` |
| Kubernetes | 企业内网 / 多环境 / 离线 | 集群 + 镜像仓库 | `deploy/k8s/app.yaml` |

> 容器与集群里**不需要挂 `.env`**：把值用 `environment` / ConfigMap / Secret 注入即可，
> 平台启动时的 `load_dotenv()` 不会覆盖已存在的系统变量，所以环境变量天然优先。

### 11.1 应用镜像（`deploy/Dockerfile`）

```bash
# 必须在项目根执行（构建上下文就是项目根）
docker build -t agent-platform:v1.0.0 -f deploy/Dockerfile .
```

- 基础镜像 `python:3.12-slim`，**没有前端构建步骤**（`web/` 是原生页面，直接 COPY 进镜像）。
- 可选依赖由 `--build-arg EXTRAS="..."` 控制，默认装默认入库后端与文档解析要用的
  `pymilvus` / `pypdf` / `python-docx`；要接 OpenAI 向量化或把平台库换成 PostgreSQL 时再追加
  `openai` / `psycopg2-binary`（`pymysql` 已在 `requirements.txt` 主依赖里）。
- 以非 root（UID `10001`）运行，`/app/data` 是唯一需要持久化的目录；容器内用
  `uvicorn --host 0.0.0.0` 启动（`run.py` 里的 `127.0.0.1` 只适合本机直跑）。
- 自带 `HEALTHCHECK`，打的是免登录的 `GET /api/health`，与 K8s 探针同一入口。
- `.dockerignore` 在项目根，把 `data/`、`.env`、`config/settings.json` 等**本地运行时状态**
  挡在镜像之外，避免把本机的库和密钥打进交付物。

### 11.2 Docker Compose 单机部署

```bash
# 1) 先起向量库（首次会拉镜像，健康检查有 90s 宽限期）
cd deploy/milvus && docker compose up -d

# 2) 回项目根，构建并启动应用
cd ../.. && docker compose -f deploy/docker-compose.yml up -d --build

# 3) 验证（返回 {"status":"ok",...} 即成功）
curl http://127.0.0.1:8000/api/health
```

打开 <http://127.0.0.1:8000> 即是工作台；首次运行会播种管理员 `admin` / `admin123`，**登录后请立刻改密**。

- **地址视角**：容器里的 `localhost` 是容器自己，不能用宿主机视角的 `localhost` 指宿主服务。
  所以 compose 用 `APP_MILVUS_URI` 作为「容器视角地址」的覆盖点，默认
  `http://host.docker.internal:19530`（Linux 通过 `extra_hosts: host-gateway` 补出该域名）。
  想沿用项目根 `.env`：`docker compose -f deploy/docker-compose.yml --env-file .env up -d`
  —— 根 `.env` 里 `MILVUS_URI=http://localhost:19530` 是宿主机视角，不会被注入容器。
- **持久化**：平台状态全在具名卷 `agent-platform-data`（容器内 `/app/data`：`platform.db` +
  `uploads/` + `script_workspace/`）。`docker compose down` 不删卷，`down -v` 才删。
- **外部插件**：宿主机 `ext_plugins/` 以只读方式挂进容器，丢 `.py` 进去后在「技能库」点
  「重新扫描插件目录」即生效，无需重建镜像。
- **容器内自检**（与 CI 同一套用例，Milvus 不通时相关用例自动跳过）：
  ```bash
  docker compose -f deploy/docker-compose.yml run --rm app python -m unittest discover -s tests
  ```
- 常用运维：`docker compose -f deploy/docker-compose.yml logs -f app`、`... ps`、
  `... exec app python -c "import app.main"`（验证配置能否装配）、`... restart app`。

### 11.3 Kubernetes 部署

```bash
kubectl apply -f deploy/k8s/app.yaml
kubectl -n agent-platform rollout status deploy/agent-platform
kubectl -n agent-platform port-forward svc/agent-platform 8080:80   # 本地验证入口
```

清单共 8 个对象（一个文件，`kubectl apply` 顺序无关）：

| 对象 | 作用 | 部署前要改什么 |
|---|---|---|
| `Namespace/agent-platform` | 独立命名空间 | — |
| `ConfigMap/agent-platform-config` | `MILVUS_URI` / 集合 / 度量 / `TZ` / Python 行为 | ★ `MILVUS_URI` 指向集群内 Milvus 服务名或集群外地址 |
| `ConfigMap/agent-platform-ext-plugins` | 需要追加的 Skill 插件（`.py`） | 空目录也能启动，按需填 |
| `Secret/agent-platform-secret` | `PLATFORM_DATABASE_URL` / `MILVUS_TOKEN` | ★ 生产建议填外部数据库连接串 |
| `PVC/agent-platform-data` | 承载 `/app/data`（平台库 + 上传原件） | `storageClassName` 与容量 |
| `Deployment/agent-platform` | 单副本、`Recreate`、非 root、只读根文件系统 | ★ `image` 换成自己的镜像仓库地址 |
| `Service/agent-platform` | 集群内 80 → 容器 8000 | — |
| `Ingress/agent-platform` | 对外域名与 TLS | ★ `host`；`proxy-body-size` 要与上传上限对齐 |

关键片段（完整内容见文件）：

```yaml
spec:
  replicas: 1
  strategy:
    type: Recreate            # RWO 卷 + 进程内状态：先停后起，不做双活滚动
  template:
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        fsGroup: 10001        # 让持久卷可被非 root 进程写入
      containers:
        - name: app
          envFrom:
            - configMapRef: {name: agent-platform-config}
            - secretRef: {name: agent-platform-secret}
          volumeMounts:
            - {name: data, mountPath: /app/data}
            - {name: ext-plugins, mountPath: /app/ext_plugins, readOnly: true}
            - {name: tmp, mountPath: /tmp}      # 只读根文件系统下留给临时文件
          startupProbe:        # 首次装配要扫插件 + 校验契约，给足 150s
            httpGet: {path: /api/health, port: http}
            periodSeconds: 5
            failureThreshold: 30
          readinessProbe:
            httpGet: {path: /api/health, port: http}
            periodSeconds: 10
          livenessProbe:
            httpGet: {path: /api/health, port: http}
            periodSeconds: 20
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: ["ALL"]}
```

**集群内的 Milvus**：用官方 Helm chart 起一个 Standalone，再把
`ConfigMap` 里的 `MILVUS_URI` 指向 `milvus.agent-platform.svc.cluster.local:19530`：

```bash
helm repo add milvus https://zilliztech.github.io/milvus-helm/ && helm repo update
helm install milvus milvus/milvus -n agent-platform \
  --set cluster.enabled=false --set etcd.replicaCount=1 \
  --set minio.mode=standalone --set pulsar.enabled=false
```

> chart 版本与 Milvus 版本需匹配（本仓库 `deploy/milvus` 用的是 `v3.0.0`），以官方安装文档为准；
> 也可以直接用集群外的 Milvus，把 `MILVUS_URI` 填成可路由地址即可。

**副本数的硬约束**（别急着 `kubectl scale`）：平台状态虽已落库，但「编排草稿」「运行上下文缓存」
仍在进程内，且默认数据卷是 `ReadWriteOnce`。要横向扩展，先满足两个前提：把
`PLATFORM_DATABASE_URL` 换成外部数据库、并接受草稿不共享（工作台建议单副本，或给 Ingress
加会话亲和把同一用户固定到一个副本）。

### 11.4 离线（内网）部署

镜像自带全部 Python 依赖与静态前端，内网无需 pip / npm / 外网。

```bash
# 有外网的机器
docker build -t agent-platform:v1.0.0 -f deploy/Dockerfile .
docker pull python:3.12-slim
docker save agent-platform:v1.0.0 python:3.12-slim | gzip > agent-platform-v1.0.0.tar.gz

# 内网机器：导入后推到内网仓库
gunzip -c agent-platform-v1.0.0.tar.gz | docker load
docker tag agent-platform:v1.0.0 harbor.internal/library/agent-platform:v1.0.0
docker push harbor.internal/library/agent-platform:v1.0.0
```

K8s 侧把 `Deployment` 的 `image` 换成内网仓库地址即可（`imagePullPolicy: IfNotPresent` 便于配合节点预导入）；
`etcd` / `minio` / `milvusdb/milvus` 三个镜像同理离线导入。新增外部插件若依赖第三方包，
需自建镜像或内网 PyPI。

### 11.5 配置项与密钥

| 变量 | 作用 | 默认 | 容器 / 集群里怎么给 |
|---|---|---|---|
| `PLATFORM_DATABASE_URL` | 平台库（设置 / 账号 / 会话 / 运行记录 / 知识库索引 / 编排方案 / 审计） | 空 = `data/platform.db`（SQLite） | Secret；生产建议 `mysql://…` / `postgresql://…` |
| `MILVUS_URI` | 向量库地址 | `http://localhost:19530` | ConfigMap（**注意视角**：容器里写容器名 / 宿主域名） |
| `MILVUS_COLLECTION` | 集合名（一个集合靠 `run_id` 隔离所有上传） | `default` | ConfigMap |
| `MILVUS_METRIC_TYPE` | `COSINE` / `IP`，首次建集合即固定 | `COSINE` | ConfigMap |
| `MILVUS_TOKEN` | 鉴权 `user:password` | 空 | Secret |
| `TZ` / `PYTHONUNBUFFERED` / `PYTHONDONTWRITEBYTECODE` | 时区与 Python 运行行为 | `Asia/Shanghai` / `1` / `1` | ConfigMap |

真实地址与口令只应出现在 Secret 或 `.env` 里：方案文件与接口里始终是 `${MILVUS_*}` 占位符原文。

### 11.6 备份、升级与数据库迁移

**备份三样**：平台库、`data/uploads/`（原始文件，支撑重放）、向量库（Milvus 集合或其数据卷）。

```bash
# 平台库 + 上传原件（Linux / macOS；PowerShell 用 ${PWD} 代替 "$PWD"）
docker run --rm -v agent-platform-data:/data -v "$PWD:/backup" alpine \
    tar czf /backup/agent-platform-data.tar.gz -C /data .
```

**升级 / 回滚**：启动时会自动建表并升级 Schema（`app/core/db.py` 的 `SCHEMA_VERSION`）。

```bash
docker compose -f deploy/docker-compose.yml up -d --build        # 单机
kubectl -n agent-platform set image deploy/agent-platform app=harbor.internal/library/agent-platform:v1.1.0
kubectl -n agent-platform rollout status deploy/agent-platform
kubectl -n agent-platform rollout undo deploy/agent-platform      # 回滚
```

**换库迁移**（SQLite → MySQL / PostgreSQL）：只改 `PLATFORM_DATABASE_URL` 不会搬数据，
用仓库自带的一次性脚本按表搬运（先停应用，避免两边同时写入）：

```bash
docker compose -f deploy/docker-compose.yml stop app
docker compose -f deploy/docker-compose.yml run --rm app \
    python scripts/migrate_platform_db.py \
    --target "mysql://app:app123456@mysql:3306/agent_platform?charset=utf8mb4" --yes
```

脚本会输出每张表的行数对比，并专门核对 `kv.security.key` —— 模型供应商 API Key 的加密密钥，
漏搬会导致旧库里的密钥在新库里解不开。

### 11.7 上线检查清单

| 项 | 建议 |
|---|---|
| 管理员口令 | 首登改密（`admin` / `admin123` 带 `must_change_password` 标记） |
| 自助注册 | 系统设置 → 登录与安全：`auth.allow_registration=false` |
| 接口文档 | `auth.docs_access=admin`（默认所有登录用户可见） |
| 脚本验证 | 容器里同样能跑 Python / Shell，等同于给使用者一个终端：对外部署建议 `script.admin_only=true` 或 `script.enabled=false`；容器内没有 `pwsh`，PowerShell 会显示为不可用 |
| 上传上限 | `upload.max_mb`（默认 20）与 Ingress `proxy-body-size` 对齐，否则大文件被网关先挡回 413 |
| 数据持久化 | `/app/data` 必须挂持久卷；容器重建、滚动升级都不该丢数据 |
| 副本数 | 默认单副本 `Recreate`；扩副本前先换外部数据库并解决草稿共享（见 11.3） |
| 备份 | 平台库 + uploads + Milvus 三样定期备份，并**做过恢复演练** |
| 日志与审计 | `docker compose logs -f app` / `kubectl -n agent-platform logs -f deploy/agent-platform`；操作留痕查 `audit_log` 表 |

---

## 12. Agent示例界面
### 概览
![alt text](docs/images/agent-overview.png)
### 编排工作台
![alt text](docs/images/orchestration-workbench.png)
### 技能库
![alt text](docs/images/skills-library.png)
### 运行记录
![alt text](docs/images/run-records.png)
### 知识库 
![alt text](docs/images/knowledge-base.png)
### 个人中心
![alt text](docs/images/profile.png)
### 主题外观
![alt text](docs/images/theme.png)
### 用户管理
![alt text](docs/images/user-management.png)
### 系统设置
![alt text](docs/images/system-settings.png)
### API管理
![alt text](docs/images/api-management.png)