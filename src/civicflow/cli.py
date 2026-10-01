"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .application import CivicFlow
from .cases import CaseService
from .security import AccessContext


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def demo(app: CivicFlow) -> dict:
    context = AccessContext.system("demo-operator")
    cases = CaseService(app.repository)
    created = cases.create(context, {"case_type": "协同事项", "subject": "示例联合处置", "owner_org": "org:demo", "priority": "high", "opened_at": app.clock.now()}, request_key="demo-case")
    accepted = app.inbox.receive(source="demo", source_key=created["entity_id"], sequence=1, payload={"kind": "opened"}, occurred_at=app.clock.now())
    reservation = app.reservations.reserve(resource_id="room:joint", subject_id=created["entity_id"], quantity=2, capacity=10, start_at="2026-09-28T10:00:00+08:00", end_at="2026-09-28T11:00:00+08:00", actor=context.actor_id)
    debit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="debit", reference="demo-debit", actor=context.actor_id)
    credit = app.ledger.post(journal_key="demo", account="coordination", currency="CNY", amount="12.34", direction="credit", reference="demo-credit", actor=context.actor_id)
    message = app.outbox.enqueue(topic="case.opened", aggregate_id=created["entity_id"], payload={"case_id": created["entity_id"]})
    return {"case": created, "inbox": accepted, "reservation": reservation, "entries": [debit, credit], "balance": app.ledger.balance("demo", currency="CNY"), "message_id": message, "verification": app.verify()}


def _seed_city(app: CivicFlow, svc, context, city: str, factor: float) -> None:
    start = "2025-01-01T00:00:00+08:00"; end = "2025-12-31T23:59:59+08:00"
    svc.put_baseline(context, "2025", city, "municipal", population=1000 * factor, unit="万人（市域）", coverage_start=start, coverage_end=end)
    svc.put_baseline(context, "2025", city, "urban", population=800 * factor, unit="万人（城区）", coverage_start=start, coverage_end=end)
    svc.put_baseline(context, "2025", city, "resident", population=900 * factor, unit="万人（常住）", coverage_start=start, coverage_end=end)
    records = [
        {"dimension": "resources", "metric_code": "public_facilities", "raw_value": 1200 * factor, "unit": "个", "basis": "resident", "coverage_start": start, "coverage_end": end, "source_ref": f"文旅局年鉴/{city}/2025"},
        {"dimension": "resources", "metric_code": "green_space_area", "raw_value": 5000 * factor, "unit": "万平方米", "basis": "municipal", "coverage_start": start, "coverage_end": end, "source_ref": f"绿化年报/{city}/2025"},
        {"dimension": "coverage", "metric_code": "service_coverage_rate", "raw_value": 72.0, "unit": "%", "basis": "urban", "coverage_start": start, "coverage_end": end, "source_ref": f"抽样调查/{city}/2025"},
        {"dimension": "coverage", "metric_code": "facilities_per_10k", "raw_value": 13.3 * factor, "unit": "个/万人", "basis": "resident", "coverage_start": start, "coverage_end": end, "source_ref": f"统计公报/{city}/2025"},
        {"dimension": "density", "metric_code": "facility_density", "raw_value": 2.4 * factor, "unit": "个/平方公里", "basis": "municipal", "coverage_start": start, "coverage_end": end, "source_ref": f"国土测算/{city}/2025"},
    ]
    svc.submit_batch(context, f"batch-2025-{city}", "2025", city, records)
    svc.process_recalc_jobs()


def leisure_demo(app: CivicFlow) -> dict:
    """端到端演示：两座城市报送 → 双人确认调整 → 关账发布 → 迟到修订 → 勘误溯源。"""
    svc = app.leisure
    operator = AccessContext.system("secretariat")
    analyst = AccessContext(actor_id="analyst-li", permissions=frozenset({"write:leisure", "read:leisure"}))
    checker = AccessContext(actor_id="reviewer-wang", permissions=frozenset({"confirm:leisure", "read:leisure"}))
    publisher = AccessContext(actor_id="editor-chen", permissions=frozenset({"publish:leisure", "read:leisure", "close:leisure"}))

    svc.register_city(operator, "sh", "上海")
    svc.register_city(operator, "hz", "杭州")
    svc.open_period(operator, "2025", collect_due_at="2026-03-01T09:00:00+08:00")
    _seed_city(app, svc, operator, "sh", 2.0)
    _seed_city(app, svc, operator, "hz", 1.0)

    # 合作机构仅获授权查阅上海。
    svc.grant_city(operator, "org:partner-a", "sh")

    # 研究人员提出权重调整，须另一名成员确认后才影响待发布结果。
    proposal = svc.propose_weight(analyst, "2025", "sh", "coverage", "service_coverage_rate", 1.5, reason="覆盖率样本扩容，提升其代表性")
    svc.confirm_proposal(checker, proposal["proposal_id"])
    svc.process_recalc_jobs()

    svc.begin_review(operator, "2025")
    svc.close_period(publisher, "2025")
    published = svc.publish_ranking(publisher, "2025")

    # 关账后上一年度补录（迟到修订）只重算受影响城市与维度，正式榜单原貌不变。
    start = "2025-01-01T00:00:00+08:00"; end = "2025-12-31T23:59:59+08:00"
    late_record = {"dimension": "resources", "metric_code": "public_facilities", "raw_value": 3000, "unit": "个", "basis": "resident", "coverage_start": start, "coverage_end": end, "source_ref": "文旅局年鉴/sh/2025/补录"}
    late = svc.submit_batch(operator, "batch-2025-sh-late", "2025", "sh", [late_record], reason="区管设施补录，原报送遗漏")
    svc.process_recalc_jobs()
    erratum = svc.issue_errata(publisher, "2025", "sh", reason="区管设施补录改变人均设施数，正式榜单保留原貌，以此勘误说明")

    partner = svc.partner_context("partner-user", "org:partner-a")
    trace = svc.trace_city(partner, "2025", "sh")
    return {
        "published_rankings": published["rankings"],
        "late_batch": late,
        "erratum": erratum,
        "trace_keys": sorted(trace.keys()),
        "sh_result_lineage_dimensions": sorted({r["dimension"] for r in trace["results"]}),
        "recalc_jobs": svc.list_recalc_jobs(),
        "verification": app.verify(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    commands.add_parser("leisure-demo")
    rankings_cmd = commands.add_parser("leisure-rankings")
    rankings_cmd.add_argument("--period", required=True)
    trace_cmd = commands.add_parser("leisure-trace")
    trace_cmd.add_argument("--period", required=True)
    trace_cmd.add_argument("--city", required=True)
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    elif args.command == "leisure-demo": emit(leisure_demo(app))
    elif args.command == "leisure-rankings":
        emit(app.leisure.list_rankings(AccessContext.system("cli"), args.period))
    elif args.command == "leisure-trace":
        emit(app.leisure.trace_city(AccessContext.system("cli"), args.period, args.city))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
