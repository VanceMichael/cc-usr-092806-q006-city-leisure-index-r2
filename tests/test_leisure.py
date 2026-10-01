from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from civicflow.application import CivicFlow
from civicflow.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from civicflow.security import AccessContext


START = "2025-01-01T00:00:00+08:00"
END = "2025-12-31T23:59:59+08:00"


def ctx(actor: str, *permissions: str) -> AccessContext:
    return AccessContext(actor_id=actor, permissions=frozenset(permissions or {"*"}))


def baseline_rows(factor: float = 1.0) -> dict:
    return {
        "municipal": 1000.0 * factor,
        "urban": 800.0 * factor,
        "resident": 900.0 * factor,
    }


def metric_records(factor: float = 1.0) -> list[dict]:
    return [
        {"dimension": "resources", "metric_code": "public_facilities", "raw_value": 1200 * factor, "unit": "个", "basis": "resident", "coverage_start": START, "coverage_end": END, "source_ref": "文旅局年鉴"},
        {"dimension": "resources", "metric_code": "green_space_area", "raw_value": 5000 * factor, "unit": "万平方米", "basis": "municipal", "coverage_start": START, "coverage_end": END, "source_ref": "绿化年报"},
        {"dimension": "coverage", "metric_code": "service_coverage_rate", "raw_value": 72.0, "unit": "%", "basis": "urban", "coverage_start": START, "coverage_end": END, "source_ref": "抽样调查"},
        {"dimension": "coverage", "metric_code": "facilities_per_10k", "raw_value": 13.3 * factor, "unit": "个/万人", "basis": "resident", "coverage_start": START, "coverage_end": END, "source_ref": "统计公报"},
        {"dimension": "density", "metric_code": "facility_density", "raw_value": 2.4 * factor, "unit": "个/平方公里", "basis": "municipal", "coverage_start": START, "coverage_end": END, "source_ref": "国土测算"},
    ]


class LeisureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "leisure.sqlite3"
        self.app = CivicFlow.open(self.db_path, fixed_now="2026-09-30T12:00:00+08:00")
        self.svc = self.app.leisure
        self.admin = AccessContext.system("secretariat")
        self.analyst = ctx("analyst-li", "write:leisure", "read:leisure")
        self.reviewer = ctx("reviewer-wang", "confirm:leisure", "read:leisure")
        self.publisher = ctx("editor-chen", "publish:leisure", "read:leisure", "close:leisure", "write:leisure")

    def tearDown(self):
        self.temp.cleanup()

    # ---- 测试辅助 -----------------------------------------------------------

    def seed_city(self, city: str, factor: float = 1.0, batch_suffix: str = ""):
        pops = baseline_rows(factor)
        for scope, value in pops.items():
            self.svc.put_baseline(self.admin, "2025", city, scope, population=value, unit=f"万人({scope})", coverage_start=START, coverage_end=END)
        result = self.svc.submit_batch(self.admin, f"batch-2025-{city}{batch_suffix}", "2025", city, metric_records(factor))
        self.svc.process_recalc_jobs()
        return result

    def open_and_seed(self, *cities: str):
        self.svc.open_period(self.admin, "2025", collect_due_at="2026-03-01T09:00:00+08:00")
        for city in cities:
            self.svc.register_city(self.admin, city, city.upper())
            self.seed_city(city)

    def review_close_publish(self) -> dict:
        self.svc.begin_review(self.admin, "2025")
        self.svc.close_period(self.publisher, "2025")
        return self.svc.publish_ranking(self.publisher, "2025")

    # ---- 口径与分母版本 ------------------------------------------------------

    def test_baseline_missing_blocks_submission(self):
        self.svc.register_city(self.admin, "sh", "上海")
        self.svc.open_period(self.admin, "2025")
        with self.assertRaises(ValidationError):
            self.svc.submit_batch(self.admin, "b1", "2025", "sh", metric_records())

    def test_denominator_version_is_pinned_in_lineage(self):
        self.open_and_seed("sh")
        # 检查待发布结果谱系中固定的分母版本、指标值、出处与覆盖期。
        with self.app.database.connect() as conn:
            row = conn.execute("SELECT lineage_json FROM lr_results WHERE period='2025' AND city_code='sh' AND dimension='resources' AND status='draft'").fetchone()
        import json
        lineage = json.loads(row["lineage_json"])
        per_capita = next(i for i in lineage["inputs"] if i["metric_code"] == "public_facilities")
        self.assertEqual(per_capita["denominator"]["scope"], "resident")
        self.assertEqual(per_capita["denominator"]["version_seq"], 1)
        # 值 = 1200 / 900万人 × 10000
        self.assertAlmostEqual(per_capita["metric_value"], 1200 / 900 * 10000, places=3)
        self.assertEqual(per_capita["source_ref"], "文旅局年鉴")
        self.assertEqual(per_capita["coverage_start"], "2024-12-31T16:00:00Z")
        self.assertEqual(per_capita["coverage_end"], "2025-12-31T15:59:59Z")

    def test_denominator_revision_changes_score_before_publish(self):
        self.open_and_seed("sh")
        import json
        with self.app.database.connect() as conn:
            before = conn.execute("SELECT score FROM lr_results WHERE period='2025' AND city_code='sh' AND dimension='resources' AND status='draft'").fetchone()["score"]
        # 常住人口口径补录修正（关账前），触发资源与覆盖两个使用 resident 观测的维度重算。
        out = self.svc.put_baseline(self.admin, "2025", "sh", "resident", population=1000.0, unit="万人(常住)", coverage_start=START, coverage_end=END, reason="人口口径复核修正")
        self.assertIsNotNone(out["recalc_job"])
        self.svc.process_recalc_jobs()
        with self.app.database.connect() as conn:
            after_row = conn.execute("SELECT score FROM lr_results WHERE period='2025' AND city_code='sh' AND dimension='resources' ORDER BY created_at DESC LIMIT 1").fetchone()
            density_row = conn.execute("SELECT COUNT(*) AS n FROM lr_results WHERE period='2025' AND city_code='sh' AND dimension='density'").fetchone()
            after = after_row["score"]
        self.assertNotEqual(before, after)
        # density 维度只用 municipal 口径，不重算。
        self.assertEqual(density_row["n"], 1)

    # ---- 报送批次幂等与冲突 --------------------------------------------------

    def test_batch_resubmit_returns_existing_result(self):
        self.open_and_seed("sh")
        first = self.svc.submit_batch(self.admin, "batch-2025-sh", "2025", "sh", metric_records())
        self.assertEqual(first["status"], "duplicate")
        with self.app.database.connect() as conn:
            row = conn.execute("SELECT result_ids_json FROM lr_batches WHERE batch_no='batch-2025-sh'").fetchone()
        import json
        self.assertEqual(first["result_ids"], json.loads(row["result_ids_json"]))
        self.assertTrue(first["result_ids"])

    def test_same_batch_no_different_content_is_quarantined(self):
        self.open_and_seed("sh")
        changed = metric_records()
        changed[0] = dict(changed[0], raw_value=9999)
        with self.assertRaises(ConflictError):
            self.svc.submit_batch(self.admin, "batch-2025-sh", "2025", "sh", changed)
        with self.app.database.connect() as conn:
            conflicts = conn.execute("SELECT * FROM lr_batch_conflicts").fetchall()
            obs_count = conn.execute("SELECT COUNT(*) AS n FROM lr_observations").fetchone()["n"]
        self.assertEqual(len(conflicts), 1)
        # 冲突内容未入库：观测数保持为首批 5 条。
        self.assertEqual(obs_count, 5)
        # 冲突留痕在异常回滚后依然存在。
        self.assertEqual(self.svc.list_recalc_jobs(status="failed"), [])

    # ---- 换算双人确认 --------------------------------------------------------

    def test_conversion_requires_second_member_confirmation(self):
        self.svc.register_city(self.admin, "sh", "上海")
        self.svc.open_period(self.admin, "2025")
        for scope, value in baseline_rows().items():
            self.svc.put_baseline(self.admin, "2025", "sh", scope, population=value, unit="万人", coverage_start=START, coverage_end=END)
        records = metric_records()
        records[1] = dict(records[1], raw_value=50.0, unit="平方公里")  # 标准单位为万平方米
        submitted = self.svc.submit_batch(self.admin, "b-conv", "2025", "sh", records)
        self.assertEqual(submitted["status"], "accepted")
        # 存在待确认换算，重算任务失败而不是产出错误结果。
        processed = self.svc.process_recalc_jobs()
        self.assertTrue(any(item["status"] == "failed" for item in processed))
        obs_id = submitted["observations"][1]
        proposal = self.svc.propose_conversion(self.analyst, obs_id, multiplier=100.0, normalized_unit="万平方米", reason="绿地面积按平方公里报送，1 平方公里=100 万平方米")
        # 提出者不能确认自己的提案。
        with self.assertRaises(PermissionDenied):
            self.svc.confirm_proposal(self.analyst, proposal["proposal_id"])
        # 未确认前不能关账。
        self.svc.begin_review(self.admin, "2025")
        with self.assertRaises(ConflictError):
            self.svc.close_period(self.publisher, "2025")
        self.svc.confirm_proposal(self.reviewer, proposal["proposal_id"])
        self.svc.reset_failed_job(self.admin, processed[0]["job_id"])
        self.svc.process_recalc_jobs()
        self.svc.close_period(self.publisher, "2025")
        import json
        with self.app.database.connect() as conn:
            lineage = json.loads(conn.execute("SELECT lineage_json FROM lr_results WHERE period='2025' AND city_code='sh' AND dimension='resources' AND status='draft'").fetchone()["lineage_json"])
        green = next(i for i in lineage["inputs"] if i["metric_code"] == "green_space_area")
        self.assertEqual(green["normalized_value"], 5000.0)
        self.assertEqual(green["conversion"]["proposed_by"], "analyst-li")
        self.assertEqual(green["conversion"]["confirmed_by"], "reviewer-wang")
        self.assertTrue(green["conversion"]["reason"])

    def test_weight_change_blocked_until_confirmed(self):
        self.open_and_seed("sh")
        import json
        with self.app.database.connect() as conn:
            before = conn.execute("SELECT score FROM lr_results WHERE period='2025' AND city_code='sh' AND dimension='coverage' AND status='draft'").fetchone()["score"]
        proposal = self.svc.propose_weight(self.analyst, "2025", "sh", "coverage", "service_coverage_rate", 1.5, reason="样本扩容")
        self.assertEqual(proposal["status"], "proposed")
        # 仅提案未确认：不影响待发布结果（重算结果分值不变，结果数不增加）。
        self.svc.process_recalc_jobs()
        with self.app.database.connect() as conn:
            rows = conn.execute("SELECT score FROM lr_results WHERE period='2025' AND city_code='sh' AND dimension='coverage' AND status='draft' ORDER BY created_at").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[-1]["score"], before)
        # 另一成员确认后才生效。
        self.svc.confirm_proposal(self.reviewer, proposal["proposal_id"])
        self.svc.process_recalc_jobs()
        with self.app.database.connect() as conn:
            after = conn.execute("SELECT score FROM lr_results WHERE period='2025' AND city_code='sh' AND dimension='coverage' AND status='draft' ORDER BY created_at DESC LIMIT 1").fetchone()["score"]
        self.assertNotEqual(before, after)

    # ---- 关账、发布冻结与迟到修订勘误 ----------------------------------------

    def test_published_ranking_is_frozen_late_change_becomes_erratum(self):
        self.open_and_seed("sh", "hz")
        published = self.review_close_publish()
        self.assertEqual([r["city_code"] for r in published["rankings"]], ["hz", "sh"])
        # 正式榜单不可二次发布、不可改算。
        with self.assertRaises(ConflictError):
            self.svc.publish_ranking(self.publisher, "2025")
        with self.assertRaises(ConflictError):
            self.svc.calculate(self.admin, "2025", "sh", "resources")
        # 迟到修订必须说明原因。
        late_record = dict(metric_records()[0], raw_value=3000, source_ref="文旅局年鉴/补录")
        with self.assertRaises(ValidationError):
            self.svc.submit_batch(self.admin, "batch-late-sh", "2025", "sh", [late_record])
        late = self.svc.submit_batch(self.admin, "batch-late-sh", "2025", "sh", [late_record], reason="区管设施补录")
        self.assertTrue(late["late"])
        self.svc.process_recalc_jobs()
        # 正式榜单原貌保持不变。
        rankings = self.svc.list_rankings(self.admin, "2025")
        sh_ranking = next(r for r in rankings if r["city_code"] == "sh")
        self.assertEqual(sh_ranking["rank_no"], 2)
        erratum = self.svc.issue_errata(self.publisher, "2025", "sh", reason="区管设施补录改变人均设施数")
        self.assertEqual(erratum["old_rank"], 2)
        self.assertNotEqual(erratum["old_score"], erratum["new_score"])
        # 勘误与正式榜单及重算结果双向可追。
        with self.app.database.connect() as conn:
            for result_id in erratum["linked_result_ids"]:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM lr_results WHERE result_id=?", (result_id,)).fetchone())
        with_errata = self.svc.list_rankings(self.admin, "2025")
        sh_with = next(r for r in with_errata if r["city_code"] == "sh")
        self.assertEqual(sh_with["errata"][0]["erratum_id"], erratum["erratum_id"])
        # 无新变化时不重复出勘误。
        with self.assertRaises(ConflictError):
            self.svc.issue_errata(self.publisher, "2025", "hz", reason="尝试无变化勘误")

    def test_late_revision_only_recalculates_affected_dimension_and_city(self):
        self.open_and_seed("sh", "hz")
        self.review_close_publish()
        late_record = dict(metric_records()[0], raw_value=3000, source_ref="补录")
        self.svc.submit_batch(self.admin, "batch-late-sh", "2025", "sh", [late_record], reason="补录")
        self.svc.process_recalc_jobs()
        with self.app.database.connect() as conn:
            recalc_sh = conn.execute("SELECT COUNT(*) AS n FROM lr_results WHERE period='2025' AND city_code='sh' AND status='recalculated'").fetchone()["n"]
            recalc_hz = conn.execute("SELECT COUNT(*) AS n FROM lr_results WHERE period='2025' AND city_code='hz' AND status='recalculated'").fetchone()["n"]
        self.assertEqual(recalc_sh, 1)   # 仅 resources 维度
        self.assertEqual(recalc_hz, 0)   # 杭州不受影响

    def test_late_revision_rejecting_nonstandard_unit(self):
        self.open_and_seed("sh")
        self.review_close_publish()
        record = dict(metric_records()[1], raw_value=60.0, unit="平方公里", source_ref="补录")
        with self.assertRaises(ValidationError):
            self.svc.submit_batch(self.admin, "batch-late-unit", "2025", "sh", [record], reason="关账后换算报送")

    # ---- 合作机构城市级授权 --------------------------------------------------

    def test_partner_sees_only_granted_cities(self):
        self.open_and_seed("sh", "hz")
        self.svc.grant_city(self.admin, "org:partner-a", "sh")
        partner = self.svc.partner_context("partner-user", "org:partner-a")
        # 发布前榜单尚不存在，直接按授权读城市数据路径验证。
        self.assertTrue(self.svc._can_read_city(partner, "sh"))
        self.assertFalse(self.svc._can_read_city(partner, "hz"))
        with self.assertRaises(PermissionDenied):
            self.svc.trace_city(partner, "2025", "hz")
        # 未授权机构无法构造出写权限。
        with self.assertRaises(PermissionDenied):
            self.svc.submit_batch(partner, "x", "2025", "sh", metric_records())
        # 重复授权幂等。
        again = self.svc.grant_city(self.admin, "org:partner-a", "sh")
        self.assertTrue(again["replayed"])
        self.svc.revoke_city(self.admin, "org:partner-a", "sh")
        partner2 = self.svc.partner_context("partner-user", "org:partner-a")
        self.assertFalse(self.svc._can_read_city(partner2, "sh"))

    # ---- 进程重启后流程可继续 ------------------------------------------------

    def test_workflow_survives_process_restart(self):
        self.svc.register_city(self.admin, "sh", "上海")
        self.svc.open_period(self.admin, "2025", collect_due_at="2026-03-01T09:00:00+08:00")
        for scope, value in baseline_rows().items():
            self.svc.put_baseline(self.admin, "2025", "sh", scope, population=value, unit="万人", coverage_start=START, coverage_end=END)
        self.svc.submit_batch(self.admin, "batch-2025-sh", "2025", "sh", metric_records())
        # 不处理重算，直接模拟进程重启。
        app2 = CivicFlow.open(self.db_path, fixed_now="2026-10-01T09:00:00+08:00")
        pending = app2.leisure.list_recalc_jobs(status="pending")
        self.assertEqual(len(pending), 1)
        app2.leisure.process_recalc_jobs()
        app2.leisure.begin_review(AccessContext.system("secretariat"), "2025")
        app2.leisure.close_period(ctx("editor-chen", "publish:leisure", "read:leisure", "close:leisure"), "2025")
        rankings = app2.leisure.publish_ranking(ctx("editor-chen", "publish:leisure", "read:leisure"), "2025")
        self.assertEqual(len(rankings["rankings"]), 1)
        # 催收提醒任务同样可在重启后领取。
        claimed = app2.jobs.claim_due()
        self.assertTrue(any(job["job_type"] == "lr:collection-reminder" for job in claimed))

    # ---- 全链溯源 -----------------------------------------------------------

    def test_ranking_trace_covers_lineage(self):
        self.open_and_seed("sh", "hz")
        proposal = self.svc.propose_weight(self.analyst, "2025", "sh", "density", "facility_density", 1.2, reason="密度指标加权")
        self.svc.confirm_proposal(self.reviewer, proposal["proposal_id"])
        self.svc.process_recalc_jobs()
        self.review_close_publish()
        late_record = dict(metric_records()[0], raw_value=2000, source_ref="补录年鉴")
        self.svc.submit_batch(self.admin, "late", "2025", "sh", [late_record], reason="补录")
        self.svc.process_recalc_jobs()
        self.svc.issue_errata(self.publisher, "2025", "sh", reason="补录勘误")
        trace = self.svc.trace_city(self.admin, "2025", "sh")
        self.assertEqual(trace["ranking"]["city_code"], "sh")
        self.assertTrue(trace["errata"])
        # 每个结果都能追到指标值、分母版本、来源材料。
        for result in trace["results"]:
            for item in result["lineage"]["inputs"]:
                self.assertIn("denominator", item)
                self.assertTrue(item["denominator"]["baseline_id"])
                self.assertTrue(item["source_ref"])
                self.assertTrue(item["coverage_start"])
        # 权重调整理由与责任人可追。
        weight_proposals = [p for p in trace["proposals"] if p["kind"] == "weight"]
        self.assertEqual(weight_proposals[0]["proposed_by"], "analyst-li")
        self.assertEqual(weight_proposals[0]["confirmed_by"], "reviewer-wang")
        # 人口基准版本历史齐全。
        self.assertEqual({b["scope"] for b in trace["baselines"]}, {"municipal", "urban", "resident"})
        # 报送批次及其处理结果可追。
        batch_nos = {b["batch_no"] for b in trace["batches"]}
        self.assertIn("batch-2025-sh", batch_nos)
        self.assertIn("late", batch_nos)
        # 审计链完整。
        self.assertGreater(self.app.verify()["audit_entries"], 5)

    def test_unregistered_city_and_unknown_metric_rejected(self):
        self.svc.open_period(self.admin, "2025")
        with self.assertRaises(NotFoundError):
            self.svc.submit_batch(self.admin, "b", "2025", "nope", metric_records())
        bad = metric_records()
        bad[0] = dict(bad[0], dimension="resources", metric_code="unknown_metric")
        self.svc.register_city(self.admin, "sh", "上海")
        with self.assertRaises(ValidationError):
            self.svc.submit_batch(self.admin, "b2", "2025", "sh", bad)


if __name__ == "__main__":
    unittest.main()
