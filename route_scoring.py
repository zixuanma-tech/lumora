"""六维标签、语义向量与馆藏价值混合评分，以及展厅边际递减聚合。"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import fsum, hypot
from types import MappingProxyType

from route_models import Exhibit, Gallery, InterestProfile, PlannerConfig, _vector


@dataclass(frozen=True, slots=True)
class ExhibitScore:
    exhibit: Exhibit
    tag_score: float
    vector_score: float
    base_score: float
    total_score: float
    cosine_similarity: float
    dimension_matches: Mapping[str, float]
    matched_labels: tuple[str, ...]
    conjunction_multiplier: float


@dataclass(frozen=True, slots=True)
class GalleryBundle:
    gallery: Gallery
    exhibits: tuple[ExhibitScore, ...]
    gain: float
    stay_time_min: float
    density: float


def _ancestors(tag: str, parents: Mapping[str, str]) -> tuple[str, ...]:
    result = [tag]
    while tag in parents:
        tag = parents[tag]
        result.append(tag)
    return tuple(result)


def _hierarchy_match(
    targets: Sequence[str], actual: Sequence[str], parents: Mapping[str, str], partial: float,
) -> float:
    """只在已知父类且没有已知异支细类时给部分分，避免元代误命中北宋。"""
    if not targets or not actual:
        return 0.0
    best = 0.0
    actual_paths = {tag: _ancestors(tag, parents) for tag in actual}
    # 数据常同时附有父类、子类；匹配时只以最具体的已知标签表示证据。
    specific_actual = [tag for tag in actual if not any(tag in path[1:] for other, path in actual_paths.items() if other != tag)]
    for target in targets:
        target_path = _ancestors(target, parents)
        for actual_tag in specific_actual:
            if target in actual_paths[actual_tag]:
                return 1.0
            if actual_tag in target_path[1:]:
                best = max(best, partial)
    return best


def _overlap_match(targets: Sequence[Sequence[str]], actual: Sequence[str], full_hits: int) -> float:
    if not targets:
        return 0.0
    actual_set = set(actual)
    hits = sum(bool(actual_set.intersection(group)) for group in targets)
    return min(1.0, hits / min(len(targets), full_hits))


def _average(values: Sequence[float]) -> float:
    return fsum(values) / len(values) if values else 0.0


def score_exhibit(
    exhibit: Exhibit,
    interests: InterestProfile,
    query_vector: Sequence[float],
    config: PlannerConfig | None = None,
) -> ExhibitScore:
    """单品三维混合打分；过滤开放状态和已参观项由 score_exhibits 统一处理。"""
    config = config or PlannerConfig()
    if not isinstance(exhibit, Exhibit) or not isinstance(interests, InterestProfile):
        raise ValueError("评分需要 Exhibit 和 InterestProfile")
    query = _vector(query_vector, "query_vector")
    vector = _vector(exhibit.vector, f"展品 {exhibit.exhibit_id} 的 vector")
    if len(query) != len(vector):
        raise ValueError(f"展品 {exhibit.exhibit_id} 与查询向量维度不一致")
    tags = exhibit.tags
    dimensions = {}
    labels = []
    era_match = _hierarchy_match(interests.target_eras, (*tags.era_broad, *tags.era_specific), config.era_parents, config.hierarchy_partial_match)
    category_match = _hierarchy_match(interests.target_categories, (*tags.category_main, *tags.craft_types), config.craft_parents, config.hierarchy_partial_match)
    time_space = []
    if interests.target_eras:
        time_space.append(era_match)
        if era_match:
            labels.append("时空与文化圈·时代匹配")
    if interests.target_regions:
        region_match = _hierarchy_match(interests.target_regions, tags.regions, config.region_parents, config.hierarchy_partial_match)
        time_space.append(region_match)
        if region_match:
            labels.append("时空与文化圈·地域匹配")
    if time_space:
        dimensions["time_space"] = _average(time_space)
    material_craft = []
    if interests.target_categories:
        material_craft.append(category_match)
        if category_match:
            labels.append("器物与工艺材质·一级品类匹配")
    if interests.target_craft_types:
        craft_match = _hierarchy_match(interests.target_craft_types, (*tags.category_main, *tags.craft_types), config.craft_parents, config.hierarchy_partial_match)
        material_craft.append(craft_match)
        if craft_match:
            labels.append("器物与工艺材质·工艺器型匹配")
    if material_craft:
        dimensions["material_craft"] = _average(material_craft)
    for name, label in (("themes", "题材与生活场景"), ("aesthetics", "视觉与审美特征"), ("cultural_ip", "文化IP与知名度"), ("audience", "受众与体验适配")):
        targets = getattr(interests, "target_" + name)
        if targets:
            dimensions[name] = _overlap_match(targets, getattr(tags, name), config.subtag_full_hits)
            if dimensions[name]:
                labels.append(label + "匹配")
    if not dimensions:
        raise ValueError("兴趣解析结果至少需要指定一个标签维度")
    weighted_match = fsum(config.dimension_weights[name] * match for name, match in dimensions.items()) / fsum(config.dimension_weights[name] for name in dimensions)
    multiplier = 1.0
    if interests.target_eras and interests.target_categories:
        era_full, category_full = era_match == 1.0, category_match == 1.0
        if era_full and category_full:
            multiplier = config.conjunction_boost
            labels.append("时代×品类交叉命中")
        elif era_full != category_full:
            multiplier = config.partial_conjunction_penalty
    tag_score = min(config.tag_max, config.tag_max * weighted_match * multiplier)
    # 先缩放再点积，避免合法但极大的输入向量平方溢出。
    query_scale, vector_scale = max(map(abs, query)), max(map(abs, vector))
    q, v = tuple(x / query_scale for x in query), tuple(x / vector_scale for x in vector)
    cosine = max(-1.0, min(1.0, fsum(x * y for x, y in zip(q, v)) / (hypot(*q) * hypot(*v))))
    vector_score = config.vector_max * max(0.0, min(1.0, (cosine - config.similarity_threshold) / (1.0 - config.similarity_threshold)))
    base_score = config.base_max * exhibit.importance_level / 5.0
    if exhibit.importance_level == 5:
        labels.append("镇馆之宝")
    return ExhibitScore(
        exhibit, tag_score, vector_score, base_score, tag_score + vector_score + base_score,
        cosine, MappingProxyType(dimensions), tuple(labels), multiplier,
    )


def score_exhibits(
    exhibits: Iterable[Exhibit], interests: InterestProfile, query_vector: Sequence[float],
    *, visited_exhibit_ids: Iterable[str] = (), config: PlannerConfig | None = None,
) -> tuple[ExhibitScore, ...]:
    config = config or PlannerConfig()
    visited = set(visited_exhibit_ids)
    return tuple(score_exhibit(exhibit, interests, query_vector, config) for exhibit in exhibits if exhibit.is_open and exhibit.exhibit_id not in visited)


def aggregate_galleries(
    galleries: Iterable[Gallery], scores: Iterable[ExhibitScore], *, pace: str = "standard", config: PlannerConfig | None = None,
) -> tuple[GalleryBundle, ...]:
    config = config or PlannerConfig()
    if pace not in config.top_k:
        raise ValueError("pace 必须是 quick、standard 或 deep")
    grouped = {}
    for score in scores:
        if score.total_score >= config.score_threshold:
            grouped.setdefault(score.exhibit.gallery_id, []).append(score)
    result = []
    for gallery in galleries:
        selected = tuple(sorted(grouped.get(gallery.gallery_id, ()), key=lambda item: (-item.total_score, item.exhibit.exhibit_id))[:config.top_k[pace]])
        if not selected:
            continue
        gain = fsum(config.fatigue_decay ** rank * score.total_score for rank, score in enumerate(selected))
        if gain <= 0:
            continue
        stay = gallery.base_pass_min + fsum(score.exhibit.dwell_time_map[pace] * score.exhibit.crowd_factor for score in selected)
        result.append(GalleryBundle(gallery, selected, gain, stay, gain / stay if stay else float("inf")))
    return tuple(result)
