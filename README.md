# 寻迹（Xunji）博物馆 AI 导览

寻迹是一个面向博物馆参观场景的导览 Agent 原型，支持“规划路线 → 按需讲解 → 追问/续播 → 动态重规划 → 生成探索手记”的完整演示流程。项目当前使用虚构演示场馆和可注入模型服务，便于在真实馆藏、空间通路和知识资料接入前验证接口、状态管理和页面交互。

> 当前版本是本地演示和开发底座，不是生产部署版本。`data/` 下的 60 件国博 demo 文物、45 条知识关联、4 个展厅和 8 条路线已接入讲解选材与路线规划；`data/map.json` 是仅用于功能测试的虚拟空间点到点地图，不代表实际馆内通路。

## 能做什么

- 记录独立游览会话和五类游客信息：内容兴趣、讲解风格、讲解角度、剩余时间、其他需求。
- 根据游客表达解析兴趣，按标签与向量相似度筛选展品和展厅，并通过有向通路生成路线。
- 生成主讲稿、追问回答和可恢复的分句播放状态；主讲成功生成后写入参观记录，播放进度单独确认。
- 在时间、兴趣、位置或其他需求改变时重新规划；跳过展品不会删除历史记录。
- 结束游览后根据已生成讲解、问答和反馈整理探索手记，并支持失败后重试。
- 提供浏览器 H5 页面与 JSON HTTP 接口，Router 可通过依赖注入接入真实数据源和模型。

## 目录

整理后的目录按职责分组如下（如果你从旧版根目录迁移文件，请保持这些职责和相对关系）：

```text
agent_v0/
├─ backend/                  Python 后端与模型编排
│  ├─ main.py                本地 HTTP 入口
│  ├─ router.py              会话级中央路由与事件调度
│  ├─ operators.py           四个算子的统一导出
│  ├─ session_state.py       游客、路线、讲解和手记状态
│  ├─ route_*.py             兴趣解析、评分、图搜索和路线规划
│  ├─ guide_*.py             讲解数据契约、提示词和生成流程
│  ├─ summary_*.py           手记数据契约、提示词和生成流程
│  ├─ model_api.py           LLM/Embedding HTTP 客户端
│  ├─ model_services.py      五项模型职责及本地材料加载的装配
│  └─ demo_museum.py         虚构演示馆与演示服务
├─ frontend/                 浏览器端页面
│  ├─ index.html
│  ├─ app.js
│  └─ style.css
├─ data/                     馆藏、展品关系和手记基础资料
│  ├─ relics.json
│  ├─ edges.json
│  ├─ galleries.json
│  ├─ routes.json
│  └─ map.json              # 虚拟空间步行图（与知识关联边分离）
├─ tests/                    单元测试与 HTTP 集成测试（test_*.py）
├─ docs/                     需求、交接、架构图和评估报告
└─ README.md
```

源码已按职责整理到上述目录；`backend/main.py` 默认发布 `frontend/`，模型配置从 `config/llm_config.json` 读取，测试通过 `tests/__init__.py` 加载后端模块。

## 快速开始

### 1. 准备 Python

项目约定使用 Python 3.13。Windows 本机可直接使用：

```powershell
D:\tools\anaconda\python.exe --version
```

项目运行只使用 Python 标准库，不需要额外安装依赖。

### 2. 配置模型密钥（可选）

演示模式需要调用模型服务时，从环境变量读取 Paratera API Key。不要把真实密钥提交到配置文件或 README：

```powershell
.\scripts\set_lumora_api_key.ps1 -PersistUser
# 或仅对当前 PowerShell 会话生效
.\scripts\set_lumora_api_key.ps1
```

仓库只提供不含真实密钥的 `config/llm_config.example.json`。本地可另建被 Git 忽略的 `config/llm_config.json` 调整非敏感参数。环境变量会覆盖配置文件中的同名字段；如果要清除当前会话变量，可运行：

```powershell
.\scripts\set_lumora_api_key.ps1 -Clear
```

### 3. 启动本地演示

在项目根目录 `agent_v0/` 运行：

```powershell
D:\tools\anaconda\python.exe -B backend\main.py
```

然后打开 <http://127.0.0.1:8000/>。服务默认只监听本机回环地址。按 `Ctrl+C` 停止。

如果端口 8000 已被本项目旧实例占用，应先停止已确认的旧实例；不要启动多个同端口服务，也不要结束其他 Python 程序。

### 4. 通过校园网访问

运行下面的命令后，服务会检测当前默认网络的 IPv4 地址并打印访问网址：

```powershell
D:\tools\anaconda\python.exe -B backend\main.py --lan
```

换网络或校园网重新分配 IP 后，在另一个 PowerShell 窗口运行下面的命令重新查询：

```powershell
.\scripts\show_lan_url.ps1
```

访问者需要连接到允许设备互访的同一网络。校园 Wi-Fi 的客户端隔离或 Windows 防火墙可能阻止连接。本机局域网模式没有身份验证，只适合可信网络里的临时演示；不要在公共网络开启。演示服务的会话保存在进程内存中，服务重启后会清空。

