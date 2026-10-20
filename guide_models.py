"""讲解算子的数据契约；不绑定知识库格式、模型 SDK 或 prompt。"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Protocol

from session_state import GuideRecord, SessionState, VisitRecord, VisitorInfo


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _sequence(value, name: str) -> tuple:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} 必须是序列")
    return tuple(value)


def _texts(value, name: str) -> tuple[str, ...]:
    return tuple(_text(item, name) for item in _sequence(value, name))


@dataclass(frozen=True, slots=True)
class GuideContext:
    """Router 从服务端稿件与实际播放进度组装的快照。

    guide_id 标识一版稿件，改稿时须换 ID。completed_segments 只统计前端
    确认完整播完的连续前缀；句中打断的单元仍在 remaining_segments。
    已生成、已下发或已加入播放队列均不表示已听完。客户端不得覆盖稿件。
    """

    session_id: str
    relic_id: str
    guide_id: str
    segments: tuple[str, ...]
    completed_segments: int = 0
    generation_complete: bool = True

    def __post_init__(self) -> None:
        for name in ("session_id", "relic_id", "guide_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        segments = _sequence(self.segments, "segments")
        if any(not isinstance(text, str) or not text.strip() for text in segments):
            raise ValueError("稿件的每个播放单元必须是非空字符串")
        # 保留原文和切分边界，只复制外部可变序列。
        object.__setattr__(self, "segments", segments)
        if type(self.generation_complete) is not bool:
            raise ValueError("generation_complete 必须是布尔值")
        if not segments and self.generation_complete:
            raise ValueError("已生成完整的稿件不能为空")
        if type(self.completed_segments) is not int or not 0 <= self.completed_segments <= len(segments):
            raise ValueError("completed_segments 必须是介于 0 和稿件单元数之间的整数")

    @property
    def played_segments(self) -> tuple[str, ...]:
        return self.segments[:self.completed_segments]

    @property
    def remaining_segments(self) -> tuple[str, ...]:
        return self.segments[self.completed_segments:]


def validate_guide_context(state: SessionState, context: GuideContext | None) -> None:
    """检查会话和展品归属；当前稿件版本与回报新旧仍由 Router 核对。"""
    if context is None:
        return
    if not isinstance(context, GuideContext):
        raise ValueError("guide_context 必须是 GuideContext 或 None")
    if context.session_id != state.session_id:
        raise ValueError("讲解上下文不属于当前会话")
    next_id = state.pending_queue[0].relic_id if state.pending_queue else None
    if context.relic_id not in (state.current_relic_id, next_id):
        raise ValueError("讲解上下文的展品必须是正在讲解的队首或最近完成的展品")


@dataclass(frozen=True, slots=True)
class GuideRequest:
    """各阶段共享的不可变输入；历史只传 ID，不携带历史讲稿或大纲。

    visited_relic_ids 保留状态中的参观顺序，不代表这些稿件已经播放完。
    current_guide_text 仅用于无播放上下文时的当前展品追问。
    """

    session_id: str
    relic_id: str
    visitor_info: VisitorInfo
    visited_relic_ids: tuple[str, ...]
    question: str | None = None
    guide_context: GuideContext | None = None
    current_guide_text: str | None = None

    def __post_init__(self) -> None:
        for name in ("session_id", "relic_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if not isinstance(self.visitor_info, VisitorInfo):
            raise ValueError("visitor_info 必须是 VisitorInfo")
        ids = _texts(self.visited_relic_ids, "visited_relic_ids")
        if len(set(ids)) != len(ids):
            raise ValueError("visited_relic_ids 不能重复")
        object.__setattr__(self, "visited_relic_ids", ids)
        if self.question is not None:
            object.__setattr__(self, "question", _text(self.question, "question"))
        if self.current_guide_text is not None:
            _text(self.current_guide_text, "current_guide_text")
        if self.guide_context is not None:
            if not isinstance(self.guide_context, GuideContext):
                raise ValueError("guide_context 必须是 GuideContext")
            if (self.guide_context.session_id, self.guide_context.relic_id) != (self.session_id, self.relic_id):
                raise ValueError("guide_context 与讲解请求不匹配")

    @property
    def connection_candidates(self) -> tuple[str, ...]:
        """建议优先查找已参观文物的关联，最近的在前；候选不是史料证据。"""
        return tuple(item for item in reversed(self.visited_relic_ids) if item != self.relic_id)


@dataclass(frozen=True, slots=True)
class GuideMaterial:
    """选材适配器输出的一段材料；source_refs 可为文献页码或稳定来源 ID。

    本地只验证来源引用非空，不以存在引用作为史实已核实的证明。
    related_relic_ids 是查证后的关联对象，不从路线过渡语自动生成。
    """

    material_id: str
    text: str
    source_refs: tuple[str, ...]
    related_relic_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("material_id", "text"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in ("source_refs", "related_relic_ids"):
            object.__setattr__(self, name, _texts(getattr(self, name), name))
        if not self.source_refs:
            raise ValueError("讲解材料必须附带 source_refs")


@dataclass(frozen=True, slots=True)
class GuideSelection:
    relic_id: str
    materials: tuple[GuideMaterial, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "relic_id", _text(self.relic_id, "relic_id"))
        materials = _sequence(self.materials, "materials")
        if not materials or any(not isinstance(item, GuideMaterial) for item in materials):
            raise ValueError("materials 必须包含至少一项 GuideMaterial")
        if len({item.material_id for item in materials}) != len(materials):
            raise ValueError("material_id 不能重复")
        object.__setattr__(self, "materials", materials)


@dataclass(frozen=True, slots=True)
class GuideDraft:
    """完整草稿；composer 按句或完整播放单元切分，算子不猜测分句边界。"""

    relic_id: str
    segments: tuple[str, ...]
    used_material_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "relic_id", _text(self.relic_id, "relic_id"))
        segments = _sequence(self.segments, "segments")
        if not segments or any(not isinstance(item, str) or not item.strip() for item in segments):
            raise ValueError("segments 必须包含非空稿件播放单元")
        object.__setattr__(self, "segments", segments)
        ids = _texts(self.used_material_ids, "used_material_ids")
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("used_material_ids 必须非空且不能重复")
        object.__setattr__(self, "used_material_ids", ids)

    @property
    def guide_text(self) -> str:
        return "".join(self.segments)


@dataclass(frozen=True, slots=True)
class GuideReview:
    """由核对适配器给出判定；不实现默认放行或模型自检。"""

    approved: bool
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.approved) is not bool:
            raise ValueError("approved 必须是布尔值")
        object.__setattr__(self, "issues", _texts(self.issues, "issues"))
        if self.approved and self.issues:
            raise ValueError("存在待解决问题时不能批准讲解")


class GuideSelector(Protocol):
    async def __call__(self, *, request: GuideRequest) -> GuideSelection: ...


class GuideComposer(Protocol):
    async def __call__(self, *, request: GuideRequest, selection: GuideSelection) -> GuideDraft: ...


class GuideReviewer(Protocol):
    async def __call__(
        self, *, request: GuideRequest, selection: GuideSelection, draft: GuideDraft,
    ) -> GuideReview: ...


@dataclass(frozen=True, slots=True)
class GuideResult:
    request: GuideRequest
    selection: GuideSelection
    draft: GuideDraft
    review: GuideReview
    guide_context: GuideContext
    guide_record: GuideRecord
    resume_context: GuideContext | None = None
    visit_record: VisitRecord | None = None

    @property
    def guide_text(self) -> str:
        return self.draft.guide_text

    def to_dict(self) -> dict:
        """返回可 JSON 序列化的独立结果，不包含可变 SessionState。"""
        return {**asdict(self), "guide_text": self.guide_text}
