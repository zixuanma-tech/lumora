"""这文件定义了一次游览中的状态。
VisitorInfo 保存提炼后的五类游客信息：内容兴趣、讲解风格、讲解角度、时间、其他需求。
    content_interests：想看或想了解什么，也可表达学习目标或选择的主题。
    guide_styles：如何讲，例如生动、通俗、简洁、深入。
    guide_focus：侧重什么角度，例如材料工艺、历史背景、与课本的联系。
    remaining_minutes：剩余时间，初始时填写本次可用时间；None 未知，0 已用尽。
    other_needs：年龄、亲子同行、无障碍要求、需要休息等其他信息或需求。

    列表元素可使用完整描述，不限于短标签。五类信息不一定都要有。（可能用户输入时没有体现）
示例：
    state = SessionState()
    state.update_visitor_info(
        content_interests=["制作工艺"],
        guide_focus=["材料与制作过程"],
    )
    state.replace_pending_queue([RouteStop("relic-001", "符合制作工艺兴趣")])
    state.complete_visit("relic-001", "生成成功后的完整讲解词")
    latest_state = state.to_dict()
每次游览创建独立的 SessionState，由 Router 按 session_id 管理。
guide_records 保存生成成功并通过核对的主讲及追问回答，独立于参观轨迹。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from math import isfinite
from typing import Any, TypeVar
from uuid import uuid4

VISITOR_INFO_FIELDS = frozenset({
    "content_interests", "guide_styles", "guide_focus", "remaining_minutes", "other_needs",
})

Record = TypeVar("Record")

def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError(f"{name} 不能为空")
    return value

def _items(value: Any, name: str) -> tuple:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ValueError(f"{name} 必须是列表或其他序列")
    return tuple(value)

def _text_items(value: Any, name: str) -> tuple[str, ...]:
    # 信息标签去重但保留顺序，暂时没有限制词表
    # 不过如果要保证讲解的正确性，可能还是需要有一些词表限制？
    return tuple(dict.fromkeys(_text(item, name) for item in _items(value, name)))


def _records(value: Any, record_type: type[Record], name: str) -> tuple[Record, ...]:
    result = []
    for item in _items(value, name):
        if isinstance(item, Mapping):
            item = record_type(**item)
        if not isinstance(item, record_type):
            raise ValueError(f"{name} 中的元素必须是 {record_type.__name__}")
        result.append(item)
    return tuple(result)


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class VisitorInfo:
    content_interests: tuple[str, ...] = ()
    guide_styles: tuple[str, ...] = ()
    guide_focus: tuple[str, ...] = ()
    remaining_minutes: float | None = None
    other_needs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("content_interests", "guide_styles", "guide_focus", "other_needs"):
            object.__setattr__(self, name, _text_items(getattr(self, name), name))
        minutes = self.remaining_minutes
        if minutes is not None:
            if type(minutes) not in (int, float) or minutes < 0:
                raise ValueError("remaining_minutes 必须是非负有限数值或 None")
            try:
                finite = isfinite(minutes)
            except OverflowError:
                finite = False
            if not finite:
                raise ValueError("remaining_minutes 必须是非负有限数值或 None")


@dataclass(frozen=True, slots=True)
class RouteStop:
    """一项待参观计划。展品史料和空间信息仍由 relics.json 提供。"""

    relic_id: str
    recommendation_reason: str = ""
    connection_from_previous: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "relic_id", _text(self.relic_id, "relic_id"))
        for name in ("recommendation_reason", "connection_from_previous"):
            object.__setattr__(self, name, _text(getattr(self, name), name, allow_empty=True))


@dataclass(frozen=True, slots=True)
class VisitRecord:
    """已完成讲解的展品快照，供当前展品追问和探索手记使用。"""

    relic_id: str
    guide_text: str
    recommendation_reason: str = ""
    connection_from_previous: str = ""

    def __post_init__(self) -> None:
        stop = RouteStop(self.relic_id, self.recommendation_reason, self.connection_from_previous)
        for field in fields(stop):
            object.__setattr__(self, field.name, getattr(stop, field.name))
        object.__setattr__(self, "guide_text", _text(self.guide_text, "guide_text"))


@dataclass(frozen=True, slots=True)
class GuideRecord:
    """一次成功生成的完整稿件；不表示游客已听完。

    kind 为 guide（首次主讲）或 answer（追问回答）。保留原始分句和稿件 ID，
    Router 可通过 ID 取回原稿并结合独立维护的播放进度重建 GuideContext。
    """

    guide_id: str
    relic_id: str
    kind: str
    segments: tuple[str, ...]
    question: str | None = None

    def __post_init__(self) -> None:
        for name in ("guide_id", "relic_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if self.kind not in ("guide", "answer"):
            raise ValueError("kind 必须是 guide 或 answer")
        segments = _items(self.segments, "segments")
        if not segments or any(not isinstance(item, str) or not item.strip() for item in segments):
            raise ValueError("segments 必须包含非空稿件播放单元")
        object.__setattr__(self, "segments", segments)
        if self.kind == "answer":
            object.__setattr__(self, "question", _text(self.question, "question"))
        elif self.question is not None:
            raise ValueError("主讲记录不能包含追问问题")

    @property
    def guide_text(self) -> str:
        return "".join(self.segments)


class SessionState:
    """单次游览的共享状态。

    所有公开快照均不可直接修改。更新通过方法完成，先校验再提交。
    guide_records 只追加；主讲记录与参观提交一起更新，追问不推进参观。
    visited_relics 只能随首次主讲追加；重规划只能替换 pending_queue。
    路线展品数量、时长分配和知识边审核属于算子规则，不在这里硬编码。
    """

    __slots__ = (
        "_session_id", "_visitor_info", "_visited_relics", "_pending_queue",
        "_skipped_relics", "_status", "_guide_records",
    )

    def __init__(
        self,
        visitor_info: VisitorInfo | Mapping[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> None:
        if isinstance(visitor_info, Mapping):
            visitor_info = VisitorInfo(**visitor_info)
        if visitor_info is not None and not isinstance(visitor_info, VisitorInfo):
            raise ValueError("visitor_info 必须是 VisitorInfo 或字典")
        self._session_id = str(uuid4()) if session_id is None else _text(session_id, "session_id")
        self._visitor_info = visitor_info if visitor_info is not None else VisitorInfo()
        self._visited_relics: tuple[VisitRecord, ...] = ()
        self._pending_queue: tuple[RouteStop, ...] = ()
        self._skipped_relics: tuple[str, ...] = ()
        self._guide_records: tuple[GuideRecord, ...] = ()
        self._status = "active"

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def visitor_info(self) -> VisitorInfo:
        return self._visitor_info

    @property
    def visited_relics(self) -> tuple[VisitRecord, ...]:
        return self._visited_relics

    @property
    def visited_relic_ids(self) -> tuple[str, ...]:
        return tuple(visit.relic_id for visit in self._visited_relics)

    @property
    def guide_records(self) -> tuple[GuideRecord, ...]:
        """按生成成功顺序保存的主讲与问答，包含未播放的完整稿件。"""
        return self._guide_records

    def get_guide_record(self, guide_id: str) -> GuideRecord:
        """按稿件版本读取服务端原稿；不存在时抛出 KeyError。"""
        guide_id = _text(guide_id, "guide_id")
        for record in self._guide_records:
            if record.guide_id == guide_id:
                return record
        raise KeyError(guide_id)

    @property
    def pending_queue(self) -> tuple[RouteStop, ...]:
        return self._pending_queue

    @property
    def skipped_relics(self) -> tuple[str, ...]:
        """跳过记录；是否在以后重新推荐，由规划算子决定。"""
        return self._skipped_relics

    @property
    def current_relic_id(self) -> str | None:
        """最近完成讲解的展品，用于 QA_CURRENT；尚未讲解时为 None。"""
        return self._visited_relics[-1].relic_id if self._visited_relics else None

    @property
    def status(self) -> str:
        return self._status

    def _ensure_active(self) -> None:
        if self._status != "active":
            raise RuntimeError("游览已结束，不能继续修改游客信息、路线或讲解记录")

    def update_visitor_info(self, **changes: Any) -> VisitorInfo:
        """局部更新提炼后的游客信息。

        未传入的字段保留原值；列表字段传 [] 清空，时间传 None 清空。
        传入的列表替换该字段的当前值，同一次更新可以涉及多个维度。
        """
        self._ensure_active()
        unknown = set(changes) - VISITOR_INFO_FIELDS
        if unknown:
            raise ValueError(f"未知的游客信息字段：{', '.join(sorted(unknown))}")
        updated = replace(self._visitor_info, **changes)
        self._visitor_info = updated
        return updated

    def replace_pending_queue(self, stops: Iterable[RouteStop | str | Mapping[str, Any]]) -> None:
        """提交首次规划或重规划结果，不允许重复展品或已参观展品。"""
        self._ensure_active()
        candidates = _items(stops, "pending_queue")
        candidates = tuple(RouteStop(item) if isinstance(item, str) else item for item in candidates)
        queue = _records(candidates, RouteStop, "pending_queue")
        self._validate_route(self._visited_relics, queue)
        self._pending_queue = queue

    @staticmethod
    def _validate_route(visits: tuple[VisitRecord, ...], queue: tuple[RouteStop, ...]) -> None:
        visited_ids = [visit.relic_id for visit in visits]
        pending_ids = [stop.relic_id for stop in queue]
        if len(set(visited_ids)) != len(visited_ids):
            raise ValueError("visited_relics 不能包含重复展品")
        if len(set(pending_ids)) != len(pending_ids):
            raise ValueError("pending_queue 不能包含重复展品")
        if set(visited_ids) & set(pending_ids):
            raise ValueError("pending_queue 不能包含已参观展品")

    def _next_stop(self, expected_relic_id: str | None = None) -> RouteStop:
        self._ensure_active()
        if not self._pending_queue:
            raise ValueError("待参观队列为空")
        stop = self._pending_queue[0]
        if expected_relic_id is not None and stop.relic_id != expected_relic_id:
            raise ValueError("展品已不在队首，请读取最新路线后再操作")
        return stop

    def complete_visit(self, relic_id: str, guide_text: str) -> VisitRecord:
        """兼容入口：保存单单元主讲稿，并将队首移入已参观轨迹。

        第三算子使用 record_guide，以保留真实 guide_id 和分句边界。
        此入口为无稿件版本的调用方生成新 ID，不推断播放进度。
        """
        record = GuideRecord(
            guide_id=str(uuid4()), relic_id=relic_id, kind="guide", segments=(guide_text,),
        )
        return self.record_guide(record)

    def record_guide(self, record: GuideRecord) -> VisitRecord | None:
        """一次提交生成记录及必要的参观更新；校验失败不修改任何字段。

        主讲只允许队首；追问只允许最近完成的展品或正在讲解的队首。
        问答可在主讲生成结束前产生，因此不要求该文物已有 VisitRecord。
        是否通过依据核对由第三算子负责，状态层只校验结构与提交边界。
        """
        self._ensure_active()
        if not isinstance(record, GuideRecord):
            raise ValueError("record 必须是 GuideRecord")
        if any(item.guide_id == record.guide_id for item in self._guide_records):
            raise ValueError("guide_id 已存在，不能重复保存稿件")
        visits, queue = self._visited_relics, self._pending_queue
        visit = None
        if record.kind == "guide":
            stop = self._next_stop(record.relic_id)
            visit = VisitRecord(
                stop.relic_id, record.guide_text, stop.recommendation_reason,
                stop.connection_from_previous,
            )
            visits = visits + (visit,)
            queue = queue[1:]
        else:
            next_id = queue[0].relic_id if queue else None
            if record.relic_id not in (self.current_relic_id, next_id):
                raise ValueError("追问记录必须属于最近完成的展品或正在讲解的队首")
        records = self._guide_records + (record,)
        self._guide_records, self._visited_relics, self._pending_queue = records, visits, queue
        return visit

    def skip_next(self, *, expected_relic_id: str | None = None) -> RouteStop:
        """移除队首并记录跳过，不计入已参观轨迹。"""
        stop = self._next_stop(expected_relic_id)
        self._skipped_relics += (stop.relic_id,)
        self._pending_queue = self._pending_queue[1:]
        return stop

    def finish(self) -> None:
        """结束游览，可重复调用；保留未完成队列以便前端显示和生成手记。"""
        self._status = "finished"

    def to_dict(self) -> dict[str, Any]:
        """返回独立、可直接 JSON 序列化的 latest_state 快照。"""
        return {
            "session_id": self._session_id,
            "status": self._status,
            "visitor_info": _json_value(self._visitor_info),
            "visited_relics": _json_value(self._visited_relics),
            "guide_records": _json_value(self._guide_records),
            "pending_queue": _json_value(self._pending_queue),
            "skipped_relics": list(self._skipped_relics),
            "current_relic_id": self.current_relic_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SessionState":
        """从服务端保存的快照恢复并重新校验；不能用客户端快照覆盖可信状态。"""
        if not isinstance(data, Mapping):
            raise ValueError("会话快照必须是字典")
        session_id = _text(data.get("session_id"), "session_id")
        state = cls(data.get("visitor_info"), session_id=session_id)
        status = data.get("status", "active")
        if status not in ("active", "finished"):
            raise ValueError("status 必须是 active 或 finished")
        visits = _records(data.get("visited_relics", ()), VisitRecord, "visited_relics")
        queue = _records(data.get("pending_queue", ()), RouteStop, "pending_queue")
        skipped = tuple(_text(item, "skipped_relics") for item in _items(data.get("skipped_relics", ()), "skipped_relics"))
        cls._validate_route(visits, queue)
        if "guide_records" in data:
            records = _records(data["guide_records"], GuideRecord, "guide_records")
            if len({item.guide_id for item in records}) != len(records):
                raise ValueError("guide_records 的 guide_id 不能重复")
            main_records = tuple(item for item in records if item.kind == "guide")
            if len(main_records) != len(visits) or any(
                (record.relic_id, record.guide_text.strip()) != (visit.relic_id, visit.guide_text)
                for record, visit in zip(main_records, visits)
            ):
                raise ValueError("主讲记录必须与已参观轨迹的顺序及正文一致")
        else:
            # 兼容旧服务端快照；旧版本未保存稿件 ID、分句与问答，不伪造这些历史。
            records = tuple(GuideRecord(
                str(uuid4()), visit.relic_id, "guide", (visit.guide_text,),
            ) for visit in visits)
        state._visited_relics = visits
        state._guide_records = records
        state._pending_queue = queue
        state._skipped_relics = skipped
        state._status = status
        # current_relic_id 是派生信息，始终从已参观轨迹重新计算。
        return state
