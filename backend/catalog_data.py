"""Adapters for the delivered National Museum demo package.

The JSON files in ``data/`` are deliberately kept as the source of truth.  This
module only turns their records into the small immutable contracts used by the
third operator and the route planner; it does not invent historical content.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from hashlib import blake2b
from math import sqrt
from pathlib import Path
from typing import Any

from guide_models import GuideMaterial, GuideRequest
from route_models import Exhibit, ExhibitTags, Gallery, InterestProfile, MuseumSnapshot, SpatialEdge
from session_state import VisitorInfo
from summary_data import JsonSummaryDataSource


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        with path.open(encoding="utf-8-sig") as stream:
            value = json.load(stream)
    except FileNotFoundError as exc:
        raise ValueError(f"数据文件不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"数据文件无法读取：{path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"数据文件顶层必须是对象：{path.name}")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} 必须是字符串数组")
    result = []
    for item in value:
        item = _text(item, name)
        if item not in result:
            result.append(item)
    return tuple(result)


def _catalog_vector(text: str, size: int = 128) -> tuple[float, ...]:
    """Stable offline vector used only when no embedding service is injected."""
    vector = [0.0] * size
    compact = "".join(text.split()) or "展品"
    for width in (1, 2):
        for index in range(max(0, len(compact) - width + 1)):
            token = compact[index:index + width].encode("utf-8")
            slot = int.from_bytes(blake2b(token, digest_size=2).digest(), "big") % size
            vector[slot] += 1.0
    norm = sqrt(sum(item * item for item in vector))
    return tuple(item / norm for item in vector) if norm else tuple([1.0] + [0.0] * (size - 1))


class JsonGuideDataSource:
    """Load ``relics[].materials`` for the third operator.

    The callable returned by :meth:`candidates` has the exact
    ``candidates(request=GuideRequest)`` shape expected by ``make_guide_selector``.
    A record with no materials (currently ``NMC-TJ-015``) is reported explicitly
    so the caller never silently asks a model to invent a lecture.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else PROJECT_ROOT / "data" / "relics.json"

    async def load_materials(self, relic_id: str) -> tuple[GuideMaterial, ...]:
        relic_id = _text(relic_id, "relic_id")
        data = _read_json(self.path)
        relics = data.get("relics")
        if not isinstance(relics, list):
            raise ValueError("relics.json 必须包含 relics 列表")
        found = None
        for item in relics:
            if not isinstance(item, Mapping):
                raise ValueError("relics 中的每项必须是对象")
            item_id = _text(item.get("relic_id"), "relic_id")
            if item_id == relic_id:
                if found is not None:
                    raise ValueError(f"relics.json 包含重复 ID：{relic_id}")
                found = item
        if found is None:
            raise ValueError(f"relics.json 缺少展品：{relic_id}")
        raw_materials = found.get("materials", [])
        if raw_materials is None:
            raw_materials = []
        if isinstance(raw_materials, (str, bytes, Mapping)) or not isinstance(raw_materials, Sequence):
            raise ValueError(f"展品 {relic_id} 的 materials 必须是数组")
        materials = []
        for raw in raw_materials:
            if not isinstance(raw, Mapping):
                raise ValueError(f"展品 {relic_id} 的 materials 项必须是对象")
            materials.append(GuideMaterial(
                material_id=_text(raw.get("material_id"), "material_id"),
                text=_text(raw.get("text"), "text"),
                source_refs=_string_list(raw.get("source_refs"), "source_refs"),
                related_relic_ids=_string_list(raw.get("related_relic_ids", []), "related_relic_ids"),
            ))
        if not materials:
            raise ValueError(f"展品 {relic_id} 没有可用讲解史料")
        if len({item.material_id for item in materials}) != len(materials):
            raise ValueError(f"展品 {relic_id} 的 material_id 重复")
        return tuple(materials)

    async def candidates(self, *, request: GuideRequest) -> tuple[GuideMaterial, ...]:
        if not isinstance(request, GuideRequest):
            raise ValueError("request 必须是 GuideRequest")
        return await self.load_materials(request.relic_id)