## HTTP 接口

所有接口返回 JSON。除健康检查和创建会话外，业务响应统一为：

```json
{
  "reply_content": {"status": "ok", "message": "..."},
  "latest_state": {"session_id": "...", "status": "active"}
}
```

### 公开接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 返回服务状态和当前模式 |
| `GET` | `/api/museum` | 返回演示馆名称、位置、展品和推荐路线预览 |
| `POST` | `/api/session` | 创建新游览会话；演示模式默认使用入口大厅和出口 |
| `GET` | `/api/state?session_id=...` | 读取服务端保存的最新状态 |
| `POST` | `/api/chat` | 发送自然语言需求、追问或结束意图 |
| `POST` | `/api/event` | 发送按钮事件和结构化参数 |

创建会话示例：

```powershell
$session = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/session `
  -ContentType 'application/json' -Body '{"pace":"standard"}'
$session
```

按钮事件的 `action` 包括：

- `submit_preferences`、`update_visitor_info`
- `replan`
- `start_guide`、`skip`
- `update_location`
- `playback_progress`、`resume_guide`
- `finish`、`generate_summary`
- `get_state`

示例：

```powershell
$body = @{
  session_id = $session.latest_state.session_id
  action = 'submit_preferences'
  payload = @{
    visitor_info = @{
      content_interests = @('青铜器制作')
      guide_styles = @('生动', '通俗')
      guide_focus = @('制作过程')
      remaining_minutes = 45
      other_needs = @('亲子同行')
    }
  }
} | ConvertTo-Json -Depth 6
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/event -ContentType 'application/json' -Body $body
```

页面会使用服务端返回的 `reply_content.playback.current` 恢复原稿及已确认的连续分句；浏览器 SpeechSynthesis 只在整段播放完成后回报播放进度。设备没有可用语音时仍可阅读文本。

## 后端结构

Router 是会话级调度层。每个 `session_id` 拥有独立的 `SessionState` 和异步锁；状态修改与正式提交持锁，主讲准备和后台预生成在锁外等待，不同会话可以并发。

四个主要算子及职责：

1. `op_process_input()`：把结构化字段或自然语言整理为五类游客信息、当前展品问题、重规划/结束意图。
2. `op_plan_route()`：解析兴趣、评分展品、聚合展厅、计算通路、按时间裁剪并提交待看队列。
3. `op_generate_guide()`：本地加载当前展品全部候选资料，在一次模型请求内生成主讲或回答并自检，本地校验通过后保存讲解记录。
4. `op_generate_summary()`：收集参观记录、讲解问答和反馈，生成并核对探索手记。

模型服务统一由 `build_model_services()` 装配，全部文本职责默认使用 Paratera 的 `GLM-5.3-Flash`，沿用现有接口与密钥；向量服务使用 `GLM-Embedding-3`。默认有五项模型职责：输入解析、兴趣解析、讲解生成与自检、手记生成、手记独立审核。应用层可注入自定义 `RouterServices`、数据源、模型调用和向量服务，测试无需真实网络请求。

讲解生成默认启用联网搜索（配置 `guide_web_search`，环境变量 `LUMORA_LLM_GUIDE_WEB_SEARCH` 可覆盖）：请求附加 GLM 服务端 `web_search` 工具，仅讲解生成生效。联网模式使用完整的证据策略：先把追问或指定讲解角度拆成事实清单，只有候选材料对事实有直接陈述才算覆盖；同主题、关键词相似、一般背景或稀疏泛化材料不算充分依据。存在必要信息缺口、涉及最新信息或明确要求查询核实时，要求下一步先检索再撰稿；检索后仍无可靠依据才说明无法确认。材料充分且无需核查时可直接撰写。候选材料中已明确的展品事实优先，可靠网络资料可补充缺失事实与背景，不能把同类器物的经历套到本件。使用网络资料时自然说明"根据公开网络资料"，正文不罗列网址；`used_material_ids` 只跟踪实际使用的本地候选材料，不代替网络来源。关闭联网时仅允许使用本地材料。默认超时为 240 秒；提示词要求仍需通过真实搜索元数据验证，不能保证每次必要检索都触发。

讲解链路已从三次串行模型请求减为一次；聊天追问还需一次输入解析。全部候选仅指当前目标展品的材料。生成内自检不等同于独立审核，事实忠实度与实际提速幅度仍需实测；严格 JSON、材料引用、播放上下文、会话变化检查及失败不提交的约束继续保留。需要独立审核时仍可显式注入原适配器并使用 `review_mode="independent"`。

## 下一件讲解预生成

首件讲解正式返回后，Router 默认在后台准备待看队首的一件讲稿；之后每开始一件，再准备下一件。缓存只在当前服务进程的会话内保存，准备完成不会写入讲解或参观记录、移除队首或自动播放。点击“开始讲解”时重新核对目标、版本和时间：准备好就直接提交，仍在生成就等待同一请求，失败则在此次点击时重新生成。

纯追问、暂停续播、刷新和自然倒计时保留缓存。倒计时导致路线预算不足时，自动重规划复用上次成功规划的馆藏快照、兴趣解析和查询向量，不再请求模型；若队首和讲解所需信息未变，继续复用已就绪或生成中的讲稿。实际偏好变化、显式时间重设、主动重规划、位置或出口变化、跳过会使旧稿失效；自动重规划改变队首或失败时也作废。结束或时间用尽时清空缓存。已发送的旧 HTTP 允许完成，但结果不会再被使用；连续变化只在旧后台请求结束后准备最新目标。后台等待不持会话锁，追问不会排在后台配额后面。

`Router(..., prefetch_enabled=True, prefetch_concurrency=2)` 默认开启，每会话只保留下一件，全局后台并发上限为 2；自定义服务可关闭。`reply_content.guide_prefetch` 只返回 `status`（`idle/generating/ready/failed`）和 `relic_id`，不下发缓存稿。页面在现有状态响应和播放进度回报后更新队首提示，不新增轮询；应用关闭时调用 `await router.aclose()` 清理任务。

2026-10-22 真实 `GLM-5.3-Flash` 验证：`NMC-CQ-104` 生成 32.42 秒，下一件 `NMC-CQ-105` 后台生成 36.20 秒；B 准备完成时仍只有 A 的正式记录，点击 B 后后端取稿约 0.0008 秒，总共仅两次模型调用。该单次取稿耗时不含浏览器、网络或语音启动时间；首件和未准备完成的下一件仍需等待生成。

## 测试与检查

在测试目录所在的项目目录运行：

```powershell
D:\tools\anaconda\python.exe -B -m unittest discover -s tests -t . -q
```

前端语法检查：

```powershell
node --check frontend\app.js
```

2026-10-22 全量 245 项 unittest 通过；本轮在 218 项基线上新增 6 项准备与提交边界、20 项预生成并发与失效、1 项 HTTP 缓存复用测试。覆盖模型 API、输入/路线/讲解/手记算子、Router 会话状态、演示 HTTP 全流程、刷新恢复、静态资源边界和非法请求回归，并保留单次讲解请求、全候选输入、自检模式、异常不提交及追问续播验证。旧模型联调与浏览器语音播放已有验证；最初 `DeepSeek-V4.1-Flash-1017` 请求返回 HTTP 403（`team_model_access_denied`），改用 `DeepSeek-V4.1-Flash` 后仍返回 HTTP 403（客户端 `authentication_failed`），未成功生成。随后按用户要求改用 `GLM-5.3-Flash`，真实 `demo-pot` 主讲单次 `guide_draft` 请求成功，耗时 23.31 秒，返回 252 字、6 个播放单元；`integrated` 模式以 `review=None` 正常保存。该结果仅是一个样本，不能代表稳定速度或质量，尚未全面核验生成内容的语义忠实度。

2026-10-22 首次讲解联网改造后全量 247 项 unittest 通过（新增联网载荷与装配 2 项）。真实请求验证：Paratera 接受带 `web_search` 工具的载荷，`finish_reason=stop` 且严格 JSON 解析正常，带工具的主讲样本 84 秒 / 382 字、追问样本 39–92 秒；用短提示词的对照请求确认网关真实执行搜索（正确返回 2026 年 10 月 8 日公布的时效信息，搜索结果注入约 2800 tokens）。当时长提示词仍限制全部事实只能来自候选材料，多数样本选择不检索；本轮已改为按联网开关选择一致的证据策略，消除该冲突。此前耗时与触发行为不能代表新策略表现。

补充当前馆藏链路实测：`NMC-CQ-104` 主讲单次请求 28.59 秒，返回 164 字、4 个播放单元；资料不足追问单次请求 13.65 秒，返回 110 字、3 个播放单元，并明确提示资料不足。两次均仅发送一次 `guide_draft`；主讲和回答共保存两条记录，已参观展品仍为一件，原稿续播上下文保留。以上与演示主讲的 23.31 秒均为各一次样本，不是稳定均值，也未完成全面语义质量验证。

## 当前限制与下一步

生产化前需要补齐：

- 真实展品资料、权威来源、标签、重要度和展品向量；
- 真实展厅、楼层、入口、出口、通路和步行时间（当前 `map.json` 仅为测试数据）；
- 讲解候选资料已按展品 ID 读取 `relics.json`，仍需权威资料复核；
- 持久化会话、路线、播放位置和手记；
- 用户身份、访问限制、多进程协调和正式运行配置；
- 真实 TTS/音频、暂停、追问与续播链路。

边生成边播放、未播内容自动调整、重复讲解检测、手记图片或 PDF 导出属于后续可选能力。

## 开发交接约定

开始新一轮开发前先阅读 `docs/开发交接.md` 和 `docs/需求文档.txt`，再核对相关源码与测试。修改字段或接口时同步更新交接文档；不要把 API Key、游客原话或模型响应体写入日志。生产场馆必须替换演示快照、候选史料、基础资料、向量编码及参数校准。

