# 寻迹（Xunji）博物馆 AI 导览

寻迹是一个面向博物馆参观场景的导览 Agent 原型，支持“规划路线 → 按需讲解 → 追问/续播 → 动态重规划 → 生成探索手记”的完整演示流程。项目当前使用虚构演示场馆和可注入模型服务，便于在真实馆藏、空间通路和知识资料接入前验证接口、状态管理和页面交互。

> 当前版本是本地演示和开发底座，不是生产部署版本。演示页面会明确标识虚构场馆；`data/relics.json` 与 `data/edges.json` 目前为空，真实馆藏资料尚未接入。

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
│  ├─ model_services.py      七项模型职责的装配
│  └─ demo_museum.py         虚构演示馆与演示服务
├─ frontend/                 浏览器端页面
│  ├─ index.html
│  ├─ app.js
│  └─ style.css
├─ data/                     馆藏、展品关系和手记基础资料
│  ├─ relics.json
│  └─ edges.json
├─ tests/                    单元测试与 HTTP 集成测试（test_*.py）
├─ docs/                     需求、交接、架构图和评估报告
├─ requirements.txt
└─ README.md
```

源码已按职责整理到上述目录；`backend/main.py` 默认发布 `frontend/`，模型配置从 `config/llm_config.json` 读取，测试通过 `tests/__init__.py` 加载后端模块。

## 快速开始

### 1. 准备 Python

项目约定使用 Python 3.13。Windows 本机可直接使用：

```powershell
D:\tools\anaconda\python.exe --version
```

当前 `requirements.txt` 只保留标准库说明，项目运行不依赖额外 pip 包。

### 2. 配置模型密钥（可选）

演示模式需要调用七项模型职责时，从环境变量读取 Paratera API Key。不要把真实密钥提交到配置文件或 README：

```powershell
.\scripts\set_lumora_api_key.ps1 -PersistUser
# 或仅对当前 PowerShell 会话生效
.\scripts\set_lumora_api_key.ps1
```

仓库只提供不含真实密钥的 `config/llm_config.example.json`。本地可另建被 Git 忽略的 `config/llm_config.json` 调整非敏感参数；部署到 Render 时只在服务的 Environment 中填写 `LUMORA_LLM_API_KEY`，不要把密钥写入任何配置文件。环境变量会覆盖配置文件中的同名字段；如果要清除当前会话变量，可运行：

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

### 部署到固定网址（Render）

校园网 IP 会随网络变化，不能作为长期网址。项目已附带 `render.yaml`，可部署到 Render 获得固定的 `https://<服务名>.onrender.com` 地址：

1. 将 `agent_v0/` 推送到自己的 GitHub 仓库。
2. 在 Render 选择 **New > Blueprint**，连接仓库并选择 `render.yaml`。
3. 在服务的 Environment 中填写 `LUMORA_LLM_API_KEY`，不要把密钥写进代码或仓库。
4. 部署完成后打开 Render 提供的 `https://...onrender.com/`，以后不再使用校园网 IP。

Render 会自动注入 `PORT`；`backend/main.py` 已支持云平台端口和 `0.0.0.0` 监听。演示服务的会话目前保存在进程内存中，服务重启后会话会清空。

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

Router 是会话级调度层。每个 `session_id` 拥有独立的 `SessionState` 和异步锁，同一会话请求串行处理，不同会话可以并发。

四个主要算子及职责：

1. `op_process_input()`：把结构化字段或自然语言整理为五类游客信息、当前展品问题、重规划/结束意图。
2. `op_plan_route()`：解析兴趣、评分展品、聚合展厅、计算通路、按时间裁剪并提交待看队列。
3. `op_generate_guide()`：读取候选资料，选择材料、写主讲或回答、独立核对，通过后保存讲解记录。
4. `op_generate_summary()`：收集参观记录、讲解问答和反馈，生成并核对探索手记。

模型服务统一由 `build_model_services()` 装配，默认配置为 Paratera 的 `GLM-5.3`；向量服务使用 `GLM-Embedding-3`。应用层可注入自定义 `RouterServices`、数据源、模型调用和向量服务，测试无需真实网络请求。

## 测试与检查

在测试目录所在的项目目录运行：

```powershell
D:\tools\anaconda\python.exe -B -m unittest discover -s tests -q
```

前端语法检查：

```powershell
node --check frontend\app.js
```

交接记录中的最近一次验证为 202 项 unittest 通过，并覆盖模型 API、输入/路线/讲解/手记算子、Router 会话状态、演示 HTTP 全流程、刷新恢复、静态资源边界和非法请求回归。真实模型联调与浏览器语音播放也已做过一次验证；真实馆藏和生产数据仍未接入。

## 当前限制与下一步

生产化前需要补齐：

- 真实展品资料、权威来源、标签、重要度和展品向量；
- 真实展厅、楼层、入口、出口、通路和步行时间；
- 按展品 ID 读取讲解候选资料，并接入讲解/手记数据源；
- 持久化会话、路线、播放位置和手记；
- 用户身份、访问限制、多进程协调和正式运行配置；
- 真实 TTS/音频、暂停、追问与续播链路。

边生成边播放、未播内容自动调整、重复讲解检测、手记图片或 PDF 导出属于后续可选能力。

## 开发交接约定

开始新一轮开发前先阅读 `docs/开发交接.md` 和 `docs/需求文档.txt`，再核对相关源码与测试。修改字段或接口时同步更新交接文档；不要把 API Key、游客原话或模型响应体写入日志。生产场馆必须替换演示快照、候选史料、基础资料、向量编码及参数校准。

