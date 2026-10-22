"""六维混合评分的行为回归；使用本地测试向量，不依赖模型服务。"""

from dataclasses import replace
from math import sqrt
import unittest

from route_models import Exhibit, ExhibitTags, Gallery, InterestProfile, MuseumSnapshot, PlannerConfig
from route_scoring import aggregate_galleries, score_exhibit, score_exhibits


class HybridScoringTests(unittest.TestCase):
    def make_exhibit(self, **changes):
        exhibit = Exhibit(
            "relic", "测试文物", "g", vector=(1, 0), importance_level=5,
            tags=ExhibitTags(category_main=("陶瓷",)),
        )
        return replace(exhibit, **changes)

    def test_unspecified_dimensions_do_not_dilute_specified_interest(self):
        result = score_exhibit(self.make_exhibit(), InterestProfile(target_categories=("陶瓷",)), (1, 0))
        self.assertEqual(dict(result.dimension_matches), {"material_craft": 1})
        self.assertEqual((result.tag_score, result.vector_score, result.base_score), (50, 30, 20))
        self.assertEqual(result.total_score, 100)

    def test_era_hierarchy_is_directional_and_known_siblings_do_not_match(self):
        for targets, actual, expected in (
            (("宋辽金元",), ExhibitTags(era_specific=("北宋",)), 1),
            (("北宋",), ExhibitTags(era_broad=("宋辽金元",)), 0.5),
            (("北宋",), ExhibitTags(era_broad=("宋辽金元",), era_specific=("元",)), 0),
            (("唐", "宋"), ExhibitTags(era_specific=("北宋",)), 1),
        ):
            with self.subTest(targets=targets, actual=actual):
                result = score_exhibit(self.make_exhibit(tags=actual), InterestProfile(target_eras=targets), (1, 0))
                self.assertEqual(result.dimension_matches["time_space"], expected)

    def test_craft_hierarchy_supports_parent_fallback_and_rejects_known_other_craft(self):
        interest = InterestProfile(target_craft_types=("汝窑",))
        for tags, expected in (
            (ExhibitTags(category_main=("陶瓷",)), 0.5),
            (ExhibitTags(category_main=("陶瓷",), craft_types=("汝窑",)), 1),
            (ExhibitTags(category_main=("陶瓷",), craft_types=("青花",)), 0),
        ):
            with self.subTest(tags=tags):
                result = score_exhibit(self.make_exhibit(tags=tags), interest, (1, 0))
                self.assertEqual(result.dimension_matches["material_craft"], expected)

    def test_first_two_dimensions_average_only_requested_parts(self):
        tags = ExhibitTags(era_specific=("北宋",), regions=("中原",), category_main=("陶瓷",))
        result = score_exhibit(self.make_exhibit(tags=tags), InterestProfile(
            target_eras=("宋",), target_regions=("江南",),
            target_categories=("陶瓷",), target_craft_types=("汝窑",),
        ), (1, 0))
        self.assertEqual(result.dimension_matches["time_space"], 0.5)
        self.assertEqual(result.dimension_matches["material_craft"], 0.75)

    def test_or_group_is_one_intention_and_two_independent_hits_fill_dimension(self):
        for targets, actual, expected in (
            ((("动物萌宠", "神话瑞兽"),), ("神话瑞兽",), 1),
            (("动物萌宠", "古代科技"), ("动物萌宠",), 0.5),
            (("动物萌宠", "古代科技", "饮食宴饮"), ("动物萌宠", "古代科技"), 1),
        ):
            with self.subTest(targets=targets):
                result = score_exhibit(self.make_exhibit(tags=ExhibitTags(themes=actual)), InterestProfile(target_themes=targets), (1, 0))
                self.assertEqual(result.dimension_matches["themes"], expected)

    def test_six_dimensions_use_configured_weights(self):
        interests = InterestProfile(
            target_eras=("宋",), target_categories=("陶瓷",),
            target_themes=("动物萌宠",), target_aesthetics=("极简美学",),
            target_cultural_ip=("历史课本同款",), target_audience=("亲子必看",),
        )
        tags = ExhibitTags(era_specific=("北宋",), themes=("动物萌宠",), cultural_ip=("历史课本同款",))
        result = score_exhibit(self.make_exhibit(tags=tags), interests, (1, 0))
        self.assertEqual(len(result.dimension_matches), 6)
        self.assertAlmostEqual(result.tag_score, 50 * (25 + 20 + 10) / 100 * 0.7)

    def test_boost_uses_era_and_main_category_and_caps_tag_score(self):
        both = InterestProfile(target_eras=("唐",), target_categories=("陶瓷",))
        hit = self.make_exhibit(tags=ExhibitTags(era_specific=("盛唐",), category_main=("陶瓷",)))
        result = score_exhibit(hit, both, (1, 0))
        self.assertEqual(result.conjunction_multiplier, 1.3)
        self.assertEqual(result.tag_score, 50)
        single = score_exhibit(replace(hit, tags=ExhibitTags(era_specific=("盛唐",))), both, (1, 0))
        self.assertEqual(single.conjunction_multiplier, 0.7)
        self.assertAlmostEqual(single.tag_score, 17.5)
        region_only = InterestProfile(target_regions=("中原",), target_categories=("陶瓷",))
        result = score_exhibit(hit, region_only, (1, 0))
        self.assertEqual(result.conjunction_multiplier, 1)

    def test_vector_threshold_stretch_and_numerical_scaling(self):
        interest = InterestProfile(target_categories=("陶瓷",))
        for cosine, expected in ((-1, 0), (0.5, 0), (0.75, 15), (1, 30)):
            vector = (cosine, sqrt(max(0, 1 - cosine * cosine)))
            with self.subTest(cosine=cosine):
                result = score_exhibit(self.make_exhibit(vector=vector), interest, (1, 0))
                self.assertAlmostEqual(result.vector_score, expected)
        result = score_exhibit(self.make_exhibit(vector=(1e300, 1e300)), interest, (1e300, 1e300))
        self.assertAlmostEqual(result.vector_score, 30)

    def test_closed_and_visited_exhibits_are_filtered_before_vector_validation(self):
        exhibits = (
            self.make_exhibit(exhibit_id="closed", is_open=False, vector=()),
            self.make_exhibit(exhibit_id="visited", vector=()),
            self.make_exhibit(exhibit_id="eligible"),
        )
        result = score_exhibits(exhibits, InterestProfile(target_categories=("陶瓷",)), (1, 0), visited_exhibit_ids=("visited",))
        self.assertEqual(tuple(item.exhibit.exhibit_id for item in result), ("eligible",))

    def test_missing_or_incompatible_vectors_fail_instead_of_scoring_zero(self):
        interest = InterestProfile(target_categories=("陶瓷",))
        for exhibit, query in ((self.make_exhibit(vector=()), (1, 0)), (self.make_exhibit(), (1,)), (self.make_exhibit(), (0, 0))):
            with self.subTest(exhibit=exhibit, query=query), self.assertRaises(ValueError):
                score_exhibit(exhibit, interest, query)