class JsonMuseumDataSource:
    """Build a route-planning snapshot from galleries, relics and ``map.json``."""

    def __init__(self, *, relics_path: str | Path | None = None,
                 galleries_path: str | Path | None = None,
                 map_path: str | Path | None = None) -> None:
        data_dir = PROJECT_ROOT / "data"
        self.relics_path = Path(relics_path) if relics_path is not None else data_dir / "relics.json"
        self.galleries_path = Path(galleries_path) if galleries_path is not None else data_dir / "galleries.json"
        self.map_path = Path(map_path) if map_path is not None else data_dir / "map.json"

    async def load(self) -> MuseumSnapshot:
        relic_data = _read_json(self.relics_path)
        gallery_data = _read_json(self.galleries_path)
        map_data = _read_json(self.map_path)
        raw_galleries = gallery_data.get("galleries")
        raw_relics = relic_data.get("relics")
        nodes = map_data.get("nodes")
        raw_edges = map_data.get("edges")
        if not isinstance(raw_galleries, list) or not isinstance(raw_relics, list):
            raise ValueError("馆藏数据必须包含 galleries/relics 列表")
        if not isinstance(nodes, list) or not isinstance(raw_edges, list):
            raise ValueError("map.json 必须包含 nodes/edges 列表")

        node_by_id = {}
        for node in nodes:
            if not isinstance(node, Mapping):
                raise ValueError("map.nodes 中的每项必须是对象")
            node_id = _text(node.get("node_id"), "node_id")
            if node_id in node_by_id:
                raise ValueError(f"map.json 包含重复节点：{node_id}")
            node_by_id[node_id] = node

        galleries = []
        gallery_by_id = {}
        for raw in raw_galleries:
            if not isinstance(raw, Mapping):
                raise ValueError("galleries 中的每项必须是对象")
            gallery_id = _text(raw.get("gallery_id"), "gallery_id")
            map_node = node_by_id.get(gallery_id)
            if map_node is None:
                raise ValueError(f"展厅 {gallery_id} 在 map.json 中没有节点")
            coords = map_node.get("coords", [0.0, 0.0])
            gallery = Gallery(
                gallery_id=gallery_id,
                name=_text(raw.get("name"), "name"),
                node_id=gallery_id,
                floor=map_node.get("floor", 1),
                base_pass_min=float(map_node.get("base_pass_min", 0.0)),
                entry_coords=coords,
            )
            galleries.append(gallery)
            gallery_by_id[gallery_id] = gallery

        exhibits = []
        seen_ids = set()
        for raw in raw_relics:
            if not isinstance(raw, Mapping):
                raise ValueError("relics 中的每项必须是对象")
            relic_id = _text(raw.get("relic_id"), "relic_id")
            if relic_id in seen_ids:
                raise ValueError(f"relics.json 包含重复 ID：{relic_id}")
            seen_ids.add(relic_id)
            gallery_id = _text(raw.get("gallery_id"), "gallery_id")
            if gallery_id not in gallery_by_id:
                raise ValueError(f"展品 {relic_id} 引用了未知展厅：{gallery_id}")
            tags = raw.get("tags")
            if not isinstance(tags, Mapping):
                raise ValueError(f"展品 {relic_id} 缺少 tags 对象")
            craft = _string_list(tags.get("craft_types", []), "craft_types")
            aesthetics = _string_list(tags.get("motifs", []), "motifs")
            vector_text = "；".join([
                _text(raw.get("name"), "name"),
                *(_string_list(tags.get("era_broad", []), "era_broad")),
                *(_string_list(tags.get("era_specific", []), "era_specific")),
                *(_string_list(tags.get("category_main", []), "category_main")),
                *_string_list(tags.get("artifact_types", []), "artifact_types"),
                *craft, *aesthetics, *_string_list(tags.get("themes", []), "themes"),
            ])
            exhibits.append(Exhibit(
                exhibit_id=relic_id,
                name=_text(raw.get("name"), "name"),
                gallery_id=gallery_id,
                tags=ExhibitTags(
                    era_broad=_string_list(tags.get("era_broad", []), "era_broad"),
                    era_specific=_string_list(tags.get("era_specific", []), "era_specific"),
                    category_main=_string_list(tags.get("category_main", []), "category_main"),
                    craft_types=craft,
                    artifact_types=_string_list(tags.get("artifact_types", []), "artifact_types"),
                    motifs=aesthetics,
                    themes=_string_list(tags.get("themes", []), "themes"),
                    aesthetics=aesthetics,
                ),
                vector=_catalog_vector(vector_text),
                importance_level=raw.get("importance_level", 3),
                coords=map_node_coords(map_node),
            ))

        edges = []
        for raw in raw_edges:
            if not isinstance(raw, Mapping):
                raise ValueError("map.edges 中的每项必须是对象")
            source = _text(raw.get("source"), "source")
            target = _text(raw.get("target"), "target")
            if source not in node_by_id or target not in node_by_id:
                raise ValueError(f"空间边端点不存在：{source} -> {target}")
            edges.append(SpatialEdge(source, target, raw.get("walk_time_min")))
        floors = {node_id: node.get("floor", 1) for node_id, node in node_by_id.items()}
        return MuseumSnapshot(tuple(galleries), tuple(exhibits), tuple(edges), floors)


def map_node_coords(node: Mapping[str, Any]) -> tuple[float, float]:
    coords = node.get("coords", [0.0, 0.0])
    if not isinstance(coords, Sequence) or isinstance(coords, (str, bytes)) or len(coords) != 2:
        raise ValueError("地图节点 coords 必须是两个数值")
    return float(coords[0]), float(coords[1])


class CatalogVectorService:
    async def embed_query(self, text: str) -> tuple[float, ...]:
        return _catalog_vector(_text(text, "text"))


