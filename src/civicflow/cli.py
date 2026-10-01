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


def leisure_demo(app: CivicFlow) -> dict:
    """走一遍城市休闲化指数复核：报送、换算双签、计算、发布、迟到修订与勘误。"""
    index = app.leisure
    operator = AccessContext.system("secretariat-a")
    reviewer = AccessContext.system("secretariat-b")
    period = index.open_period(operator, year=2025, label="2025年度",
                               coverage_start="2025-01-01T00:00:00+08:00",
                               coverage_end="2025-12-31T23:59:59+08:00")
    shanghai = index.register_city(operator, code="310000", name="上海市")
    suzhou = index.register_city(operator, code="320500", name="苏州市")
    index.grant_city(operator, org_id="org:su-research", city_id=suzhou["city_id"])

    def records(pop, cult, sport, green, coverage, area):
        return [
            {"metric": "pop_resident", "value": pop, "unit": "persons", "source_ref": "统计局年报", "source_detail": "常住人口"},
            {"metric": "cultural_facilities", "value": cult, "unit": "count", "source_ref": "文旅局台账", "source_detail": "公共文化设施"},
            {"metric": "sports_sites", "value": sport, "unit": "count", "source_ref": "体育局名录", "source_detail": "体育休闲场所"},
            {"metric": "park_green_space", "value": green, "unit": "ha", "source_ref": "绿化市容局", "source_detail": "公园绿地"},
            {"metric": "service_coverage", "value": coverage, "unit": "percent", "source_ref": "抽样调查", "source_detail": "15分钟休闲圈覆盖率"},
            {"metric": "land_area", "value": area, "unit": "km2", "source_ref": "自然资源局", "source_detail": "陆域面积"},
        ]

    sh_batch = index.submit_batch(
        operator, period_id=period["period_id"], city_id=shanghai["city_id"], batch_id="batch-sh-2025",
        records=records(24_870_000, 3_200, 2_900, 17_700, 92.5, 6_340))
    # 苏州市以“万平方米”报送绿地，需要一次说明原因并经他人确认的换算
    su_records = records(12_910_000, 980, 1_050, 4_700, 86.0, 8_657)
    su_records[3] = {"metric": "park_green_space", "value": 70_500, "unit": "mu", "source_ref": "园林绿化年报", "source_detail": "公园绿地（按亩报送，需换算为公顷）"}
    index.submit_batch(operator, period_id=period["period_id"], city_id=suzhou["city_id"],
                       batch_id="batch-su-2025", records=su_records)
    adjustment = index.propose_adjustment(
        operator, period_id=period["period_id"], city_id=suzhou["city_id"], kind="conversion",
        target="park_green_space", from_value="mu", to_value="ha", factor=1 / 15,
        reason="报送方按亩统计，按 1 亩=1/15 公顷换算")
    confirmed = index.confirm_adjustment(reviewer, adjustment["adjustment_id"])

    for city_id, reason in ((shanghai["city_id"], "首次计算"), (suzhou["city_id"], "首次计算（含已确认换算）")):
        index.compute(operator, period_id=period["period_id"], city_id=city_id, reason=reason)
    ranking = index.publish_ranking(operator, period_id=period["period_id"], title="2025城市休闲化指数年度榜单")

    # 关账后，上海迟到补录体育场所数据，只重算受影响维度，榜单原貌保留
    index.close_period(operator, period["period_id"])
    late = index.submit_batch(
        operator, period_id=period["period_id"], city_id=shanghai["city_id"], batch_id="batch-sh-2025-late",
        records=[{"metric": "sports_sites", "value": 3_150, "unit": "count",
                  "source_ref": "体育局补录", "source_detail": "跨年度补录体育休闲场所"}], late=True)
    index.recompute_affected(operator, period_id=period["period_id"], city_id=shanghai["city_id"],
                             reason="体育局迟到补录", dimensions=late["dirty_dimensions"])
    erratum = index.issue_erratum(
        reviewer, ranking_id=ranking["ranking_id"], city_id=shanghai["city_id"],
        dimension="resources", reason="体育场所迟到补录后资源维度变化，正式榜单不变")
    collection = index.schedule_collection(operator, period["period_id"])
    return {"period": period, "ranking": ranking, "adjustment": confirmed,
            "late_batch": late, "erratum": erratum, "collection_jobs": collection,
            "trace": index.trace_ranking(operator, ranking["ranking_id"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同事务平台")
    parser.add_argument("--db", default=os.getenv("CIVICFLOW_DB", "civicflow.sqlite3"))
    parser.add_argument("--now", default=None, help="测试或演示使用的固定时间")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo")
    commands.add_parser("leisure-demo")
    commands.add_parser("verify")
    commands.add_parser("list-cases")
    args = parser.parse_args(argv)
    app = CivicFlow.open(Path(args.db), fixed_now=args.now)
    if args.command == "demo": emit(demo(app))
    elif args.command == "leisure-demo": emit(leisure_demo(app))
    elif args.command == "verify": emit(app.verify())
    elif args.command == "list-cases": emit(CaseService(app.repository).list_current(AccessContext.system("cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
