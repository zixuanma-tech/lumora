# 寻迹（Lumora）博物馆 AI 导览

寻迹是一个面向博物馆参观场景的导览 Agent 原型，按“了解需求 → 安排路线 → 逐件讲解 → 随时调整 → 整理手记”工作。游客可以表达兴趣和可用时间，系统生成路线；到达展品后再生成讲解，支持追问、跳过、重规划和结束后的探索手记。

当前分支用于开发演示。代码支持依赖注入，真实馆藏、空间通路、史料候选、向量库和持久化仍需接入；本地页面和后端接口可先用于验证状态管理与交互流程。

## 目录

```text
.
├─ backend/       Python 后端、Router、四个算子、路线/讲解/手记模块
├─ frontend/      index.html、app.js、style.css
├─ docs/          架构与开发进度说明
└─ README.md
```

## 本地启动

项目使用 Python 标准库。进入仓库根目录后运行：

```powershell
D:\tools\anaconda\python.exe -B backend\main.py
```

然后访问 <http://127.0.0.1:8000/>。服务默认只监听本机回环地址。

### 在校园网内分享

运行以下命令可启动局域网模式，程序会显示当前网址：

```powershell
D:\tools\anaconda\python.exe -B backend\main.py --lan
```

换网络或 IP 变化后，可在另一个 PowerShell 窗口重新查询：

```powershell
.\scripts\show_lan_url.ps1
```

访问者须连接到允许设备互访的同一网络。校园网客户端隔离或 Windows 防火墙可能阻止访问。该模式没有身份验证，只用于可信网络中的临时演示。

## HTTP 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/api/museum` | 获取演示场馆信息与推荐路线 |
| `POST` | `/api/session` | 创建游览会话 |
| `GET` | `/api/state?session_id=...` | 读取会话状态 |
| `POST` | `/api/chat` | 发送自然语言需求、追问或结束意图 |
| `POST` | `/api/event` | 处理开始讲解、跳过、重规划、播放进度、结束等事件 |

业务请求统一返回 `reply_content` 和 `latest_state`。Router 按 `session_id` 管理独立会话，同一会话串行处理，不同会话可以并发。

## 核心模块

1. `op_process_input()`：整理游客信息、当前问题和重规划/结束意图。
2. `op_plan_route()`：解析兴趣、评分展品、计算通路并生成待看队列。
3. `op_generate_guide()`：选择资料、生成主讲或追问回答并核对后保存。
4. `op_generate_summary()`：根据游览记录和反馈生成探索手记。

## 模型配置

模型客户端位于 `backend/model_api.py`，服务装配位于 `backend/model_services.py`。API Key 应通过环境变量 `LUMORA_LLM_API_KEY` 提供，避免写入仓库；本地配置文件须保持未跟踪。

## 当前限制

- 真实展品资料、展厅通路、权威史料和向量数据尚未接入。
- 会话状态仍在进程内存中，重启后不会保留。
- 语音播放使用浏览器能力，生产 TTS 和音频持久化尚未接入。
- 该分支面向本地开发演示，不是生产部署配置。

开始后续开发前，请先阅读 [`docs/Agent架构与开发进度.md`](docs/Agent架构与开发进度.md)，并同步更新接口或字段说明。
