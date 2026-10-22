"""按会话管理四个算子、讲解预生成与原子状态提交；不绑定 HTTP 或模型 SDK。

客户端只提交事件、文字、位置和实际播放进度，不能覆盖服务端状态或稿件。
完整路线、播放版本、续播栈和真实时间账本由本模块独立于 SessionState 保存。
"""

import asyncio
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from math import fsum, isfinite
from time import monotonic
from typing import Any

from guide_models import GuideComposer, GuideContext, GuideRequest, GuideReviewer, GuideSelector
from guide_generator import build_guide_request, commit_prepared_guide, prepare_guide
from operators import (
    InputInterpreter, op_generate_guide, op_generate_summary, op_plan_route,
    op_process_input,
)
from route_models import InterestParser, InterestProfile, MuseumDataSource, MuseumSnapshot, PlannerConfig, VectorService
from session_state import SessionState, VisitorInfo
from summary_models import SummaryComposer, SummaryDataSource, SummaryFeedback, SummaryReviewer


@dataclass(frozen=True, slots=True)
class RouterServices:
    """应用启动时注入服务，不允许客户端通过事件提交适配器或规划快照。"""

    interpreter: InputInterpreter | None = None
    snapshot: MuseumSnapshot | None = None
    data_source: MuseumDataSource | None = None
    interest_parser: InterestParser | None = None
    vector_service: VectorService | None = None
    selector: GuideSelector | None = None
    composer: GuideComposer | None = None
    reviewer: GuideReviewer | None = None
    planner_config: PlannerConfig | None = None
    summary_data_source: SummaryDataSource | None = None
    summary_composer: SummaryComposer | None = None
    summary_reviewer: SummaryReviewer | None = None
    guide_review_mode: str = "independent"


@dataclass(slots=True)
class _GuideJob:
    key: tuple
    request: GuideRequest
    background: bool
    task: asyncio.Task | None = None
    started: bool = False
    invalidated: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class _PlanningInputs:
    visitor_key: tuple
    snapshot: MuseumSnapshot
    interests: InterestProfile
    query_vector: tuple[float, ...]


@dataclass(slots=True)
class _Session:
    state: SessionState
    checkpoint: float
    current_node_id: str | None
    exit_node_id: str | None
    pace: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    route: dict | None = None
    plan_result: dict | None = None
    route_dirty: bool = True
    current: GuideContext | None = None
    resume_stack: list[GuideContext] = field(default_factory=list)
    paused: bool = False
    summary: dict | None = None
    summary_attempted: bool = False
    guide_epoch: int = 0
    prefetch: _GuideJob | None = None
    background_job: _GuideJob | None = None
    foreground_job: _GuideJob | None = None
    planning_inputs: _PlanningInputs | None = None


