"""博物馆导览应用的本地 HTTP 入口。

HTTP 层只负责把 JSON 请求转交给 :class:`router.Router`，不在浏览器侧
保存或合并会话状态。生产部署可以替换本文件中的 ``serve``，而前端使用的
接口契约保持不变：

* ``POST /api/session`` 创建会话；
* ``POST /api/event`` 调用按钮事件；
* ``POST /api/chat`` 发送自然语言输入；
* ``GET /api/state?session_id=...`` 读取最新状态；
* ``GET /health`` 做健康检查。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from demo_museum import ENTRANCE, EXIT, build_demo_services, museum_metadata
from router import Router


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_BODY_BYTES = 1 * 1024 * 1024
STATIC_SUFFIXES = frozenset({".html", ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".mp3", ".wav", ".ogg", ".mp4", ".webm"})


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code, "message": message}, "message": message}


class MuseumHTTPServer(ThreadingHTTPServer):
    """HTTP 请求并发处理，Router 始终运行于同一个独立事件循环。"""

    # Windows 的 SO_REUSEADDR 会让多个进程同时绑定同一端口。
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, address, handler, *, router: Router, static_dir: str | Path, museum: dict | None = None):
        super().__init__(address, handler)
        self.router = router
        self.static_dir = Path(static_dir).resolve()
        self.museum = museum or {"mode": "custom", "name": "博物馆", "nodes": [], "relics": [], "recommended_routes": []}
        self.loop = asyncio.new_event_loop()
        self.loop_thread = Thread(target=self.loop.run_forever, name="xunji-router", daemon=True)
        self.loop_thread.start()

    def server_close(self) -> None:
        try:
            if not self.loop.is_closed():
                async def cancel_pending():
                    pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                asyncio.run_coroutine_threadsafe(cancel_pending(), self.loop).result(timeout=5)
                self.loop.call_soon_threadsafe(self.loop.stop)
                self.loop_thread.join()
                self.loop.close()
        finally:
            super().server_close()


class MuseumRequestHandler(BaseHTTPRequestHandler):
    """HTTP 到 Router 的薄适配层。"""

    server: MuseumHTTPServer
    # 本地开发入口不保留空闲连接。
    protocol_version = "HTTP/1.0"

    def _send_bytes(self, body: bytes, *, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, *, status: int = HTTPStatus.OK) -> None:
        reply = payload.get("reply_content", {}) if isinstance(payload, dict) else {}
        if reply.get("status") == "error":
            # Router 的固定错误信息用于本地联调，不记录密钥、游客原话或模型响应体。
            self.log_error("Router %s", reply.get("error", {}).get("code", "unknown"))
        self._send_bytes(_json_bytes(payload), status=int(status), content_type="application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError as exc:
            raise ValueError("Content-Length 必须是整数") from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体必须是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _run_async(self, awaitable):
        return asyncio.run_coroutine_threadsafe(awaitable, self.server.loop).result()

    def _router_error(self, error: Exception) -> None:
        if isinstance(error, KeyError):
            self._send_json(_error("unknown_session", "会话不存在"), status=HTTPStatus.NOT_FOUND)
        elif isinstance(error, (ValueError, TypeError)):
            self._send_json(_error("invalid_request", str(error)), status=HTTPStatus.BAD_REQUEST)
        else:
            self._send_json(_error("server_error", "服务器暂时无法处理请求"), status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_OPTIONS(self) -> None:
        self._send_bytes(b"", status=HTTPStatus.NO_CONTENT, content_type="text/plain; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._send_json({"status": "ok", "mode": self.server.museum["mode"]})
            return
        if parsed.path == "/api/museum":
            self._send_json(self.server.museum)
            return
        if parsed.path == "/api/state":
            query = parse_qs(parsed.query, keep_blank_values=True)
            session_id = query.get("session_id", [""])[0]
            if not session_id:
                self._send_json(_error("invalid_request", "缺少 session_id"), status=HTTPStatus.BAD_REQUEST)
                return
            try:
                result = self._run_async(self.server.router.dispatch_action(session_id, "get_state"))
            except Exception as error:
                self._router_error(error)
                return
            self._send_json(result)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        try:
            body = self._read_json()
        except ValueError as error:
            self._send_json(_error("invalid_json", str(error)), status=HTTPStatus.BAD_REQUEST)
            return

        parsed_path = urlsplit(self.path).path
        try:
            if parsed_path == "/api/session":
                self._create_session(body)
                return
            if parsed_path == "/api/event":
                self._dispatch_event(body)
                return
            if parsed_path == "/api/chat":
                self._dispatch_chat(body)
                return
        except Exception as error:
            self._router_error(error)
            return
        self._send_json(_error("not_found", "接口不存在"), status=HTTPStatus.NOT_FOUND)

    def _create_session(self, body: dict[str, Any]) -> None:
        allowed = {"visitor_info", "session_id", "current_node_id", "exit_node_id", "pace"}
        unknown = set(body) - allowed
        if unknown:
            raise ValueError("创建会话包含不支持的字段")
        kwargs = {key: body[key] for key in allowed if key in body}
        if self.server.museum["mode"] == "demo":
            kwargs.setdefault("current_node_id", ENTRANCE)
            kwargs.setdefault("exit_node_id", EXIT)
        async def create():
            return self.server.router.create_session(**kwargs)
        result = self._run_async(create())
        self._send_json(result, status=HTTPStatus.CREATED)

    def _dispatch_event(self, body: dict[str, Any]) -> None:
        session_id = body.get("session_id")
        action = body.get("action")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id 必须是非空字符串")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("action 必须是非空字符串")
        unknown = set(body) - {"session_id", "action", "payload"}
        if unknown:
            raise ValueError("事件请求包含不支持的字段")
        payload = body.get("payload", {})
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("payload 必须是字段字典")
        result = self._run_async(self.server.router.dispatch_action(session_id, action, payload))
        self._send_json(result)

    def _dispatch_chat(self, body: dict[str, Any]) -> None:
        session_id = body.get("session_id")
        text = body.get("text")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id 必须是非空字符串")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text 必须是非空字符串")
        unknown = set(body) - {"session_id", "text", "playback"}
        if unknown:
            raise ValueError("聊天请求包含不支持的字段")
        playback = body.get("playback")
        if playback is not None and not isinstance(playback, dict):
            raise ValueError("playback 必须是字段字典")
        result = self._run_async(self.server.router.dispatch_chat(session_id, text, playback=playback))
        self._send_json(result)

    def _serve_static(self, requested_path: str) -> None:
        relative = unquote(requested_path).lstrip("/") or "index.html"
        candidate = (self.server.static_dir / relative).resolve()
        try:
            candidate.relative_to(self.server.static_dir)
        except ValueError:
            self._send_json(_error("not_found", "资源不存在"), status=HTTPStatus.NOT_FOUND)
            return
        # 仅发布浏览器所需的静态资源；模型配置、数据快照、脚本和源码
        # 位于同一项目目录中，不应能通过资源路径读出。
        parts = candidate.relative_to(self.server.static_dir).parts
        published = relative in {"index.html", "style.css", "app.js"} or (parts and parts[0] == "assets")
        if (not published or not candidate.is_file() or candidate.suffix.lower() not in STATIC_SUFFIXES
                or any(part.startswith(".") for part in candidate.relative_to(self.server.static_dir).parts)):
            self._send_json(_error("not_found", "资源不存在"), status=HTTPStatus.NOT_FOUND)
            return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._send_json(_error("server_error", "资源暂时无法读取"), status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or candidate.suffix in {".js", ".json"}:
            content_type += "; charset=utf-8"
        self._send_bytes(body, status=HTTPStatus.OK, content_type=content_type)


def create_server(
    *, router: Router | None = None, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
    static_dir: str | Path | None = None, demo: bool = True,
) -> MuseumHTTPServer:
    """创建可供测试或 ``serve`` 使用的 HTTPServer。"""
    if router is not None and not isinstance(router, Router):
        raise TypeError("router 必须是 Router")
    root = Path(static_dir) if static_dir is not None else Path(__file__).resolve().parents[1] / "frontend"
    museum = None
    if router is None:
        if not demo:
            raise ValueError("真实场馆模式需要显式注入 Router 及经过核实的场馆资料")
        router = Router(build_demo_services())
        museum = museum_metadata()
    return MuseumHTTPServer((host, port), MuseumRequestHandler, router=router, static_dir=root, museum=museum)


def serve(
    *, router: Router | None = None, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
    static_dir: str | Path | None = None,
) -> None:
    """启动开发服务器，按 Ctrl+C 停止。"""
    server = create_server(router=router, host=host, port=port, static_dir=static_dir)
    print(f"寻迹演示导览 http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()
