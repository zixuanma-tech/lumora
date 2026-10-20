"""探索手记的数据契约；模型、核对服务与数据读取均可注入，不包含 prompt。"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Protocol

from session_state import GuideRecord, VisitRecord, VisitorInfo


SUMMARY_QUESTIONS = (
    "这次哪件展品最让你有印象？",
    "这次游览有什么感想？",
)


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _sequence(value, name: str) -> tuple:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} 必须是序列")
    return tuple(value)


def _records(value, name: str, record_type: type) -> tuple:
    result = _sequence(value, name)
    if any(not isinstance(item, record_type) for item in result):
        raise ValueError(f"{name} 的元素必须是 {record_type.__name__}")
    return result


def _ids(value, name: str) -> tuple[str, ...]:
    result = tuple(_text(item, name) for item in _sequence(value, name))
    if not result or len(set(result)) != len(result):
        raise ValueError(f"{name} 必须非空且不能重复")
    return result


@dataclass(frozen=True, slots=True)
class SummaryFeedback:
    """两个可跳过的结束问题；感想保留原文，只将纯空白视为未回答。"""

    favorite_relic_id: str | None = None
    reflection: str | None = None

    def __post_init__(self) -> None:
        if self.favorite_relic_id is not None:
            object.__setattr__(self, "favorite_relic_id", _text(
                self.favorite_relic_id, "favorite_relic_id",
            ))
        if self.reflection is not None:
            if not isinstance(self.reflection, str):
                raise ValueError("reflection 必须是字符串或 None")
            if not self.reflection.strip():
                object.__setattr__(self, "reflection", None)


@dataclass(frozen=True, slots=True)
class SummaryRelic:
    """按展品 ID 查得的基础资料；缺少名称时不可用 ID 伪装名称。"""

    relic_id: str
    name: str
    era: str = ""
    gallery_name: str = ""

    def __post_init__(self) -> None:
        for name in ("relic_id", "name"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in ("era", "gallery_name"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} 必须是字符串")
            object.__setattr__(self, name, value.strip())


@dataclass(frozen=True, slots=True)
class SummaryRequest:
    """服务端一次游览的不可变快照；讲解及问答全文均来自会话日志。

    visited_relics 决定参观足迹。guide_records 保留全部成功稿件，包含可能
    在主讲完成前生成的队首问答；这些问答不会额外增加参观足迹。
    不读取播放完成度，也不使用规划时间推断实际游览时长。
    """

    session_id: str
    visitor_info: VisitorInfo
    visited_relics: tuple[VisitRecord, ...]
    guide_records: tuple[GuideRecord, ...]
    relics: tuple[SummaryRelic, ...]
    feedback: SummaryFeedback = field(default_factory=SummaryFeedback)

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_id", _text(self.session_id, "session_id"))
        if not isinstance(self.visitor_info, VisitorInfo):
            raise ValueError("visitor_info 必须是 VisitorInfo")
        if not isinstance(self.feedback, SummaryFeedback):
            raise ValueError("feedback 必须是 SummaryFeedback")
        for name, record_type in (
            ("visited_relics", VisitRecord), ("guide_records", GuideRecord),
            ("relics", SummaryRelic),
        ):
            object.__setattr__(self, name, _records(getattr(self, name), name, record_type))
        for name, ids in (
            ("visited_relics", self.visited_relic_ids),
            ("guide_records", tuple(item.guide_id for item in self.guide_records)),
            ("relics", tuple(item.relic_id for item in self.relics)),
        ):
            if len(set(ids)) != len(ids):
                raise ValueError(f"{name} 的 ID 不能重复")
        required_ids = set(self.visited_relic_ids) | {
            item.relic_id for item in self.guide_records
        }
        if {item.relic_id for item in self.relics} != required_ids:
            raise ValueError("relics 必须准确覆盖参观及讲解记录中的展品")
        if (self.feedback.favorite_relic_id is not None
                and self.feedback.favorite_relic_id not in self.visited_relic_ids):
            raise ValueError("印象最深的展品必须属于本次已参观展品")

    @property
    def visited_relic_ids(self) -> tuple[str, ...]:
        return tuple(item.relic_id for item in self.visited_relics)


@dataclass(frozen=True, slots=True)
class SummaryStop:
    relic_id: str
    recap: str

    def __post_init__(self) -> None:
        for name in ("relic_id", "recap"):
            object.__setattr__(self, name, _text(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class SummaryInsight:
    """一项发现或知识联系，保留对应展品和讲解稿 ID 便于核对。"""

    title: str
    text: str
    relic_ids: tuple[str, ...]
    guide_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("title", "text"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in ("relic_ids", "guide_ids"):
            object.__setattr__(self, name, _ids(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class SummaryQuestion:
    """引用原始问答日志；原问题及展品信息由算子补齐，不由模型改写。"""

    guide_id: str
    answer_summary: str

    def __post_init__(self) -> None:
        for name in ("guide_id", "answer_summary"):
            object.__setattr__(self, name, _text(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class SummaryDraft:
    title: str
    introduction: str
    trail: tuple[SummaryStop, ...]
    discoveries: tuple[SummaryInsight, ...]
    connections: tuple[SummaryInsight, ...] = ()
    questions: tuple[SummaryQuestion, ...] = ()
    next_exploration: str | None = None

    def __post_init__(self) -> None:
        for name in ("title", "introduction"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name, record_type in (
            ("trail", SummaryStop), ("discoveries", SummaryInsight),
            ("connections", SummaryInsight), ("questions", SummaryQuestion),
        ):
            object.__setattr__(self, name, _records(getattr(self, name), name, record_type))
        if not self.discoveries:
            raise ValueError("手记必须包含至少一项发现")
        if self.next_exploration is not None:
            object.__setattr__(self, "next_exploration", _text(
                self.next_exploration, "next_exploration",
            ))


@dataclass(frozen=True, slots=True)
class SummaryReview:
    """核对内容是否忠实于本次讲解、问答及用户回答；没有默认批准。"""

    approved: bool
    issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.approved) is not bool:
            raise ValueError("approved 必须是布尔值")
        issues = tuple(_text(item, "issues") for item in _sequence(self.issues, "issues"))
        object.__setattr__(self, "issues", issues)
        if self.approved and self.issues:
            raise ValueError("存在待解决问题时不能批准手记")


class SummaryDataSource(Protocol):
    async def load_relics(self, relic_ids: tuple[str, ...]) -> Sequence[SummaryRelic]: ...


class SummaryComposer(Protocol):
    async def __call__(self, *, request: SummaryRequest) -> SummaryDraft: ...


class SummaryReviewer(Protocol):
    async def __call__(
        self, *, request: SummaryRequest, draft: SummaryDraft,
    ) -> SummaryReview: ...


@dataclass(frozen=True, slots=True)
class SummaryResult:
    request: SummaryRequest
    draft: SummaryDraft | None = None
    review: SummaryReview | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request, SummaryRequest):
            raise ValueError("request 必须是 SummaryRequest")
        if not self.request.visited_relics and not self.request.guide_records:
            if self.draft is not None or self.review is not None:
                raise ValueError("空游览不能附带模型草稿或核对结果")
        elif not isinstance(self.draft, SummaryDraft) or not isinstance(self.review, SummaryReview):
            raise ValueError("非空游览必须附带草稿和核对结果")
        elif not self.review.approved:
            raise ValueError("手记必须通过核对后返回")

    @property
    def status(self) -> str:
        return "empty" if self.draft is None else "generated"

    def to_dict(self) -> dict:
        """返回展示数据，不重复暴露输入中的全程讲解、游客信息或内部核对。"""
        relics = {item.relic_id: item for item in self.request.relics}
        records = {item.guide_id: item for item in self.request.guide_records}
        draft = self.draft
        return {
            "status": self.status,
            "session_id": self.request.session_id,
            "title": draft.title if draft else "本次探索手记",
            "introduction": draft.introduction if draft else "本次还没有可整理的讲解或问答记录。",
            "relics": [asdict(item) for item in self.request.relics],
            "trail": [
                {**asdict(relics[item.relic_id]), "recap": item.recap}
                for item in draft.trail
            ] if draft else [],
            "discoveries": [asdict(item) for item in draft.discoveries] if draft else [],
            "connections": [asdict(item) for item in draft.connections] if draft else [],
            "questions": [{
                "guide_id": item.guide_id,
                "relic_id": records[item.guide_id].relic_id,
                "relic_name": relics[records[item.guide_id].relic_id].name,
                "question": records[item.guide_id].question,
                "answer_summary": item.answer_summary,
            } for item in draft.questions] if draft else [],
            "next_exploration": draft.next_exploration if draft else None,
            "feedback": asdict(self.request.feedback),
        }
