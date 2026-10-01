from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


PERIOD = "period:2025"
COVERAGE = ("2025-01-01T00:00:00+08:00", "2025-12-31T23:59:59+08:00")


def full_records(pop=10_000_000, cult=1200, sport=1100, green=5000, coverage=88.0, area=4000):
    return [
        {"metric": "pop_resident", "value": pop, "unit": "persons", "source_ref": "统计局", "source_detail": "常住人口"},
        {"metric": "cultural_facilities", "value": cult, "unit": "count", "source_ref": "文旅局", "source_detail": "文化设施"},
        {"metric": "sports_sites", "value": sport, "unit": "count", "source_ref": "体育局", "source_detail": "体育场所"},
        {"metric": "park_green_space", "value": green, "unit": "ha", "source_ref": "绿化局", "source_detail": "公园绿地"},
        {"metric": "service_coverage", "value": coverage, "unit": "percent", "source_ref": "抽样", "source_detail": "覆盖率"},
        {"metric": "land_area", "value": area, "unit": "km2", "source_ref": "资源局", "source_detail": "陆域面积"},
    ]


class LeisureReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "leisure.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-09-30T10:00:00+08:00")
        self.alice = AccessContext.system("secretariat-alice")
        self.bob = AccessContext.system("secretariat-bob")
        self.app.leisure.open_period(self.alice, year=2025, label="2025年度",
                                     coverage_start=COVERAGE[0], coverage_end=COVERAGE[1])
        self.sh = self.app.leisure.register_city(self.alice, code="310000", name="上海市")
        self.su = self.app.leisure.register_city(self.alice, code="320500", name="苏州市")

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self, now="2026-09-30T10:05:00+08:00"):
        return CivicFlow.open(self.db_path, fixed_now=now)

    def submit_and_compute(self, ctx, city, batch, **record_kwargs):
        self.app.leisure.submit_batch(ctx, period_id=PERIOD, city_id=city["city_id"],
                                      batch_id=batch, records=full_records(**record_kwargs))
        return self.app.leisure.compute(ctx, period_id=PERIOD, city_id=city["city_id"], reason="首次计算")

    # ---- 批次幂等与冲突 -------------------------------------------------

    def test_same_batch_returns_existing_result(self):
        first = self.app.leisure.submit_batch(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b1", records=full_records())
        second = self.app.leisure.submit_batch(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b1", records=full_records())
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["result"]["metrics"], first["metrics"])

    def test_same_batch_different_content_is_conflicted_and_not_ingested(self):
        self.app.leisure.submit_batch(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b2", records=full_records(cult=1200))
        with self.assertRaises(ConflictError):
            self.app.leisure.submit_batch(
                self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b2",
                records=full_records(cult=1299))
        with self.app.database.connect() as connection:
            conflicts = connection.execute("SELECT COUNT(*) AS n FROM leisure_batch_conflicts").fetchone()["n"]
            versions = connection.execute(
                "SELECT current_version FROM leisure_obs WHERE city_id=? AND period_id=? AND metric='cultural_facilities'",
                (self.sh["city_id"], PERIOD)).fetchone()
        self.assertEqual(conflicts, 1)
        self.assertEqual(versions["current_version"], 1)  # 冲突内容未入库

    # ---- 计算固定单位、覆盖期与分母版本 ---------------------------------

    def test_computation_pins_units_coverage_and_denominator_version(self):
        comp = self.submit_and_compute(self.alice, self.sh, "b3")
        basis = comp["basis"]
        self.assertEqual(basis["coverage_start"], "2024-12-31T16:00:00Z")
        self.assertEqual(basis["denominator"]["metric"], "pop_resident")
        self.assertEqual(basis["denominator"]["version"], 1)
        self.assertEqual(basis["metrics"]["park_green_space"]["canonical_unit"], "ha")
        self.assertEqual(basis["metrics"]["cultural_facilities"]["source_ref"], "文旅局")
        self.assertAlmostEqual(comp["total"],
                               0.4 * comp["scores"]["resources"]["score"]
                               + 0.3 * comp["scores"]["coverage"]["score"]
                               + 0.3 * comp["scores"]["density"]["score"], places=6)

    def test_unconfirmed_conversion_blocks_computation(self):
        records = full_records()
        records[3] = {"metric": "park_green_space", "value": 75000, "unit": "mu",
                      "source_ref": "绿化局", "source_detail": "按亩报送"}
        self.app.leisure.submit_batch(self.alice, period_id=PERIOD, city_id=self.su["city_id"],
                                      batch_id="b-su", records=records)
        with self.assertRaises(ValidationError):
            self.app.leisure.compute(self.alice, period_id=PERIOD, city_id=self.su["city_id"], reason="缺换算")

    def test_conversion_requires_reason_and_second_member(self):
        with self.assertRaises(ValidationError):
            self.app.leisure.propose_adjustment(
                self.alice, period_id=PERIOD, city_id=self.su["city_id"], kind="conversion",
                target="park_green_space", from_value="mu", to_value="ha", factor=1 / 15, reason="  ")
        adjustment = self.app.leisure.propose_adjustment(
            self.alice, period_id=PERIOD, city_id=self.su["city_id"], kind="conversion",
            target="park_green_space", from_value="mu", to_value="ha", factor=1 / 15,
            reason="报送按亩，换算为公顷")
        self.assertEqual(adjustment["state"], "proposed")
        with self.assertRaises(PermissionDenied):
            self.app.leisure.confirm_adjustment(self.alice, adjustment["adjustment_id"])
        confirmed = self.app.leisure.confirm_adjustment(self.bob, adjustment["adjustment_id"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["confirmed_by"], "secretariat-bob")

    def test_confirmed_conversion_affects_pending_result(self):
        records = full_records()
        records[3] = {"metric": "park_green_space", "value": 75000, "unit": "mu",
                      "source_ref": "绿化局", "source_detail": "按亩报送"}
        self.app.leisure.submit_batch(self.alice, period_id=PERIOD, city_id=self.su["city_id"],
                                      batch_id="b-su2", records=records)
        with self.assertRaises(ValidationError):
            self.app.leisure.compute(self.alice, period_id=PERIOD, city_id=self.su["city_id"], reason="未确认换算")
        adjustment = self.app.leisure.propose_adjustment(
            self.alice, period_id=PERIOD, city_id=self.su["city_id"], kind="conversion",
            target="park_green_space", from_value="mu", to_value="ha", factor=1 / 15,
            reason="1 亩等于 1/15 公顷")
        self.app.leisure.confirm_adjustment(self.bob, adjustment["adjustment_id"])
        comp = self.app.leisure.compute(self.alice, period_id=PERIOD, city_id=self.su["city_id"],
                                        reason="换算确认后计算")
        info = comp["basis"]["metrics"]["park_green_space"]
        self.assertAlmostEqual(info["converted_value"], 5000.0, places=6)
        self.assertEqual(info["conversion_adjustment"], adjustment["adjustment_id"])

    def test_weight_adjustment_applies_as_a_set_after_confirmation(self):
        self.submit_and_compute(self.alice, self.sh, "b-w")
        with self.assertRaises(ValidationError):
            self.app.leisure.propose_adjustment(
                self.alice, period_id=PERIOD, kind="weight", reason="权重之和不为 1",
                weights={"resources": 0.5, "coverage": 0.3, "density": 0.3})
        proposal = self.app.leisure.propose_adjustment(
            self.alice, period_id=PERIOD, kind="weight",
            reason="提高资源维度权重", weights={"resources": 0.5, "coverage": 0.2, "density": 0.3})
        # 确认前权重不变：立即重算应与上一版一致
        before = self.app.leisure.latest_computation(self.alice, PERIOD, self.sh["city_id"])
        self.app.leisure.compute(self.alice, period_id=PERIOD, city_id=self.sh["city_id"],
                                 reason="确认前再算一次", trigger="weight_change")
        interim = self.app.leisure.latest_computation(self.alice, PERIOD, self.sh["city_id"])
        self.assertEqual(interim["total"], before["total"])
        self.app.leisure.confirm_adjustment(self.bob, proposal["adjustment_id"])
        self.app.leisure.compute(self.alice, period_id=PERIOD, city_id=self.sh["city_id"],
                                 reason="权重确认后重算", trigger="weight_change")
        after = self.app.leisure.latest_computation(self.alice, PERIOD, self.sh["city_id"])
        self.assertEqual(after["basis"]["weights"]["resources"]["version"], 2)
        self.assertEqual(after["basis"]["weights"]["resources"]["adjustment_id"], proposal["adjustment_id"])
        self.assertNotEqual(after["total"], before["total"])

    # ---- 迟到修订：仅相关维度、相关城市重算 -----------------------------

    def test_late_revision_recomputes_only_affected_dimensions(self):
        first = self.submit_and_compute(self.alice, self.sh, "b-late", sport=1100)
        other = self.submit_and_compute(self.alice, self.su, "b-late-su")
        self.app.leisure.close_period(self.alice, PERIOD)
        with self.assertRaises(ConflictError):
            self.app.leisure.submit_batch(
                self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b-normal-after-close",
                records=[{"metric": "sports_sites", "value": 1300, "unit": "count",
                          "source_ref": "体育局", "source_detail": "补录"}])
        late = self.app.leisure.submit_batch(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b-late-fix", late=True,
            records=[{"metric": "sports_sites", "value": 1300, "unit": "count",
                      "source_ref": "体育局补录", "source_detail": "迟到补录"}])
        self.assertEqual(late["dirty_dimensions"], ["density", "resources"])
        self.app.leisure.recompute_affected(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"],
            reason="体育局迟到补录", dimensions=late["dirty_dimensions"])
        recomputed = self.app.leisure.latest_computation(self.alice, PERIOD, self.sh["city_id"])
        self.assertEqual(recomputed["trigger"], "late_revision")
        self.assertEqual(set(recomputed["scope_dimensions"]), {"resources", "density"})
        self.assertEqual(recomputed["basis"]["inherited"],
                         {"coverage": first["computation_id"]})
        # 覆盖率得分与首版完全一致，人口版本也沿用首版
        self.assertEqual(recomputed["scores"]["coverage"], first["scores"]["coverage"])
        # 分母版本在补录后仍固定记录为当时版本
        self.assertEqual(recomputed["basis"]["denominator"]["version"], 1)
        # 苏州市未受影响
        untouched = self.app.leisure.latest_computation(self.alice, PERIOD, self.su["city_id"])
        self.assertEqual(untouched["computation_id"], other["computation_id"])

    # ---- 发布冻结与勘误 -------------------------------------------------

    def test_published_ranking_is_immutable_and_errata_links_computations(self):
        first_sh = self.submit_and_compute(self.alice, self.sh, "b-pub", sport=1100)
        first_su = self.submit_and_compute(self.alice, self.su, "b-pub-su", pop=9_000_000, cult=800)
        ranking = self.app.leisure.publish_ranking(self.alice, period_id=PERIOD, title="2025年度榜单")
        entries = ranking["entries"]
        self.assertEqual([e["rank"] for e in entries], [1, 2])
        frozen_total = entries[0]["total"]
        with self.app.database.connect() as connection:
            status = connection.execute(
                "SELECT status FROM leisure_computations WHERE computation_id=?",
                (entries[0]["computation_id"],)).fetchone()["status"]
        self.assertEqual(status, "frozen")
        # 迟到修订并出勘误
        self.app.leisure.close_period(self.alice, PERIOD)
        self.app.leisure.submit_batch(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b-pub-late", late=True,
            records=[{"metric": "sports_sites", "value": 1600, "unit": "count",
                      "source_ref": "体育局", "source_detail": "补录"}])
        self.app.leisure.recompute_affected(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"],
            reason="迟到补录", dimensions=("resources", "density"))
        erratum = self.app.leisure.issue_erratum(
            self.bob, ranking_id=ranking["ranking_id"], city_id=self.sh["city_id"],
            dimension="resources", reason="体育场所补录改变资源维度")
        self.assertEqual(erratum["before_computation_id"], first_sh["computation_id"])
        self.assertNotEqual(erratum["after_computation_id"], first_sh["computation_id"])
        fresh = self.app.leisure.get_ranking(self.bob, ranking["ranking_id"])
        # 榜单原貌：排名、分数、冻结计算均不变
        self.assertEqual(fresh["entries"][0]["total"], frozen_total)
        self.assertEqual(fresh["entries"][0]["computation_id"], first_sh["computation_id"])
        self.assertEqual(len(fresh["errata"]), 1)
        self.assertEqual(fresh["errata"][0]["erratum_id"], erratum["erratum_id"])
        with self.assertRaises(ConflictError):
            self.app.leisure.issue_erratum(
                self.bob, ranking_id=ranking["ranking_id"], city_id=self.su["city_id"],
                dimension="coverage", reason="苏州无变化也想出勘误")

    # ---- 合作机构城市授权 -----------------------------------------------

    def test_partner_org_sees_only_granted_cities(self):
        self.app.leisure.grant_city(self.alice, org_id="org:su-research", city_id=self.su["city_id"])
        partner = AccessContext(actor_id="su-analyst",
                                permissions=frozenset({"read:leisure", "write:leisure"}),
                                org_id="org:su-research")
        visible = self.app.leisure.list_cities(partner)
        self.assertEqual([c["city_id"] for c in visible], [self.su["city_id"]])
        with self.assertRaises(PermissionDenied):
            self.app.leisure.get_city(partner, self.sh["city_id"])
        # 授权城市可以报送
        self.app.leisure.submit_batch(
            partner, period_id=PERIOD, city_id=self.su["city_id"], batch_id="b-partner",
            records=full_records(pop=9_000_000))
        with self.assertRaises(PermissionDenied):
            self.app.leisure.submit_batch(
                partner, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b-partner-denied",
                records=full_records())
        # 发布后未授权城市不在榜单与追溯中出现
        self.submit_and_compute(self.alice, self.sh, "b-grant-sh")
        self.app.leisure.compute(self.alice, period_id=PERIOD, city_id=self.su["city_id"], reason="合作机构数据计算")
        ranking = self.app.leisure.publish_ranking(self.alice, period_id=PERIOD, title="授权可见性榜单")
        partner_view = self.app.leisure.get_ranking(partner, ranking["ranking_id"])
        self.assertEqual({e["city_id"] for e in partner_view["entries"]}, {self.su["city_id"]})
        trace = self.app.leisure.trace_ranking(partner, ranking["ranking_id"])
        self.assertEqual({t["city_id"] for t in trace}, {self.su["city_id"]})

    # ---- 进程重启后催收、复核与关账可继续 --------------------------------

    def test_work_survives_process_restart(self):
        self.app.leisure.schedule_collection(self.alice, PERIOD)
        self.app.leisure.close_period(self.alice, PERIOD)
        restarted = self.reopen()
        # 关账状态持久化
        self.assertEqual(restarted.leisure.get_period(PERIOD)["state"], "closed")
        # 催收任务持久化，重启后可领取
        claimed = restarted.jobs.claim_due()
        self.assertEqual(len(claimed), 12)  # 两个城市 × 六个指标
        self.assertTrue(all(job["job_type"] == "leisure_collection" for job in claimed))
        # 重启后仍可完成迟到补录与重算（复核继续）
        restarted.leisure.submit_batch(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b-restart", late=True,
            records=full_records())
        comp = restarted.leisure.compute(self.alice, period_id=PERIOD, city_id=self.sh["city_id"],
                                         reason="重启后完成计算")
        self.assertEqual(comp["status"], "current")
        # 幂等：同一批次在重启后重放仍返回原结果
        again = restarted.leisure.submit_batch(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], batch_id="b-restart", late=True,
            records=full_records())
        self.assertEqual(again["status"], "duplicate")

    # ---- 端到端追溯 -----------------------------------------------------

    def test_trace_from_rank_covers_values_population_version_sources_and_responsibility(self):
        records = full_records()
        records[3] = {"metric": "park_green_space", "value": 75000, "unit": "mu",
                      "source_ref": "绿化局", "source_detail": "按亩报送"}
        self.app.leisure.submit_batch(self.alice, period_id=PERIOD, city_id=self.su["city_id"],
                                      batch_id="b-trace", records=records)
        conversion = self.app.leisure.propose_adjustment(
            self.alice, period_id=PERIOD, city_id=self.su["city_id"], kind="conversion",
            target="park_green_space", from_value="mu", to_value="ha", factor=1 / 15,
            reason="亩转公顷")
        self.app.leisure.confirm_adjustment(self.bob, conversion["adjustment_id"])
        weight = self.app.leisure.propose_adjustment(
            self.alice, period_id=PERIOD, kind="weight", reason="方法学校准",
            weights={"resources": 0.5, "coverage": 0.2, "density": 0.3})
        self.app.leisure.confirm_adjustment(self.bob, weight["adjustment_id"])
        self.app.leisure.compute(self.alice, period_id=PERIOD, city_id=self.su["city_id"], reason="追溯用计算")
        ranking = self.app.leisure.publish_ranking(self.alice, period_id=PERIOD, title="追溯榜单")
        trace = self.app.leisure.trace_ranking(self.bob, ranking["ranking_id"])
        row = next(t for t in trace if t["city_id"] == self.su["city_id"])
        # 人口版本
        self.assertEqual(row["denominator_version"]["metric"], "pop_resident")
        self.assertEqual(row["denominator_version"]["version"], 1)
        # 指标值与来源材料、报送责任人
        metric = row["metric_values"]["cultural_facilities"]
        self.assertEqual(metric["source_ref"], "文旅局")
        self.assertEqual(metric["recorded_by"], "secretariat-alice")
        self.assertEqual(metric["version"], 1)
        # 调整理由与双方责任人（提出者、确认者）
        kinds = {a["kind"]: a for a in row["adjustments"]}
        self.assertEqual(set(kinds), {"conversion", "weight"})
        self.assertEqual(kinds["conversion"]["proposed_by"], "secretariat-alice")
        self.assertEqual(kinds["conversion"]["confirmed_by"], "secretariat-bob")
        self.assertEqual(kinds["conversion"]["reason"], "亩转公顷")
        self.assertEqual(kinds["weight"]["state"], "confirmed")
        # 覆盖期固定
        self.assertEqual(row["coverage"]["start"], "2024-12-31T16:00:00Z")
        # 计算责任人
        self.assertEqual(row["computed_by"], "secretariat-alice")

    def test_alternate_population_basis_is_pinned_per_computation(self):
        records = full_records()
        records.append({"metric": "pop_urban", "value": 8_800_000, "unit": "persons",
                        "source_ref": "公安户籍", "source_detail": "城区人口"})
        self.app.leisure.submit_batch(self.alice, period_id=PERIOD, city_id=self.sh["city_id"],
                                      batch_id="b-urban", records=records)
        resident = self.app.leisure.compute(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], reason="常住人口口径")
        urban = self.app.leisure.compute(
            self.alice, period_id=PERIOD, city_id=self.sh["city_id"], reason="复核改用城区人口口径",
            denominator_metric="pop_urban")
        self.assertEqual(resident["basis"]["denominator"]["metric"], "pop_resident")
        self.assertEqual(urban["basis"]["denominator"]["metric"], "pop_urban")
        self.assertEqual(urban["basis"]["denominator"]["value"], 8_800_000)
        self.assertNotEqual(resident["total"], urban["total"])
        # 两份计算各自固定，互不覆盖
        history = self.app.leisure.list_computations(self.alice, period_id=PERIOD, city_id=self.sh["city_id"])
        self.assertEqual([c["status"] for c in history], ["superseded", "current"])


if __name__ == "__main__":
    unittest.main()
