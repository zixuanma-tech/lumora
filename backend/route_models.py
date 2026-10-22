"""路线规划的数据契约与可调参数；不依赖数据库、模型 SDK 或可变会话。"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Any, Protocol

from session_state import VisitorInfo


DIMENSION_NAMES = ("time_space", "material_craft", "themes", "aesthetics", "cultural_ip", "audience")
PACE_NAMES = ("quick", "standard", "deep")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _sequence(value: Any, name: str) -> tuple:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Iterable):
        raise ValueError(f"{name} 必须是序列")
    return tuple(value)


def _texts(value: Any, name: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_text(item, name) for item in _sequence(value, name)))


def _number(value: Any, name: str, *, minimum: float = 0, maximum: float | None = None) -> float:
    try:
        finite = type(value) in (int, float) and isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError(f"{name} 必须是有限数值")
    value = float(value)
    if value < minimum or maximum is not None and value > maximum:
        raise ValueError(f"{name} 超出允许范围")
    return value


def _vector(value: Any, name: str) -> tuple[float, ...]:
    result = tuple(_number(item, name, minimum=-float("inf")) for item in _sequence(value, name))
    if not result or not any(result):
        raise ValueError(f"{name} 必须是非空且非零的向量")
    return result


def _groups(value: Any, name: str) -> tuple[tuple[str, ...], ...]:
    """外层是独立意向；内层是 OR 备选，一个备选组只贡献一次命中。"""
    result = []
    for item in _sequence(value, name):
        group = (_text(item, name),) if isinstance(item, str) else _texts(item, name)
        if not group:
            raise ValueError(f"{name} 的备选组不能为空")
        if group not in result:
            result.append(group)
    return tuple(result)


def _pace_map(value: Any, name: str, *, integer: bool = False) -> Mapping[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(PACE_NAMES):
        raise ValueError(f"{name} 必须包含 quick、standard、deep 三种节奏")
    result = {}
    for key, item in value.items():
        if integer and (type(item) is not int or item < 1):
            raise ValueError(f"{name} 的值必须是正整数")
        result[key] = item if integer else _number(item, name)
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class ExhibitTags:
    """馆藏标签。

    ``artifact_types`` and ``motifs`` are retained because the delivered data
    package uses these fields explicitly; older callers can continue using the
    original fields unchanged.
    """

    era_broad: tuple[str, ...] = ()
    era_specific: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    category_main: tuple[str, ...] = ()
    craft_types: tuple[str, ...] = ()
    artifact_types: tuple[str, ...] = ()
    motifs: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    aesthetics: tuple[str, ...] = ()
    cultural_ip: tuple[str, ...] = ()
    audience: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, _texts(getattr(self, name), name))


@dataclass(frozen=True, slots=True)
class InterestProfile:
    """内容兴趣解析器的结构化结果；不存入或覆盖原始游客信息。

    target_eras 可含大时代或具体朝代。时代、地域、品类和器型内部均为 OR。
    后四维的普通字符串表示独立目标，嵌套序列表示“动物或神话瑞兽”一组 OR。
    """

    target_eras: tuple[str, ...] = ()
    target_regions: tuple[str, ...] = ()
    target_categories: tuple[str, ...] = ()
    target_craft_types: tuple[str, ...] = ()
    target_themes: tuple[tuple[str, ...], ...] = ()
    target_aesthetics: tuple[tuple[str, ...], ...] = ()
    target_cultural_ip: tuple[tuple[str, ...], ...] = ()
    target_audience: tuple[tuple[str, ...], ...] = ()
    query_text: str = ""

    def __post_init__(self) -> None:
        for name in ("target_eras", "target_regions", "target_categories", "target_craft_types"):
            object.__setattr__(self, name, _texts(getattr(self, name), name))
        for name in ("target_themes", "target_aesthetics", "target_cultural_ip", "target_audience"):
            object.__setattr__(self, name, _groups(getattr(self, name), name))
        if not isinstance(self.query_text, str):
            raise ValueError("query_text 必须是字符串")
        object.__setattr__(self, "query_text", self.query_text.strip())

    @property
    def has_targets(self) -> bool:
        return any(getattr(self, name) for name in self.__dataclass_fields__ if name != "query_text")


@dataclass(frozen=True, slots=True)
class Exhibit:
    exhibit_id: str
    name: str
    gallery_id: str
    tags: ExhibitTags = field(default_factory=ExhibitTags)
    vector: tuple[float, ...] = ()
    importance_level: int = 1
    is_open: bool = True
    coords: tuple[float, float] = (0.0, 0.0)
    dwell_time_map: Mapping[str, float] = field(default_factory=lambda: {"quick": 3, "standard": 5, "deep": 8})
    crowd_factor: float = 1.0

    def __post_init__(self) -> None:
        for name in ("exhibit_id", "name", "gallery_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        if isinstance(self.tags, Mapping):
            object.__setattr__(self, "tags", ExhibitTags(**self.tags))
        if not isinstance(self.tags, ExhibitTags):
            raise ValueError("tags 必须是 ExhibitTags 或对应字典")
        # 空向量允许数据快照加载；执行评分时要求服务补齐，不能默认为语义 0 分。
        raw_vector = _sequence(self.vector, "vector")
        object.__setattr__(self, "vector", _vector(raw_vector, "vector") if raw_vector else ())
        if type(self.importance_level) is not int or not 1 <= self.importance_level <= 5:
            raise ValueError("importance_level 必须是 1 到 5 的整数")
        if type(self.is_open) is not bool:
            raise ValueError("is_open 必须是布尔值")
        coords = _sequence(self.coords, "coords")
        if len(coords) != 2:
            raise ValueError("coords 必须包含 x、y 两个数值")
        object.__setattr__(self, "coords", tuple(_number(item, "coords", minimum=-float("inf")) for item in coords))
        object.__setattr__(self, "dwell_time_map", _pace_map(self.dwell_time_map, "dwell_time_map"))
        crowd = _number(self.crowd_factor, "crowd_factor")
        if crowd == 0:
            raise ValueError("crowd_factor 必须大于 0")
        object.__setattr__(self, "crowd_factor", crowd)


@dataclass(frozen=True, slots=True)
class Gallery:
    gallery_id: str
    name: str
    node_id: str | None = None
    floor: str | int = 1
    base_pass_min: float = 0.0
    entry_coords: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        for name in ("gallery_id", "name"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "node_id", self.gallery_id if self.node_id is None else _text(self.node_id, "node_id"))
        if type(self.floor) not in (str, int) or isinstance(self.floor, str) and not self.floor.strip():
            raise ValueError("floor 必须是楼层字符串或整数")
        object.__setattr__(self, "floor", str(self.floor).strip())
        object.__setattr__(self, "base_pass_min", _number(self.base_pass_min, "base_pass_min"))
        entry_coords = _sequence(self.entry_coords, "entry_coords")
        if len(entry_coords) != 2:
            raise ValueError("entry_coords 必须包含 x、y 两个数值")
        object.__setattr__(self, "entry_coords", tuple(_number(item, "entry_coords", minimum=-float("inf")) for item in entry_coords))


@dataclass(frozen=True, slots=True)
class SpatialEdge:
    source: str
    target: str
    walk_time_min: float

    def __post_init__(self) -> None:
        for name in ("source", "target"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "walk_time_min", _number(self.walk_time_min, "walk_time_min"))


@dataclass(frozen=True, slots=True)
class MuseumSnapshot:
    galleries: tuple[Gallery, ...]
    exhibits: tuple[Exhibit, ...]
    edges: tuple[SpatialEdge, ...]
    node_floors: Mapping[str, str | int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, expected in (("galleries", Gallery), ("exhibits", Exhibit), ("edges", SpatialEdge)):
            values = tuple(expected(**item) if isinstance(item, Mapping) else item for item in _sequence(getattr(self, name), name))
            if any(not isinstance(item, expected) for item in values):
                raise ValueError(f"{name} 中的元素必须是 {expected.__name__}")
            object.__setattr__(self, name, values)
        for name, ids in (("gallery_id", [g.gallery_id for g in self.galleries]), ("node_id", [g.node_id for g in self.galleries]), ("exhibit_id", [e.exhibit_id for e in self.exhibits])):
            if len(ids) != len(set(ids)):
                raise ValueError(f"{name} 不能重复")
        gallery_ids = {g.gallery_id for g in self.galleries}
        if any(e.gallery_id not in gallery_ids for e in self.exhibits):
            raise ValueError("展品引用了不存在的展厅")
        if not isinstance(self.node_floors, Mapping):
            raise ValueError("node_floors 必须是字典")
        floors = {}
        for node, floor in self.node_floors.items():
            if type(floor) not in (str, int) or isinstance(floor, str) and not floor.strip():
                raise ValueError("node_floors 的楼层必须是非空字符串或整数")
            floors[_text(node, "node_floors key")] = str(floor).strip()
        for gallery in self.galleries:
            if gallery.node_id in floors and floors[gallery.node_id] != gallery.floor:
                raise ValueError("展厅楼层与 node_floors 不一致")
            floors[gallery.node_id] = gallery.floor
        object.__setattr__(self, "node_floors", MappingProxyType(floors))


def _default_era_parents() -> dict[str, str]:
    return {
        "春秋": "先秦", "战国": "先秦", "秦": "秦汉", "汉": "秦汉", "西汉": "秦汉", "东汉": "秦汉",
        "魏晋": "魏晋南北朝", "南北朝": "魏晋南北朝", "隋": "隋唐五代", "唐": "隋唐五代", "盛唐": "唐", "五代": "隋唐五代",
        "宋": "宋辽金元", "北宋": "宋", "南宋": "宋", "辽": "宋辽金元", "金": "宋辽金元", "元": "宋辽金元",
        "明": "明清", "清": "明清", "清乾隆": "清", "乾隆": "清乾隆",
    }


@dataclass(frozen=True, slots=True)
class PlannerConfig:
    """暂定系数集中在此；修改实验参数无需改搜索或评分代码。"""

    dimension_weights: Mapping[str, float] = field(default_factory=lambda: dict(zip(DIMENSION_NAMES, (25, 25, 20, 10, 10, 10))))
    tag_max: float = 50.0
    vector_max: float = 30.0
    base_max: float = 20.0
    similarity_threshold: float = 0.5
    conjunction_boost: float = 1.3
    partial_conjunction_penalty: float = 0.7
    hierarchy_partial_match: float = 0.5
    subtag_full_hits: int = 2
    score_threshold: float = 60.0
    top_k: Mapping[str, int] = field(default_factory=lambda: {"quick": 3, "standard": 5, "deep": 8})
    fatigue_decay: float = 0.85
    net_budget_ratio: float = 0.90
    floor_change_allowance: int = 2
    floor_change_penalty_min: float = 3.0
    trim_overrun_min: float = 15.0
    treasure_dwell_min: float = 4.0
    exact_gallery_limit: int = 12
    era_parents: Mapping[str, str] = field(default_factory=_default_era_parents)
    region_parents: Mapping[str, str] = field(default_factory=dict)
    craft_parents: Mapping[str, str] = field(default_factory=lambda: {"汝窑": "陶瓷", "青花": "陶瓷", "唐三彩": "陶瓷", "失蜡法": "青铜", "螺钿": "杂项工艺"})

    def __post_init__(self) -> None:
        if not isinstance(self.dimension_weights, Mapping) or set(self.dimension_weights) != set(DIMENSION_NAMES):
            raise ValueError("dimension_weights 必须包含六个维度")
        weights = {name: _number(value, name) for name, value in self.dimension_weights.items()}
        if not all(weights.values()):
            raise ValueError("每个维度权重必须大于 0")
        object.__setattr__(self, "dimension_weights", MappingProxyType(weights))
        for name in ("tag_max", "vector_max", "base_max", "conjunction_boost", "partial_conjunction_penalty", "score_threshold", "floor_change_penalty_min", "trim_overrun_min", "treasure_dwell_min"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        if self.tag_max + self.vector_max + self.base_max > 100:
            raise ValueError("三项评分上限之和不能超过 100")
        if self.score_threshold > 100:
            raise ValueError("score_threshold 不能超过 100")
        if not self.treasure_dwell_min:
            raise ValueError("treasure_dwell_min 必须大于 0")
        for name in ("similarity_threshold", "hierarchy_partial_match", "fatigue_decay", "net_budget_ratio"):
            object.__setattr__(self, name, _number(getattr(self, name), name, maximum=1))
        if self.similarity_threshold == 1:
            raise ValueError("similarity_threshold 必须小于 1")
        if self.net_budget_ratio == 0:
            raise ValueError("net_budget_ratio 必须大于 0")
        for name in ("subtag_full_hits", "floor_change_allowance", "exact_gallery_limit"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "floor_change_allowance" else 1):
                raise ValueError(f"{name} 必须是允许范围内的整数")
        if self.exact_gallery_limit > 12:
            raise ValueError("exact_gallery_limit 不能大于 12")
        object.__setattr__(self, "top_k", _pace_map(self.top_k, "top_k", integer=True))
        for name in ("era_parents", "region_parents", "craft_parents"):
            raw = getattr(self, name)
            if not isinstance(raw, Mapping):
                raise ValueError(f"{name} 必须是子标签到父标签的字典")
            parents = {_text(child, name): _text(parent, name) for child, parent in raw.items()}
            for child in parents:
                seen = set()
                cursor = child
                while cursor in parents:
                    if cursor in seen:
                        raise ValueError(f"{name} 不允许层级循环")
                    seen.add(cursor)
                    cursor = parents[cursor]
            object.__setattr__(self, name, MappingProxyType(parents))


class InterestParser(Protocol):
    async def __call__(self, *, visitor_info: VisitorInfo) -> InterestProfile: ...


class VectorService(Protocol):
    """查询与馆藏向量必须来自同一编码模型和维数。"""

    async def embed_query(self, text: str) -> Sequence[float]: ...


class MuseumDataSource(Protocol):
    async def load(self) -> MuseumSnapshot: ...