class CatalogInterestParser:
    """Resolve explicit catalog labels locally and fall back for ambiguous wording."""

    _FIELDS = (
        ("eras", "target_eras"),
        ("categories", "target_categories"),
        ("craft_types", "target_craft_types"),
        ("themes", "target_themes"),
        ("aesthetics", "target_aesthetics"),
    )
    _ALIASES = {
        "日常生活": (("themes", "生活美学"),),
        "制作工艺": (("themes", "工艺技术"),),
        "材料与工艺": (("themes", "工艺技术"),),
    }

    def __init__(self, taxonomy: Mapping[str, Sequence[str]], fallback) -> None:
        if not callable(fallback):
            raise TypeError("fallback 必须是可调用的兴趣解析器")
        self._taxonomy = {
            name: tuple(_text(item, name) for item in taxonomy.get(name, ()))
            for name, _ in self._FIELDS
        }
        self._fallback = fallback

    async def __call__(self, *, visitor_info: VisitorInfo) -> InterestProfile:
        if not isinstance(visitor_info, VisitorInfo):
            raise ValueError("visitor_info 必须是 VisitorInfo")
        phrases = (*visitor_info.content_interests, *visitor_info.guide_focus)
        matched = {}
        for source_name, target_name in self._FIELDS:
            values = []
            for label in self._taxonomy[source_name]:
                if any(label in phrase or len(phrase) >= 2 and phrase in label for phrase in phrases):
                    values.append(label)
            for phrase in phrases:
                for alias, targets in self._ALIASES.items():
                    if alias not in phrase:
                        continue
                    values.extend(
                        label for field, label in targets
                        if field == source_name and label in self._taxonomy[source_name]
                    )
            values = list(dict.fromkeys(values))
            matched[target_name] = tuple(values)
        profile = InterestProfile(
            **matched, query_text="；".join(visitor_info.content_interests),
        )
        return profile if profile.has_targets else await self._fallback(visitor_info=visitor_info)


def catalog_taxonomy(path: str | Path | None = None) -> dict[str, tuple[str, ...]]:
    """Collect model taxonomy from the delivered labels; no old demo labels."""
    data = _read_json(Path(path) if path is not None else PROJECT_ROOT / "data" / "relics.json")
    values = {key: set() for key in ("eras", "categories", "craft_types", "themes", "aesthetics")}
    for raw in data.get("relics", []):
        tags = raw.get("tags", {}) if isinstance(raw, Mapping) else {}
        values["eras"].update(_string_list(tags.get("era_broad", []), "era_broad"))
        values["eras"].update(_string_list(tags.get("era_specific", []), "era_specific"))
        values["categories"].update(_string_list(tags.get("category_main", []), "category_main"))
        values["craft_types"].update(_string_list(tags.get("craft_types", []), "craft_types"))
        values["craft_types"].update(_string_list(tags.get("artifact_types", []), "artifact_types"))
        values["themes"].update(_string_list(tags.get("themes", []), "themes"))
        values["aesthetics"].update(_string_list(tags.get("motifs", []), "motifs"))
    return {key: tuple(sorted(items)) for key, items in values.items()}


def catalog_metadata() -> dict[str, Any]:
    """Small public metadata payload for the HTTP museum endpoint."""
    galleries = _read_json(PROJECT_ROOT / "data" / "galleries.json").get("galleries", [])
    relics = _read_json(PROJECT_ROOT / "data" / "relics.json").get("relics", [])
    routes = _read_json(PROJECT_ROOT / "data" / "routes.json").get("recommended_routes", [])
    map_data = _read_json(PROJECT_ROOT / "data" / "map.json")
    nodes = map_data.get("nodes", [])
    return {
        "mode": "catalog",
        "name": "中国国家博物馆·寻迹测试数据",
        "notice": "展品资料来自队友整理的数据包；地图节点和步行时间为虚拟测试数据。",
        "entrance_node_id": "entrance",
        "exit_node_id": "exit",
        "nodes": [{"node_id": item["node_id"], "name": item.get("name", item["node_id"]),
                   "kind": item.get("kind", "waypoint"), "floor": item.get("floor", 1)}
                  for item in nodes if isinstance(item, Mapping)],
        "galleries": [{"gallery_id": item.get("gallery_id"), "name": item.get("name"),
                       "hall": item.get("hall")} for item in galleries if isinstance(item, Mapping)],
        "relics": [{"relic_id": item.get("relic_id"), "name": item.get("name"),
                    "gallery_id": item.get("gallery_id"), "hall": item.get("hall")}
                   for item in relics if isinstance(item, Mapping)],
        "recommended_routes": routes,
    }


def build_catalog_services(*, model_call=None):
    """Assemble the delivered 60-relic data with the existing model pipeline."""
    from model_services import build_model_services

    guide_source = JsonGuideDataSource()
    taxonomy = catalog_taxonomy()
    services = build_model_services(
        model_call=model_call,
        candidates=guide_source.candidates,
        data_source=JsonMuseumDataSource(),
        vector_service=CatalogVectorService(),
        summary_data_source=JsonSummaryDataSource(),
        taxonomy=taxonomy,
    )
    return replace(
        services,
        interest_parser=CatalogInterestParser(taxonomy, services.interest_parser),
    )


__all__ = ["CatalogInterestParser", "CatalogVectorService", "JsonGuideDataSource", "JsonMuseumDataSource",
           "build_catalog_services", "catalog_metadata", "catalog_taxonomy"]