class GalleryAggregationTests(unittest.TestCase):
    def test_score_threshold_top_k_discount_and_time_are_independent_per_gallery(self):
        interest = InterestProfile(target_categories=("陶瓷",))
        exhibits = tuple(Exhibit(
            f"e{index}", f"文物{index}", "g", tags=ExhibitTags(category_main=("陶瓷",)),
            vector=(1, 0), importance_level=5 - index,
            dwell_time_map={"quick": 3, "standard": 5, "deep": 8}, crowd_factor=2,
        ) for index in range(5))
        # 第六件不匹配标签且无语义分，低于及格线。
        exhibits += (Exhibit("low", "低分文物", "g", vector=(0, 1)),)
        scores = score_exhibits(exhibits, interest, (1, 0))
        result = aggregate_galleries((Gallery("g", "展厅", base_pass_min=2), Gallery("empty", "空厅")), scores, pace="quick")
        self.assertEqual(len(result), 1)
        bundle = result[0]
        self.assertEqual(tuple(item.exhibit.exhibit_id for item in bundle.exhibits), ("e0", "e1", "e2"))
        expected_gain = 100 + 0.85 * 96 + 0.85 ** 2 * 92
        self.assertAlmostEqual(bundle.gain, expected_gain)
        self.assertEqual(bundle.stay_time_min, 2 + 3 * 3 * 2)
        self.assertAlmostEqual(bundle.density, expected_gain / 20)

    def test_parameters_can_be_tuned_without_source_changes(self):
        config = PlannerConfig(score_threshold=99, top_k={"quick": 1, "standard": 1, "deep": 1}, fatigue_decay=0.5)
        exhibits = (
            Exhibit("a", "甲", "g", tags=ExhibitTags(category_main=("陶瓷",)), vector=(1, 0), importance_level=5),
            Exhibit("b", "乙", "g", tags=ExhibitTags(category_main=("陶瓷",)), vector=(1, 0), importance_level=4),
        )
        scores = score_exhibits(exhibits, InterestProfile(target_categories=("陶瓷",)), (1, 0), config=config)
        bundles = aggregate_galleries((Gallery("g", "展厅"),), scores, config=config)
        self.assertEqual(tuple(item.exhibit.exhibit_id for item in bundles[0].exhibits), ("a",))
        self.assertEqual(bundles[0].gain, 100)


class PlanningModelValidationTests(unittest.TestCase):
    def test_invalid_configurations_cannot_break_scoring_and_time_bounds(self):
        for changes in ({"tag_max": 70}, {"score_threshold": 101},
                        {"treasure_dwell_min": 0}, {"exact_gallery_limit": 13},
                        {"era_parents": {"唐": "盛唐", "盛唐": "唐"}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PlannerConfig(**changes)

    def test_nonfinite_geometry_and_invalid_floors_are_rejected(self):
        with self.assertRaises(ValueError):
            Gallery("g", "展厅", entry_coords=(float("nan"), 0))
        for floor in (None, True, 1.5, " "):
            with self.subTest(floor=floor), self.assertRaises(ValueError):
                MuseumSnapshot([], [], [], {"s": floor})


if __name__ == "__main__":
    unittest.main()