class _DispatchError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _payload(value: Mapping | None, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError("payload 必须是字段字典")
    if set(value) - allowed:
        raise ValueError("事件包含不支持的字段")
    if required - set(value):
        raise ValueError(f"缺少事件字段：{', '.join(sorted(required - set(value)))}")
    return dict(value)


class Router:
    """单进程、单事件循环的内存路由器；每个应用实例创建一个 Router。

    状态修改持会话锁；主讲准备与后台预生成在锁外等待，不同会话可并发。持久化、多进程
    锁、身份鉴权及真正的语音暂停由部署/端侧接入，不能用客户端快照恢复。
    """

    ROUTE_FIELDS = frozenset({"content_interests", "remaining_minutes", "other_needs"})

    def __init__(self, services: RouterServices | None = None, *, clock: Callable[[], float] = monotonic,
                 prefetch_enabled: bool = True, prefetch_concurrency: int = 2):
        if services is not None and not isinstance(services, RouterServices):
            raise ValueError("services 必须是 RouterServices")
        self.services = services or RouterServices()
        if self.services.snapshot is not None and self.services.data_source is not None:
            raise ValueError("snapshot 和 data_source 只能提供一个")
        if not callable(clock):
            raise ValueError("clock 必须是单调秒时钟")
        if type(prefetch_enabled) is not bool:
            raise ValueError("prefetch_enabled 必须是布尔值")
        if type(prefetch_concurrency) is not int or prefetch_concurrency < 1:
            raise ValueError("prefetch_concurrency 必须是正整数")
        self._clock = clock
        self._sessions: dict[str, _Session] = {}
        self._prefetch_enabled = prefetch_enabled
        self._prefetch_slots = asyncio.Semaphore(prefetch_concurrency)
        self._guide_tasks: set[asyncio.Task] = set()
        self._closing = False

    def _now(self) -> float:
        now = self._clock()
        if type(now) not in (int, float) or not isfinite(now):
            raise ValueError("clock 必须返回有限秒数")
        return float(now)

    def create_session(
        self, visitor_info: VisitorInfo | Mapping[str, Any] | None = None, *,
        session_id: str | None = None, current_node_id: str | None = None,
        exit_node_id: str | None = None, pace: str = "standard",
    ) -> dict:
        """新建游览并启动真实时间账本；首次规划由 submit_preferences/replan 触发。"""
        if pace not in ("quick", "standard", "deep"):
            raise ValueError("pace 必须是 quick、standard 或 deep")
        current_node_id = self._node(current_node_id) if current_node_id is not None else None
        exit_node_id = self._node(exit_node_id) if exit_node_id is not None else None
        state = SessionState(visitor_info, session_id=session_id)
        if state.session_id in self._sessions:
            raise ValueError("session_id 已存在")
        session = _Session(state, self._now(), current_node_id, exit_node_id, pace)
        self._sessions[state.session_id] = session
        return self._response(session, {"status": "created", "message": "游览已创建"})

    def _session(self, session_id: str) -> _Session:
        return self._sessions[_text(session_id, "session_id")]

    def _node(self, node_id: str) -> str:
        node_id = _text(node_id, "node_id")
        if self.services.snapshot is not None and node_id not in self.services.snapshot.node_floors:
            raise ValueError("位置节点不存在于场馆快照中")
        return node_id

    @staticmethod
    def _remaining_estimate(session: _Session) -> float | None:
        """保守估计缓存路线的剩余耗时；通行不根据生成记录自动核销。"""
        if session.route is None or session.route.get("status") == "time_exhausted":
            return None
        total = session.route.get("time_bill", {}).get("total_min")
        if total is None:
            return None
        pending = {stop.relic_id for stop in session.state.pending_queue}
        consumed = fsum(
            row["dwell_time_min"] for row in session.route.get("exhibits", ())
            if row["relic_id"] not in pending
        )
        return max(0.0, total - consumed)

    def _sync_time(self, session: _Session) -> None:
        now = self._now()
        if now < session.checkpoint:
            raise ValueError("clock 不能倒退")
        state = session.state
        minutes = state.visitor_info.remaining_minutes
        if state.status == "active" and minutes is not None and minutes > 0:
            remaining = max(0.0, minutes - (now - session.checkpoint) / 60)
            if remaining != minutes:
                state.update_visitor_info(remaining_minutes=remaining)
                if remaining == 0:
                    self._time_exhausted(session)
        session.checkpoint = now

    def _time_exhausted(self, session: _Session) -> None:
        self._invalidate_guides(session)
        session.state.replace_pending_queue(())
        session.plan_result = {
            "status": "time_exhausted", "pending_queue_updated": True,
            "start_node_id": session.current_node_id, "exit_node_id": session.exit_node_id,
            "path_nodes": [], "nodes": [], "galleries": [], "exhibits": [],
            "time_bill": {"available_min": 0.0, "total_min": 0.0, "remaining_min": 0.0},
            "warnings": ["剩余时间已用尽；空路线不表示已到达出口"],
        }
        session.route = deepcopy(session.plan_result)
        session.route_dirty = False

    @staticmethod
    def _response(session: _Session, reply: dict) -> dict:
        content = {
            **reply,
            "current_node_id": session.current_node_id, "exit_node_id": session.exit_node_id,
            "route": session.route, "plan_result": session.plan_result,
            "route_dirty": session.route_dirty,
            "playback": {
                "current": asdict(session.current) if session.current else None,
                "paused": session.paused,
                "resume_stack": [asdict(context) for context in session.resume_stack],
            },
            "summary": session.summary,
            "guide_prefetch": Router._prefetch_status(session),
        }
        return {"reply_content": deepcopy(content), "latest_state": session.state.to_dict()}

    async def _run(self, session: _Session, operation) -> dict:
        async with session.lock:
            self._sync_time(session)
            reply = {"status": "ok", "message": "操作完成", "steps": []}
            try:
                await operation(session, reply)
            except Exception as error:
                if isinstance(error, _DispatchError):
                    code = error.code
                elif isinstance(error, NotImplementedError):
                    code = "service_unavailable"
                elif isinstance(error, ValueError):
                    code = "invalid_request"
                else:
                    code = "operator_failed"
                reply.update(status="error", message=str(error), error={"code": code, "message": str(error)})
            # 请求处理本身消耗的真实时间也计入；成功的前序步骤不因后序失败回滚。
            self._sync_time(session)
            self._schedule_prefetch(session)
            return self._response(session, reply)

    @staticmethod
    def _guide_key(session: _Session, target: str) -> tuple:
        info = session.state.visitor_info
        return (session.guide_epoch, target, session.state.visited_relic_ids,
                info.content_interests, info.guide_styles, info.guide_focus, info.other_needs)

    @staticmethod
    def _invalidate_guides(session: _Session) -> None:
        session.guide_epoch += 1
        for job in (session.prefetch, session.foreground_job):
            if job is not None:
                job.invalidated.set()
        session.prefetch = None
        session.foreground_job = None
        # 已发送的 HTTP 不强行取消；background_job 保留到真实调用结束。

    @staticmethod
    def _prefetch_status(session: _Session) -> dict:
        job = session.prefetch
        if job is None or job.invalidated.is_set():
            return {"status": "idle", "relic_id": None}
        status = "generating"
        if job.task.done():
            status = "failed" if job.task.cancelled() or job.task.exception() is not None else "ready"
        return {"status": status, "relic_id": job.request.relic_id}

    def _schedule_prefetch(self, session: _Session) -> None:
        state = session.state
        if (self._closing or not self._prefetch_enabled or state.status != "active"
                or state.visitor_info.remaining_minutes == 0 or session.route_dirty
                or not state.pending_queue or session.foreground_job is not None
                or not any(record.kind == "guide" for record in state.guide_records)):
            return
        target = state.pending_queue[0].relic_id
        key = self._guide_key(session, target)
        if session.prefetch is not None and session.prefetch.key == key:
            return  # 包括失败结果；后台不自动循环重试。
        if session.background_job is not None and not session.background_job.task.done():
            return  # 多次变动只在旧 HTTP 结束后安排最新队首。
        request = build_guide_request(state, relic_id=target)
        job = self._create_guide_job(session, request, background=True)
        session.prefetch = session.background_job = job

    def _track_task(self, task: asyncio.Task) -> None:
        self._guide_tasks.add(task)
        task.add_done_callback(self._guide_tasks.discard)

    def _create_guide_job(self, session: _Session, request: GuideRequest, *, background: bool) -> _GuideJob:
        job = _GuideJob(self._guide_key(session, request.relic_id), request, background)

        async def prepare():
            if background:
                async with self._prefetch_slots:
                    async with session.lock:
                        if job.invalidated.is_set() or job.key != self._guide_key(session, request.relic_id):
                            raise _DispatchError("stale_guide", "预生成目标已变化")
                        job.started = True
                    return await self._prepare_guide(request)
            job.started = True
            return await self._prepare_guide(request)

        job.task = asyncio.create_task(prepare())
        self._track_task(job.task)

        def completed(task):
            if task.cancelled():
                return
            task.exception()  # 取走后台异常，避免泄露异常正文或未处理任务告警。
            if not self._closing:
                settle = asyncio.create_task(self._settle_guide_job(session, job))
                self._track_task(settle)

        job.task.add_done_callback(completed)
        return job

    async def _prepare_guide(self, request: GuideRequest):
        return await prepare_guide(
            request, selector=self.services.selector, composer=self.services.composer,
            reviewer=self.services.reviewer, review_mode=self.services.guide_review_mode,
        )

    async def _settle_guide_job(self, session: _Session, job: _GuideJob) -> None:
        async with session.lock:
            if session.background_job is job:
                session.background_job = None
            self._schedule_prefetch(session)

    @staticmethod
    async def _wait_guide_job(job: _GuideJob):
        invalidated = asyncio.create_task(job.invalidated.wait())
        try:
            done, _ = await asyncio.wait((job.task, invalidated), return_when=asyncio.FIRST_COMPLETED)
            if invalidated in done:
                raise _DispatchError("stale_guide", "需求或路线已变化，请按最新路线开始讲解")
            return job.task.result()
        finally:
            invalidated.cancel()
            await asyncio.gather(invalidated, return_exceptions=True)

    async def aclose(self) -> None:
        """服务关闭时回收后台任务；运行中作废使用版本标记而不是取消 HTTP。"""
        self._closing = True
        for session in self._sessions.values():
            self._invalidate_guides(session)
        while self._guide_tasks:
            pending = tuple(self._guide_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            # gather 对已完成任务可能立即返回，不能只依赖下一轮回调移除任务。
            self._guide_tasks.difference_update(pending)

    @staticmethod
    def _guide_error(reply: dict, error: Exception) -> None:
        if isinstance(error, _DispatchError):
            code = error.code
        elif isinstance(error, NotImplementedError):
            code = "service_unavailable"
        elif isinstance(error, ValueError):
            code = "invalid_request"
        else:
            code = "operator_failed"
        reply.update(status="error", message=str(error), error={"code": code, "message": str(error)})

    async def _check_guide_start(self, session: _Session, target: str, reply: dict) -> bool:
        if session.state.status != "active":
            raise _DispatchError("session_finished", "游览已结束，不能继续操作")
        if session.state.visitor_info.remaining_minutes == 0:
            self._time_exhausted(session)
            reply.update(status="time_exhausted", message="剩余时间已用尽")
            return False
        if session.route_dirty:
            raise _DispatchError("route_needs_replan", "路线已失效，请先完成重规划")
        if not session.state.pending_queue or session.state.pending_queue[0].relic_id != target:
            raise ValueError("展品已不在队首，请读取最新路线后再操作")
        estimate = self._remaining_estimate(session)
        minutes = session.state.visitor_info.remaining_minutes
        if estimate is not None and minutes is not None and estimate > minutes + 1e-6:
            await self._plan(session, reply, preserve_target=target)
            if session.route_dirty or session.state.visitor_info.remaining_minutes == 0:
                return False
            if not session.state.pending_queue or session.state.pending_queue[0].relic_id != target:
                reply["message"] = "时间变化后路线已调整，请按最新队列开始讲解"
                return False
        return True

    async def _start_guide(self, session: _Session, data: Mapping | None) -> dict:
        reply = {"status": "ok", "message": "操作完成", "steps": []}
        job = None
        try:
            async with session.lock:
                self._sync_time(session)
                values = _payload(data, {"relic_id"}, {"relic_id"})
                target = _text(values["relic_id"], "relic_id")
                if not await self._check_guide_start(session, target, reply):
                    self._sync_time(session)
                    self._schedule_prefetch(session)
                    return self._response(session, reply)
                key = self._guide_key(session, target)
                candidate = session.foreground_job or session.prefetch
                if (candidate is not None and candidate.key == key and not candidate.invalidated.is_set()
                        and not (candidate.task.done() and (candidate.task.cancelled() or candidate.task.exception()))):
                    job = candidate
                    if job.background and not job.started:
                        # 尚未发请求的排队预取提升为前台，不受后台并发配额阻塞。
                        job.invalidated.set()
                        job.task.cancel()
                        if session.background_job is job:
                            session.background_job = None
                        if session.prefetch is job:
                            session.prefetch = None
                        job = None
                if job is None:
                    job = self._create_guide_job(
                        session, build_guide_request(session.state, relic_id=target), background=False,
                    )
                session.foreground_job = job
            # 不持会话锁等待，播放进度、追问和重规划仍可处理。
            prepared = await self._wait_guide_job(job)
            async with session.lock:
                self._sync_time(session)
                if not await self._check_guide_start(session, target, reply):
                    if session.foreground_job is job:
                        session.foreground_job = None
                    self._schedule_prefetch(session)
                    return self._response(session, reply)
                if job.invalidated.is_set() or job.key != self._guide_key(session, target):
                    raise _DispatchError("stale_guide", "需求或路线已变化，请按最新路线开始讲解")
                result = commit_prepared_guide(session.state, prepared)
                if session.foreground_job is job:
                    session.foreground_job = None
                if session.prefetch is job:
                    session.prefetch = None
                session.current = result.guide_context
                session.resume_stack.clear()
                session.paused = False
                reply.update(status="guide_ready", message="讲解已生成", guide=result.to_dict())
                reply["steps"].append("guide_generated")
                self._sync_time(session)
                self._schedule_prefetch(session)
                return self._response(session, reply)
        except asyncio.CancelledError:
            # HTTP 客户端离开不取消共享准备任务；后续相同点击可继续复用。
            raise
        except Exception as error:
            async with session.lock:
                if session.foreground_job is job:
                    session.foreground_job = None
                self._guide_error(reply, error)
                self._sync_time(session)
                self._schedule_prefetch(session)
                return self._response(session, reply)

    async def dispatch_action(self, session_id: str, action: str, payload: Mapping | None = None) -> dict:
        """按钮直达；游客信息字典仍交给第一算子校验，不调用意图分析器。"""
        session = self._session(session_id)
        action = _text(action, "action")
        # 在等待锁之前复制请求，避免调用方在排队期间修改 payload。
        data = deepcopy(payload)
        if action == "start_guide":
            return await self._start_guide(session, data)

        async def operation(session, reply):
            if action == "get_state":
                _payload(data, set())
                reply["message"] = "已读取最新状态"
                return
            if action == "finish":
                values = _payload(data, {"feedback"})
                feedback = self._summary_feedback(values.get("feedback"))
                await self._finish(session, reply, feedback=feedback)
                return
            if action in ("generate_summary", "summary"):
                values = _payload(data, {"feedback"})
                if session.state.status != "finished":
                    raise _DispatchError("session_not_finished", "请先结束游览，再生成探索手记")
                await self._summary(session, self._summary_feedback(values.get("feedback")), reply)
                return
            if session.state.status != "active":
                raise _DispatchError("session_finished", "游览已结束，不能继续操作")
            if action in ("submit_preferences", "update_visitor_info"):
                values = _payload(data, {"visitor_info"}, {"visitor_info"})
                if not isinstance(values["visitor_info"], Mapping):
                    raise ValueError("visitor_info 必须是五类游客信息字段字典")
                await self._input(session, values["visitor_info"], reply, force_plan=action == "submit_preferences")
            elif action == "replan":
                _payload(data, set())
                await self._plan(session, reply)
            elif action == "skip":
                values = _payload(data, {"relic_id"}, {"relic_id"})
                stop = session.state.skip_next(expected_relic_id=_text(values["relic_id"], "relic_id"))
                self._invalidate_guides(session)
                reply.update(message="已跳过展品", skipped_relic_id=stop.relic_id)
                reply["steps"].append("skipped")
            elif action == "update_location":
                values = _payload(data, {"current_node_id", "exit_node_id"}, {"current_node_id"})
                current = self._node(values["current_node_id"])
                exit_node = self._node(values["exit_node_id"]) if "exit_node_id" in values else session.exit_node_id
                if current != session.current_node_id or exit_node != session.exit_node_id:
                    session.route_dirty = True
                    self._invalidate_guides(session)
                session.current_node_id, session.exit_node_id = current, exit_node
                reply["message"] = "当前位置已更新"
            elif action in ("playback_progress", "resume_guide"):
                self._progress(session, data)
                if action == "resume_guide":
                    if session.resume_stack:
                        if session.current.remaining_segments:
                            raise _DispatchError("answer_not_finished", "请先播放完当前回答再恢复原稿")
                        session.current = session.resume_stack.pop()
                    session.paused = False
                    reply["message"] = "已返回可续播的服务端原稿"
                else:
                    reply["message"] = "实际播放进度已确认"
            else:
                raise ValueError(f"未知事件：{action}")

        return await self._run(session, operation)

    async def dispatch_chat(self, session_id: str, text: str, *, playback: Mapping | None = None) -> dict:
        """端侧先暂停语音并附带最终进度；缺少回报时只用已确认的前缀。"""
        session = self._session(session_id)
        progress = deepcopy(playback)

        async def operation(session, reply):
            if session.state.status != "active":
                raise _DispatchError("session_finished", "游览已结束，不能继续聊天")
            user_text = _text(text, "text")
            if progress is not None:
                self._progress(session, progress)
            was_paused = session.paused
            if session.current:
                session.paused = True
            await self._input(session, user_text, reply)
            if "guide" not in reply and session.state.status == "active":
                session.paused = was_paused

        return await self._run(session, operation)

    def _progress(self, session: _Session, payload: Mapping | None) -> None:
        values = _payload(payload, {"guide_id", "completed_segments"}, {"guide_id", "completed_segments"})
        guide_id = _text(values["guide_id"], "guide_id")
        context = session.current
        if context is None or guide_id != context.guide_id:
            raise _DispatchError("stale_guide", "稿件不是当前播放版本，请读取最新播放状态")
        count = values["completed_segments"]
        if type(count) is not int or not context.completed_segments <= count <= len(context.segments):
            raise ValueError("播放进度必须是合法且不倒退的完整单元数")
        record = session.state.get_guide_record(guide_id)
        if record.relic_id != context.relic_id or record.segments != context.segments:
            raise RuntimeError("当前播放稿件与服务端记录不一致")
        session.current = replace(context, completed_segments=count, segments=record.segments)

    async def _guide(self, session: _Session, **kwargs):
        return await op_generate_guide(
            session.state, selector=self.services.selector, composer=self.services.composer,
            reviewer=self.services.reviewer, **kwargs,
            review_mode=self.services.guide_review_mode,
        )

    @staticmethod
    def _summary_feedback(value: Any) -> SummaryFeedback:
        if value is None:
            return SummaryFeedback()
        if isinstance(value, SummaryFeedback):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("feedback 必须是字段字典")
        allowed = {"favorite_relic_id", "reflection"}
        if set(value) - allowed:
            raise ValueError("feedback 包含不支持的字段")
        return SummaryFeedback(**dict(value))

    async def _summary(self, session: _Session, feedback: SummaryFeedback, reply: dict) -> None:
        result = await op_generate_summary(
            session.state,
            feedback=feedback,
            data_source=self.services.summary_data_source,
            composer=self.services.summary_composer,
            reviewer=self.services.summary_reviewer,
        )
        session.summary = deepcopy(result.to_dict())
        session.summary_attempted = True
        reply["summary"] = deepcopy(session.summary)
        reply["steps"].append("summary_generated")
        reply.update(status="summary_ready", message="探索手记已生成")

    async def _input(self, session: _Session, user_input, reply: dict, *, force_plan: bool = False):
        result = await op_process_input(
            session.state, user_input, interpreter=self.services.interpreter, guide_context=session.current,
        )
        reply["input_result"] = asdict(result)
        if result.changed_fields or "remaining_minutes" in result.updated_fields:
            self._invalidate_guides(session)
        if result.changed_fields:
            reply["steps"].append("visitor_info_updated")
        if "remaining_minutes" in result.updated_fields:
            # 显式预算从确认更新时重新计时，不把前一次预算的已过时间重复扣减。
            session.checkpoint = self._now()
        needs_plan = force_plan or result.replan_requested or bool(self.ROUTE_FIELDS.intersection(result.changed_fields))
        if needs_plan:
            session.route_dirty = True
        if result.finish_requested:
            # 明确结束优先，不在结束前继续安排生成和规划。
            await self._finish(session, reply)
            return
        if result.question is not None:
            if result.needs_current_relic:
                reply.update(status="needs_input", message="请先开始当前展品讲解，再继续这个问题",
                             question=result.question, needs_current_relic=True)
            else:
                answer = await self._guide(
                    session, question=result.question, relic_id=result.current_relic_id,
                    guide_context=result.guide_context,
                )
                if session.current and session.current.remaining_segments:
                    session.resume_stack.append(session.current)
                session.current = answer.guide_context
                session.paused = False
                reply.update(status="answer_ready", message="追问回答已生成", guide=answer.to_dict())
                reply["steps"].append("answered")
        if needs_plan:
            await self._plan(session, reply)
        elif result.question is None:
            reply["message"] = "游客信息已更新" if result.changed_fields else "未识别到需要执行的游览操作"

    async def _plan(self, session: _Session, reply: dict, *, preserve_target: str | None = None):
        # 自然计时只改变预算；同一队首的准备稿不依赖路线和具体剩余分钟数。
        preserve_key = self._guide_key(session, preserve_target) if preserve_target is not None else None
        if preserve_target is None:
            self._invalidate_guides(session)
        session.route_dirty = True
        # 首先同步异步输入/讲解期间消耗的时间，再交给规划算子。
        self._sync_time(session)
        if session.state.visitor_info.remaining_minutes == 0:
            self._time_exhausted(session)
            reply.update(status="time_exhausted", message="剩余时间已用尽，未结束游览")
            reply["steps"].append("route_planned")
            return
        missing = []
        if session.state.visitor_info.remaining_minutes is None:
            missing.append("remaining_minutes")
        if not session.state.visitor_info.content_interests:
            missing.append("content_interests")
        if session.current_node_id is None:
            missing.append("current_node_id")
        if session.exit_node_id is None:
            missing.append("exit_node_id")
        if missing:
            if preserve_target is not None:
                self._invalidate_guides(session)
            session.plan_result = {"status": "needs_input", "pending_queue_updated": False, "missing_fields": missing}
            reply.update(status="needs_input", message="请补充规划所需信息", missing_fields=missing)
            return
        info = session.state.visitor_info
        visitor_key = (info.content_interests, info.guide_styles, info.guide_focus, info.other_needs)
        cached = session.planning_inputs
        reuse_inputs = preserve_target is not None and cached is not None and cached.visitor_key == visitor_key
        snapshot = cached.snapshot if reuse_inputs else self.services.snapshot
        interests = cached.interests if reuse_inputs else None
        query_vector = cached.query_vector if reuse_inputs else None

        # 记录常规规划实际使用的不可变输入，预算重算可完全在本地进行。
        async def parse_interest(*, visitor_info):
            nonlocal interests
            interests = await self.services.interest_parser(visitor_info=visitor_info)
            return interests

        class CaptureDependencies:
            async def load(capture):
                nonlocal snapshot
                snapshot = await self.services.data_source.load()
                return snapshot

            async def embed_query(capture, text):
                nonlocal query_vector
                query_vector = await self.services.vector_service.embed_query(text)
                return query_vector

        capture = CaptureDependencies()
        try:
            plan = await op_plan_route(
                session.state, start_node_id=session.current_node_id, exit_node_id=session.exit_node_id,
                pace=session.pace, snapshot=snapshot,
                data_source=capture if snapshot is None and self.services.data_source is not None else None,
                interests=interests, query_vector=query_vector,
                interest_parser=parse_interest if self.services.interest_parser is not None else None,
                vector_service=capture if self.services.vector_service is not None else None,
                config=self.services.planner_config,
            )
        except Exception as error:
            if preserve_target is not None:
                self._invalidate_guides(session)
            session.plan_result = {"status": "error", "pending_queue_updated": False, "message": str(error)}
            raise
        session.plan_result = deepcopy(plan)
        if not plan["pending_queue_updated"]:
            if preserve_target is not None:
                self._invalidate_guides(session)
            reply.update(status=plan["status"], message="当前没有可行路线；保留的旧队列尚未更新")
            return
        session.planning_inputs = _PlanningInputs(visitor_key, snapshot, interests, tuple(query_vector))
        session.route = deepcopy(plan)
        session.route_dirty = False
        if preserve_target is not None and (
            not session.state.pending_queue or session.state.pending_queue[0].relic_id != preserve_target
            or self._guide_key(session, preserve_target) != preserve_key
        ):
            self._invalidate_guides(session)
        reply["steps"].append("route_planned")
        if reply["status"] not in ("answer_ready", "needs_input"):
            reply.update(status=plan["status"], message="路线已更新")

    async def _finish(
        self, session: _Session, reply: dict, *, feedback: SummaryFeedback | None = None,
    ) -> None:
        self._sync_time(session)
        self._invalidate_guides(session)
        session.state.finish()
        session.paused = True
        reply.update(status="finished", message="游览已结束")
        reply["steps"].append("finished")
        has_summary_service = any((
            self.services.summary_data_source is not None,
            self.services.summary_composer is not None,
            self.services.summary_reviewer is not None,
        ))
        if has_summary_service and not session.summary_attempted:
            await self._summary(session, feedback or SummaryFeedback(), reply)


async def dispatch_action(router: Router, session_id: str, action: str, payload: Mapping | None = None) -> dict:
    """显式传入应用持有的 Router，避免隐藏的全局会话与测试相互污染。"""
    return await router.dispatch_action(session_id, action, payload)


async def dispatch_chat(router: Router, session_id: str, text: str, *, playback: Mapping | None = None) -> dict:
    return await router.dispatch_chat(session_id, text, playback=playback)
