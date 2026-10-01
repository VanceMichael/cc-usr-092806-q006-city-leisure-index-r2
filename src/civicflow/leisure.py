"""城市休闲化指数复核领域服务。

按城市与统计期保存人口基准、休闲资源、服务覆盖、空间密度、样本出处与指标权重；
每次计算固定所采用的单位、覆盖期与分母版本。换算或权重调整需说明原因并由另一名
成员确认后才影响待发布结果；迟到修订只触发相关维度和城市的重算；正式榜单不可变，
变化以带关联关系的勘误说明，并可从任一排名追溯到指标值、人口版本、来源、调整理由
与责任人。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .jsonutil import canonical_json, digest_json
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant

# 指标及其固定计量单位
METRIC_UNITS = {
    "pop_resident": "persons",       # 常住人口（默认分母）
    "pop_urban": "persons",          # 城区人口口径
    "pop_admin": "persons",          # 市域人口口径
    "land_area": "km2",              # 陆域面积
    "cultural_facilities": "count",  # 公共文化设施数
    "sports_sites": "count",         # 体育休闲场所数
    "park_green_space": "ha",        # 公园绿地面积
    "service_coverage": "percent",   # 休闲服务覆盖率
}
DIMENSIONS = ("resources", "coverage", "density")
DEFAULT_DENOMINATOR = "pop_resident"
# 默认计算（常住人口口径）必须报送的指标；pop_urban/pop_admin 为可选复核口径
COLLECTION_METRICS = ("pop_resident", "cultural_facilities", "sports_sites",
                      "park_green_space", "service_coverage", "land_area")
# 每个维度使用的指标
DIMENSION_METRICS = {
    "resources": ("cultural_facilities", "sports_sites", "park_green_space"),
    "coverage": ("service_coverage",),
    "density": ("land_area", "cultural_facilities", "sports_sites", "park_green_space"),
}
# 指标修订会弄脏的维度（人口口径变化影响所有按人计算的维度）
METRIC_DIRTY_DIMENSIONS = {
    "pop_resident": ("resources", "density"),
    "pop_urban": ("resources", "density"),
    "pop_admin": ("resources", "density"),
    "land_area": ("density",),
    "cultural_facilities": ("resources", "density"),
    "sports_sites": ("resources", "density"),
    "park_green_space": ("resources", "density"),
    "service_coverage": ("coverage",),
}
DEFAULT_WEIGHTS = {"resources": 0.4, "coverage": 0.3, "density": 0.3}
# 指标口径标定：某人均/密度水平对应维度满分，固定后不再随数据变化
CALIBRATION = {
    "cultural_per_10k_full": 2.0,
    "sports_per_10k_full": 2.0,
    "green_m2_per_person_full": 15.0,
    "facilities_per_km2_full": 5.0,
    "green_ha_per_km2_full": 100.0,
}


def _component(value: float, full: float) -> float:
    return round(max(0.0, min(100.0, value / full * 100.0)), 6)


@dataclass(frozen=True)
class LeisureIndex:
    """封装城市休闲化指数的数据报送、复核、计算、发布、勘误与追溯。"""

    database: Database
    clock: Clock
    audit: AuditLog

    # ---- 基础目录：城市、授权、统计期 -----------------------------------

    def register_city(self, context: AccessContext, *, code: str, name: str) -> dict:
        context.require("write:leisure")
        require_safe(code, "城市代码")
        if not name or not name.strip():
            raise ValidationError("城市名称不能为空")
        city_id = f"city:{code.strip()}"
        now = self.clock.now()
        with self.database.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO leisure_cities(city_id,code,name,created_at) VALUES(?,?,?,?)",
                    (city_id, code.strip(), name.strip(), now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("城市代码已存在") from exc
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.city.register",
                              entity_type="leisure_cities", entity_id=city_id, version=1, detail={"code": code, "name": name})
        return self.get_city(context, city_id)

    def grant_city(self, context: AccessContext, *, org_id: str, city_id: str) -> dict:
        """授权合作机构仅可查阅/报送指定城市。"""
        context.require("grant:leisure")
        require_safe(org_id, "机构标识")
        now = self.clock.now()
        with self.database.transaction() as connection:
            self._city_row(connection, city_id)
            connection.execute(
                "INSERT INTO leisure_city_grants(org_id,city_id,granted_by,granted_at) VALUES(?,?,?,?) "
                "ON CONFLICT(org_id,city_id) DO NOTHING",
                (org_id, city_id, context.actor_id, now),
            )
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.city.grant",
                              entity_type="leisure_city_grants", entity_id=f"{org_id}@{city_id}", version=1,
                              detail={"org_id": org_id, "city_id": city_id})
        return {"org_id": org_id, "city_id": city_id, "granted_at": now}

    def open_period(self, context: AccessContext, *, year: int, label: str,
                    coverage_start: str, coverage_end: str) -> dict:
        context.require("write:leisure")
        if not isinstance(year, int) or year < 1900:
            raise ValidationError("统计年度不合法")
        start = canonical_instant(coverage_start); end = canonical_instant(coverage_end)
        if end <= start:
            raise ValidationError("覆盖期结束必须晚于开始")
        period_id = f"period:{year}"
        now = self.clock.now()
        with self.database.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO leisure_periods(period_id,year,label,coverage_start,coverage_end,state) VALUES(?,?,?,?,?,?)",
                    (period_id, year, label.strip() or str(year), start, end, "open"),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("统计期已存在") from exc
            for dimension, weight in DEFAULT_WEIGHTS.items():
                connection.execute(
                    "INSERT INTO leisure_weights(period_id,dimension,weight,version,changed_by,changed_at) VALUES(?,?,?,1,?,?)",
                    (period_id, dimension, weight, context.actor_id, now),
                )
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.period.open",
                              entity_type="leisure_periods", entity_id=period_id, version=1,
                              detail={"year": year, "coverage_start": start, "coverage_end": end})
        return self.get_period(period_id)

    def close_period(self, context: AccessContext, period_id: str) -> dict:
        """关账：常规报送不再受理，状态持久化，进程重启后仍为已关账。"""
        context.require("close:leisure")
        with self.database.transaction() as connection:
            period = self._period_row(connection, period_id)
            if period["state"] != "open":
                raise ConflictError(f"统计期当前状态为 {period['state']}")
            now = self.clock.now()
            connection.execute(
                "UPDATE leisure_periods SET state='closed',closed_by=?,closed_at=? WHERE period_id=?",
                (context.actor_id, now, period_id),
            )
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.period.close",
                              entity_type="leisure_periods", entity_id=period_id, version=1, detail={})
        return self.get_period(period_id)

    def get_city(self, context: AccessContext, city_id: str) -> dict:
        context.require("read:leisure")
        with self.database.connect() as connection:
            row = self._city_row(connection, city_id)
            self._require_city(connection, context, city_id)
            result = dict(row)
            result["granted_orgs"] = [r["org_id"] for r in connection.execute(
                "SELECT org_id FROM leisure_city_grants WHERE city_id=? ORDER BY org_id", (city_id,))]
            return result

    def list_cities(self, context: AccessContext) -> list[dict]:
        context.require("read:leisure")
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM leisure_cities ORDER BY code").fetchall()
            result = []
            for row in rows:
                if self._can_city(connection, context, row["city_id"]):
                    item = dict(row)
                    item["granted_orgs"] = [r["org_id"] for r in connection.execute(
                        "SELECT org_id FROM leisure_city_grants WHERE city_id=? ORDER BY org_id", (row["city_id"],))]
                    result.append(item)
            return result

    def get_period(self, period_id: str) -> dict:
        with self.database.connect() as connection:
            return dict(self._period_row(connection, period_id))

    # ---- 数据报送：批次幂等与冲突 ---------------------------------------

    def submit_batch(self, context: AccessContext, *, period_id: str, city_id: str, batch_id: str,
                     records: Iterable[Mapping[str, object]], late: bool = False) -> dict:
        """接收一个报送批次。

        同一批次编号再次提交返回既有处理结果；编号相同但内容不一致时记录冲突，
        暂不入库、不计算。``late=True`` 表示关账后的迟到补录，仍可入库并触发
        相关维度与城市的重算标记。
        """
        context.require("write:leisure")
        require_safe(batch_id, "批次编号")
        records = [dict(r) for r in records]
        if not records:
            raise ValidationError("批次不能为空")
        digest = digest_json(records)
        now = self.clock.now()
        with self.database.transaction() as connection:
            period = self._period_row(connection, period_id)
            self._city_row(connection, city_id)
            self._require_city(connection, context, city_id)
            existing = connection.execute(
                "SELECT * FROM leisure_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if existing:
                if existing["payload_digest"] != digest:
                    connection.execute(
                        "INSERT INTO leisure_batch_conflicts(batch_id,existing_digest,incoming_digest,received_at) VALUES(?,?,?,?)",
                        (batch_id, existing["payload_digest"], digest, now),
                    )
                    # 先落库冲突记录，再拒绝入库，避免异常回滚抹掉冲突痕迹
                    connection.commit()
                    raise ConflictError("相同批次编号对应不同内容，暂不入库")
                return {"status": "duplicate", "batch_id": batch_id,
                        "result": json.loads(existing["result_json"])}
            if period["state"] != "open" and not late:
                raise ConflictError("统计期已关账，常规报送不再受理；迟到补录需显式标记 late")
            ingested = []
            for record in records:
                ingested.append(self._ingest_record(connection, context, period_id, city_id, batch_id, record, now))
            result = {"status": "accepted", "batch_id": batch_id, "period_id": period_id,
                      "city_id": city_id, "late": bool(late), "metrics": ingested,
                      "dirty_dimensions": sorted({d for r in ingested for d in METRIC_DIRTY_DIMENSIONS[r["metric"]]})}
            connection.execute(
                "INSERT INTO leisure_batches(batch_id,period_id,city_id,payload_digest,status,result_json,recorded_by,received_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (batch_id, period_id, city_id, digest, "accepted_late" if late else "accepted",
                 canonical_json(result), context.actor_id, now),
            )
            self.audit.append(connection, actor_id=context.actor_id,
                              action="leisure.batch.submit_late" if late else "leisure.batch.submit",
                              entity_type="leisure_batches", entity_id=batch_id, version=1,
                              detail={"period_id": period_id, "city_id": city_id, "digest": digest})
            return result

    def _ingest_record(self, connection, context: AccessContext, period_id: str, city_id: str,
                       batch_id: str, record: Mapping[str, object], now: str) -> dict:
        metric = str(record.get("metric", "")).strip()
        if metric not in METRIC_UNITS:
            raise ValidationError(f"未知指标: {metric}")
        value = record.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValidationError(f"{metric} 的值必须是有限数字")
        value = float(value)
        unit = str(record.get("unit", "")).strip()
        source_ref = str(record.get("source_ref", "")).strip()
        source_detail = str(record.get("source_detail", "")).strip()
        if not unit or not source_ref:
            raise ValidationError(f"{metric} 必须声明单位与样本出处")
        row = connection.execute(
            "SELECT current_version FROM leisure_obs WHERE city_id=? AND period_id=? AND metric=?",
            (city_id, period_id, metric)).fetchone()
        version = (row["current_version"] + 1) if row else 1
        connection.execute(
            "INSERT INTO leisure_obs_versions(city_id,period_id,metric,version,value,unit,source_ref,source_detail,batch_id,recorded_by,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (city_id, period_id, metric, version, value, unit, source_ref, source_detail,
             batch_id, context.actor_id, now),
        )
        if row:
            connection.execute(
                "UPDATE leisure_obs SET current_version=?,value=?,unit=?,batch_id=?,recorded_by=?,recorded_at=? "
                "WHERE city_id=? AND period_id=? AND metric=?",
                (version, value, unit, batch_id, context.actor_id, now, city_id, period_id, metric),
            )
        else:
            connection.execute(
                "INSERT INTO leisure_obs(city_id,period_id,metric,current_version,value,unit,batch_id,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (city_id, period_id, metric, version, value, unit, batch_id, context.actor_id, now),
            )
        return {"metric": metric, "version": version, "value": value, "unit": unit,
                "canonical_unit": METRIC_UNITS[metric], "needs_conversion": unit != METRIC_UNITS[metric]}

    # ---- 调整：换算与权重，需另一成员确认 -------------------------------

    def propose_adjustment(self, context: AccessContext, *, period_id: str, kind: str, target: str = "",
                           from_value: str = "", to_value: str = "", reason: str, factor: float | None = None,
                           city_id: str | None = None,
                           weights: Mapping[str, float] | None = None) -> dict:
        context.require("write:leisure")
        if kind not in ("conversion", "weight"):
            raise ValidationError("调整类型必须是 conversion 或 weight")
        if not reason or not reason.strip():
            raise ValidationError("调整必须说明原因")
        if kind == "conversion":
            if city_id is None:
                raise ValidationError("换算调整必须针对具体城市")
            if target not in METRIC_UNITS:
                raise ValidationError(f"未知指标: {target}")
            if not from_value or not to_value:
                raise ValidationError("换算必须写明源单位与目标单位")
            if not isinstance(factor, (int, float)) or not math.isfinite(float(factor)) or float(factor) <= 0:
                raise ValidationError("换算系数必须为正数")
            factor = float(factor)
        else:
            # 权重调整一次给出整套维度权重，保证确认后总和恒为 1
            if weights is None or set(weights) != set(DIMENSIONS):
                raise ValidationError("权重调整必须给出全部维度的权重")
            normalized = {}
            for dimension in DIMENSIONS:
                value = float(weights[dimension])
                if not math.isfinite(value) or not 0.0 < value <= 1.0:
                    raise ValidationError("权重必须在 0 到 1 之间")
                normalized[dimension] = value
            if abs(sum(normalized.values()) - 1.0) > 1e-6:
                raise ValidationError("各维度权重之和必须为 1")
            target = "dimensions"
            from_value = "current"
            to_value = canonical_json(normalized)
            factor = None
        with self.database.transaction() as connection:
            self._period_row(connection, period_id)
            if city_id is not None:
                self._city_row(connection, city_id)
                self._require_city(connection, context, city_id)
            adjustment_id = new_id("adjustment")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO leisure_adjustments(adjustment_id,period_id,city_id,kind,target,from_value,to_value,factor,reason,state,proposed_by,proposed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?, 'proposed',?,?)",
                (adjustment_id, period_id, city_id, kind, target, str(from_value), str(to_value),
                 factor, reason.strip(), context.actor_id, now),
            )
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.adjustment.propose",
                              entity_type="leisure_adjustments", entity_id=adjustment_id, version=1,
                              detail={"kind": kind, "target": target, "city_id": city_id})
        return self.get_adjustment(adjustment_id)

    def confirm_adjustment(self, context: AccessContext, adjustment_id: str, *, decision: str = "confirmed") -> dict:
        """由另一名成员确认；确认前调整不影响任何计算结果。"""
        context.require("confirm:leisure")
        if decision not in ("confirmed", "rejected"):
            raise ValidationError("决定必须是 confirmed 或 rejected")
        with self.database.transaction() as connection:
            row = connection.execute("SELECT * FROM leisure_adjustments WHERE adjustment_id=?", (adjustment_id,)).fetchone()
            if not row:
                raise NotFoundError("调整不存在")
            if row["state"] != "proposed":
                raise ConflictError(f"调整当前状态为 {row['state']}")
            assert_distinct(row["proposed_by"], context.actor_id)
            now = self.clock.now()
            if decision == "confirmed" and row["kind"] == "weight":
                self._apply_weight(connection, context, row, now)
            connection.execute(
                "UPDATE leisure_adjustments SET state=?,confirmed_by=?,confirmed_at=? WHERE adjustment_id=?",
                (decision, context.actor_id, now, adjustment_id),
            )
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.adjustment.confirm",
                              entity_type="leisure_adjustments", entity_id=adjustment_id, version=1,
                              detail={"decision": decision})
        return self.get_adjustment(adjustment_id)

    def _apply_weight(self, connection, context: AccessContext, row: sqlite3.Row, now: str) -> None:
        period_id = row["period_id"]
        try:
            new_weights = json.loads(row["to_value"])
        except json.JSONDecodeError as exc:
            raise ValidationError("权重调整内容格式错误") from exc
        if set(new_weights) != set(DIMENSIONS):
            raise ValidationError("权重调整必须覆盖全部维度")
        for dimension, new_weight in new_weights.items():
            new_weight = float(new_weight)
            current = connection.execute(
                "SELECT version,weight FROM leisure_weights WHERE period_id=? AND dimension=?",
                (period_id, dimension)).fetchone()
            if current is None:
                raise ValidationError(f"维度 {dimension} 的权重尚未初始化")
            if abs(float(current["weight"]) - new_weight) < 1e-12:
                continue
            connection.execute(
                "UPDATE leisure_weights SET weight=?,version=?,adjustment_id=?,changed_by=?,changed_at=? WHERE period_id=? AND dimension=?",
                (new_weight, current["version"] + 1, row["adjustment_id"], context.actor_id, now,
                 period_id, dimension),
            )

    def get_adjustment(self, adjustment_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM leisure_adjustments WHERE adjustment_id=?", (adjustment_id,)).fetchone()
            if not row:
                raise NotFoundError("调整不存在")
            return dict(row)

    def list_adjustments(self, context: AccessContext, *, period_id: str, state: str | None = None) -> list[dict]:
        context.require("read:leisure")
        sql = "SELECT * FROM leisure_adjustments WHERE period_id=?"
        params: list[object] = [period_id]
        if state:
            sql += " AND state=?"; params.append(state)
        sql += " ORDER BY proposed_at,adjustment_id"
        with self.database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
            return [dict(r) for r in rows if r["city_id"] is None or self._can_city(connection, context, r["city_id"])]

    # ---- 计算：固定单位、覆盖期、分母版本 -------------------------------

    def compute(self, context: AccessContext, *, period_id: str, city_id: str, reason: str,
                dimensions: Iterable[str] | None = None, denominator_metric: str = DEFAULT_DENOMINATOR,
                trigger: str = "manual") -> dict:
        context.require("write:leisure")
        if not reason or not reason.strip():
            raise ValidationError("计算必须说明原因")
        if denominator_metric not in METRIC_UNITS or not denominator_metric.startswith("pop_"):
            raise ValidationError("分母必须是人口口径指标")
        scope = tuple(dimensions) if dimensions is not None else DIMENSIONS
        unknown = [d for d in scope if d not in DIMENSIONS]
        if unknown:
            raise ValidationError("未知维度: " + ", ".join(unknown))
        if len(set(scope)) != len(scope):
            raise ValidationError("重算维度不能重复")
        with self.database.transaction() as connection:
            period = self._period_row(connection, period_id)
            self._city_row(connection, city_id)
            self._require_city(connection, context, city_id)
            parent = connection.execute(
                "SELECT * FROM leisure_computations WHERE period_id=? AND city_id=? AND status IN ('current','frozen') "
                "ORDER BY CASE status WHEN 'current' THEN 0 ELSE 1 END, rowid DESC LIMIT 1",
                (period_id, city_id)).fetchone()
            if scope != DIMENSIONS and parent is None:
                raise ConflictError("局部重算需要一份既有计算作为继承基础")
            basis, scores = self._build_scores(connection, period, city_id, scope, denominator_metric, parent)
            total = round(sum(basis["weights"][d]["weight"] * scores[d]["score"] for d in DIMENSIONS), 6)
            computation_id = new_id("computation")
            now = self.clock.now()
            connection.execute(
                "INSERT INTO leisure_computations(computation_id,period_id,city_id,scope_dimensions,trigger,reason,parent_computation_id,status,basis_json,scores_json,total,inputs_digest,computed_by,computed_at) "
                "VALUES(?,?,?,?,?,?,?, 'current',?,?,?,?,?,?)",
                (computation_id, period_id, city_id, ",".join(scope), trigger, reason.strip(),
                 parent["computation_id"] if parent else None, canonical_json(basis), canonical_json(scores),
                 total, digest_json({"basis": basis, "scores": scores}), context.actor_id, now),
            )
            if parent is not None and parent["status"] == "current":
                connection.execute(
                    "UPDATE leisure_computations SET status='superseded' WHERE computation_id=?",
                    (parent["computation_id"],))
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.compute",
                              entity_type="leisure_computations", entity_id=computation_id, version=1,
                              detail={"period_id": period_id, "city_id": city_id, "scope": scope, "trigger": trigger})
            return self._computation_dict(connection.execute(
                "SELECT * FROM leisure_computations WHERE computation_id=?", (computation_id,)).fetchone())

    def recompute_affected(self, context: AccessContext, *, period_id: str, city_id: str, reason: str,
                           dimensions: Iterable[str]) -> list[dict]:
        """迟到修订入口：只重算相关维度和城市，未受影响维度沿用上一版。"""
        return [self.compute(context, period_id=period_id, city_id=city_id, reason=reason,
                             dimensions=tuple(dict.fromkeys(dimensions)), trigger="late_revision")]

    def recompute_all_cities(self, context: AccessContext, *, period_id: str, reason: str,
                             dimensions: Iterable[str] | None = None, trigger: str = "weight_change") -> list[dict]:
        """权重调整后按同一口径重算所有城市（每个城市仍是独立、可追溯的一次计算）。"""
        scope = tuple(dimensions) if dimensions is not None else DIMENSIONS
        results = []
        with self.database.connect() as connection:
            city_ids = [r["city_id"] for r in connection.execute(
                "SELECT city_id FROM leisure_cities ORDER BY code").fetchall()]
        for city_id in city_ids:
            results.append(self.compute(context, period_id=period_id, city_id=city_id, reason=reason,
                                        dimensions=scope, trigger=trigger))
        return results

    def _build_scores(self, connection, period: sqlite3.Row, city_id: str, scope: tuple[str, ...],
                      denominator_metric: str, parent: sqlite3.Row | None):
        period_id = period["period_id"]
        obs_rows = {r["metric"]: r for r in connection.execute(
            "SELECT * FROM leisure_obs WHERE period_id=? AND city_id=?", (period_id, city_id)).fetchall()}
        denominator = obs_rows.get(denominator_metric)
        if denominator is None:
            raise ValidationError(f"缺少分母指标 {denominator_metric}，无法计算")
        basis: dict = {
            "period_id": period_id,
            "city_id": city_id,
            "coverage_start": period["coverage_start"],
            "coverage_end": period["coverage_end"],
            "denominator": {"metric": denominator_metric, "version": denominator["current_version"],
                            "value": denominator["value"], "unit": denominator["unit"]},
            "metrics": {},
            "weights": {},
            "inherited": {},
        }
        needed = {denominator_metric}
        for dimension in scope:
            needed.update(DIMENSION_METRICS[dimension])
        conversions = self._confirmed_conversions(connection, period_id, city_id)
        converted: dict[str, float] = {}
        for metric in needed:
            row = obs_rows.get(metric)
            if row is None:
                raise ValidationError(f"缺少指标 {metric}，无法计算")
            value = row["value"]
            entry = {"version": row["current_version"], "raw_value": value, "submitted_unit": row["unit"],
                     "canonical_unit": METRIC_UNITS[metric], "source_ref": None}
            version_row = connection.execute(
                "SELECT source_ref,source_detail,batch_id,recorded_by,recorded_at FROM leisure_obs_versions "
                "WHERE city_id=? AND period_id=? AND metric=? AND version=?",
                (city_id, period_id, metric, row["current_version"])).fetchone()
            entry["source_ref"] = version_row["source_ref"]
            entry["source_detail"] = version_row["source_detail"]
            entry["batch_id"] = version_row["batch_id"]
            entry["recorded_by"] = version_row["recorded_by"]
            entry["recorded_at"] = version_row["recorded_at"]
            if row["unit"] != METRIC_UNITS[metric]:
                adjustment = conversions.get((metric, row["unit"]))
                if adjustment is None:
                    raise ValidationError(f"指标 {metric} 的单位 {row['unit']} 尚未经确认的换算调整")
                value = round(value * adjustment["factor"], 6)
                entry["converted_value"] = value
                entry["conversion_adjustment"] = adjustment["adjustment_id"]
            else:
                entry["converted_value"] = value
            converted[metric] = value
            basis["metrics"][metric] = entry
        for weight_row in connection.execute("SELECT * FROM leisure_weights WHERE period_id=?", (period_id,)):
            basis["weights"][weight_row["dimension"]] = {
                "weight": weight_row["weight"], "version": weight_row["version"],
                "adjustment_id": weight_row["adjustment_id"]}
        if abs(sum(w["weight"] for w in basis["weights"].values()) - 1.0) > 1e-6:
            raise ValidationError("指标权重之和必须为 1")
        scores: dict = {}
        if parent is not None:
            parent_scores = json.loads(parent["scores_json"])
            parent_basis = json.loads(parent["basis_json"])
        else:
            parent_scores = parent_basis = None
        denominator_value = converted[denominator_metric]
        for dimension in DIMENSIONS:
            if dimension not in scope:
                scores[dimension] = parent_scores[dimension]
                basis["inherited"][dimension] = parent["computation_id"]
                continue
            scores[dimension] = self._score_dimension(dimension, converted, denominator_value)
        # 分母若不是常住人口基准，也固定记录其来源版本
        if parent_basis is not None and denominator_metric == parent_basis["denominator"]["metric"]:
            basis["denominator"]["previous_version"] = parent_basis["denominator"]["version"]
        return basis, scores

    @staticmethod
    def _score_dimension(dimension: str, v: dict[str, float], denominator_value: float) -> dict:
        if dimension == "resources":
            cult = _component(v["cultural_facilities"] / denominator_value * 10_000, CALIBRATION["cultural_per_10k_full"])
            sport = _component(v["sports_sites"] / denominator_value * 10_000, CALIBRATION["sports_per_10k_full"])
            green = _component(v["park_green_space"] * 10_000 / denominator_value, CALIBRATION["green_m2_per_person_full"])
            return {"score": round((cult + sport + green) / 3, 6),
                    "components": {"cultural_per_10k": cult, "sports_per_10k": sport, "green_m2_per_person": green}}
        if dimension == "coverage":
            coverage = _component(v["service_coverage"], 100.0)
            return {"score": coverage, "components": {"service_coverage": coverage}}
        if dimension == "density":
            area = v["land_area"]
            facilities = _component((v["cultural_facilities"] + v["sports_sites"]) / area,
                                    CALIBRATION["facilities_per_km2_full"])
            green = _component(v["park_green_space"] / area, CALIBRATION["green_ha_per_km2_full"])
            return {"score": round((facilities + green) / 2, 6),
                    "components": {"facilities_per_km2": facilities, "green_ha_per_km2": green}}
        raise ValidationError(f"未知维度: {dimension}")

    def latest_computation(self, context: AccessContext, period_id: str, city_id: str) -> dict:
        context.require("read:leisure")
        with self.database.connect() as connection:
            self._period_row(connection, period_id)
            self._require_city(connection, context, city_id)
            row = connection.execute(
                "SELECT * FROM leisure_computations WHERE period_id=? AND city_id=? "
                "ORDER BY CASE status WHEN 'current' THEN 0 WHEN 'frozen' THEN 1 ELSE 2 END, rowid DESC LIMIT 1",
                (period_id, city_id)).fetchone()
            if not row:
                raise NotFoundError("该城市在本统计期尚无计算结果")
            return self._computation_dict(row)

    def list_computations(self, context: AccessContext, *, period_id: str, city_id: str) -> list[dict]:
        context.require("read:leisure")
        with self.database.connect() as connection:
            self._require_city(connection, context, city_id)
            rows = connection.execute(
                "SELECT * FROM leisure_computations WHERE period_id=? AND city_id=? ORDER BY rowid",
                (period_id, city_id)).fetchall()
            return [self._computation_dict(r) for r in rows]

    @staticmethod
    def _computation_dict(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["basis"] = json.loads(result.pop("basis_json"))
        result["scores"] = json.loads(result.pop("scores_json"))
        result["scope_dimensions"] = result["scope_dimensions"].split(",") if result["scope_dimensions"] else []
        return result

    # ---- 发布：榜单冻结与勘误 -------------------------------------------

    def publish_ranking(self, context: AccessContext, *, period_id: str, title: str) -> dict:
        """正式发布年度榜单：快照各城市当前计算，随后这些计算被冻结、永不改变。"""
        context.require("publish:leisure")
        if not title or not title.strip():
            raise ValidationError("榜单标题不能为空")
        now = self.clock.now()
        with self.database.transaction() as connection:
            period = self._period_row(connection, period_id)
            rows = connection.execute(
                "SELECT c.* FROM leisure_computations c JOIN leisure_cities city ON city.city_id=c.city_id "
                "WHERE c.period_id=? AND c.status='current' ORDER BY city.code",
                (period_id,)).fetchall()
            if not rows:
                raise ValidationError("没有可发布的计算结果")
            entries = []
            for row in rows:
                entries.append({"city_id": row["city_id"], "total": row["total"],
                                "computation_id": row["computation_id"],
                                "scores": json.loads(row["scores_json"])})
            entries.sort(key=lambda e: (-e["total"], e["city_id"]))
            for rank, entry in enumerate(entries, 1):
                entry["rank"] = rank
                connection.execute(
                    "UPDATE leisure_computations SET status='frozen' WHERE computation_id=?",
                    (entry["computation_id"],))
            ranking_id = new_id("ranking")
            digest = digest_json({"period_id": period_id, "entries": entries})
            connection.execute(
                "INSERT INTO leisure_rankings(ranking_id,period_id,title,entries_json,digest,published_by,published_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (ranking_id, period_id, title.strip(), canonical_json(entries), digest, context.actor_id, now),
            )
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.ranking.publish",
                              entity_type="leisure_rankings", entity_id=ranking_id, version=1,
                              detail={"period_id": period_id, "cities": len(entries)})
        return self.get_ranking(context, ranking_id)

    def get_ranking(self, context: AccessContext, ranking_id: str) -> dict:
        context.require("read:leisure")
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM leisure_rankings WHERE ranking_id=?", (ranking_id,)).fetchone()
            if not row:
                raise NotFoundError("榜单不存在")
            entries = [e for e in json.loads(row["entries_json"])
                       if self._can_city(connection, context, e["city_id"])]
            errata = [dict(r) for r in connection.execute(
                "SELECT * FROM leisure_errata WHERE ranking_id=? ORDER BY rowid",
                (ranking_id,)).fetchall()
                       if self._can_city(connection, context, r["city_id"])]
            result = dict(row)
            result["entries"] = entries
            result["errata"] = errata
            result["immutable"] = True
            return result

    def issue_erratum(self, context: AccessContext, *, ranking_id: str, city_id: str, dimension: str,
                      reason: str) -> dict:
        """榜单原貌不变；以关联到榜单、前后计算的勘误说明迟到修订带来的变化。"""
        context.require("publish:leisure")
        if dimension not in DIMENSIONS:
            raise ValidationError(f"未知维度: {dimension}")
        if not reason or not reason.strip():
            raise ValidationError("勘误必须说明原因")
        with self.database.transaction() as connection:
            ranking = connection.execute("SELECT * FROM leisure_rankings WHERE ranking_id=?", (ranking_id,)).fetchone()
            if not ranking:
                raise NotFoundError("榜单不存在")
            self._require_city(connection, context, city_id)
            before_entry = next((e for e in json.loads(ranking["entries_json"]) if e["city_id"] == city_id), None)
            if before_entry is None:
                raise NotFoundError("该城市不在榜单中")
            before_comp = connection.execute(
                "SELECT * FROM leisure_computations WHERE computation_id=?",
                (before_entry["computation_id"],)).fetchone()
            after_comp = connection.execute(
                "SELECT * FROM leisure_computations WHERE period_id=? AND city_id=? AND status='current' "
                "ORDER BY rowid DESC LIMIT 1",
                (ranking["period_id"], city_id)).fetchone()
            if after_comp is None:
                raise ConflictError("该城市榜单之后尚无新的计算结果，无法出勘误")
            before_score = json.loads(before_comp["scores_json"])[dimension]["score"]
            after_score = json.loads(after_comp["scores_json"])[dimension]["score"]
            if before_score == after_score and before_comp["total"] == after_comp["total"]:
                raise ConflictError("该维度与总分均无变化")
            erratum_id = new_id("erratum")
            now = self.clock.now()
            summary = f"{city_id} {dimension} 维度得分 {before_score} -> {after_score}；总分 {before_comp['total']} -> {after_comp['total']}"
            connection.execute(
                "INSERT INTO leisure_errata(erratum_id,ranking_id,period_id,city_id,dimension,before_computation_id,after_computation_id,before_value,after_value,change_summary,reason,issued_by,issued_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (erratum_id, ranking_id, ranking["period_id"], city_id, dimension,
                 before_comp["computation_id"], after_comp["computation_id"], before_score, after_score,
                 summary, reason.strip(), context.actor_id, now),
            )
            self.audit.append(connection, actor_id=context.actor_id, action="leisure.erratum.issue",
                              entity_type="leisure_errata", entity_id=erratum_id, version=1,
                              detail={"ranking_id": ranking_id, "city_id": city_id, "dimension": dimension})
        with self.database.connect() as connection:
            return dict(connection.execute("SELECT * FROM leisure_errata WHERE erratum_id=?", (erratum_id,)).fetchone())

    # ---- 追溯：从排名到指标值、人口版本、来源、理由与责任人 ---------------

    def trace_ranking(self, context: AccessContext, ranking_id: str) -> list[dict]:
        """返回榜单每个名次当时的完整谱系。"""
        context.require("read:leisure")
        with self.database.connect() as connection:
            ranking = connection.execute("SELECT * FROM leisure_rankings WHERE ranking_id=?", (ranking_id,)).fetchone()
            if not ranking:
                raise NotFoundError("榜单不存在")
            trace = []
            for entry in json.loads(ranking["entries_json"]):
                if not self._can_city(connection, context, entry["city_id"]):
                    continue
                comp = connection.execute(
                    "SELECT * FROM leisure_computations WHERE computation_id=?",
                    (entry["computation_id"],)).fetchone()
                basis = json.loads(comp["basis_json"])
                adjustments_used = []
                seen_adjustments: set[str] = set()
                for metric, info in basis["metrics"].items():
                    if info.get("conversion_adjustment") and info["conversion_adjustment"] not in seen_adjustments:
                        seen_adjustments.add(info["conversion_adjustment"])
                        a = connection.execute("SELECT * FROM leisure_adjustments WHERE adjustment_id=?",
                                               (info["conversion_adjustment"],)).fetchone()
                        if a:
                            adjustments_used.append({"type": "conversion", "metric": metric, **self._responsibility(a)})
                for dimension, w in basis["weights"].items():
                    if w["adjustment_id"] and w["adjustment_id"] not in seen_adjustments:
                        seen_adjustments.add(w["adjustment_id"])
                        a = connection.execute("SELECT * FROM leisure_adjustments WHERE adjustment_id=?",
                                               (w["adjustment_id"],)).fetchone()
                        if a:
                            adjustments_used.append({"type": "weight", "dimension": dimension, **self._responsibility(a)})
                errata = [dict(r) for r in connection.execute(
                    "SELECT * FROM leisure_errata WHERE ranking_id=? AND city_id=? ORDER BY rowid",
                    (ranking_id, entry["city_id"],)).fetchall()]
                trace.append({
                    "rank": entry["rank"],
                    "city_id": entry["city_id"],
                    "total": entry["total"],
                    "scores": entry["scores"],
                    "computation_id": comp["computation_id"],
                    "computed_by": comp["computed_by"],
                    "computed_at": comp["computed_at"],
                    "trigger": comp["trigger"],
                    "reason": comp["reason"],
                    "coverage": {"start": basis["coverage_start"], "end": basis["coverage_end"]},
                    "denominator_version": basis["denominator"],
                    "metric_values": basis["metrics"],
                    "weights": basis["weights"],
                    "inherited_dimensions": basis.get("inherited", {}),
                    "adjustments": adjustments_used,
                    "errata": errata,
                })
            return trace

    @staticmethod
    def _responsibility(row: sqlite3.Row) -> dict:
        return {"adjustment_id": row["adjustment_id"], "kind": row["kind"], "target": row["target"],
                "from_value": row["from_value"], "to_value": row["to_value"], "factor": row["factor"],
                "reason": row["reason"], "state": row["state"],
                "proposed_by": row["proposed_by"], "proposed_at": row["proposed_at"],
                "confirmed_by": row["confirmed_by"], "confirmed_at": row["confirmed_at"]}

    # ---- 数据催收 -------------------------------------------------------

    def schedule_collection(self, context: AccessContext, period_id: str, *, run_at: str | None = None) -> list[str]:
        """对缺失指标安排催收任务；任务持久化，进程重启后仍可领取续作。"""
        context.require("write:leisure")
        run_at = canonical_instant(run_at or self.clock.now())
        job_ids = []
        with self.database.transaction() as connection:
            period = self._period_row(connection, period_id)
            cities = connection.execute("SELECT city_id FROM leisure_cities ORDER BY code").fetchall()
            required = COLLECTION_METRICS
            for city in cities:
                present = {r["metric"] for r in connection.execute(
                    "SELECT metric FROM leisure_obs WHERE period_id=? AND city_id=?",
                    (period_id, city["city_id"])).fetchall()}
                for metric in required:
                    if metric in present:
                        continue
                    job_id = new_id("job")
                    payload = {"period_id": period_id, "city_id": city["city_id"],
                               "metric": metric, "year": period["year"]}
                    connection.execute(
                        "INSERT INTO scheduled_jobs(job_id,job_type,subject_id,run_at,payload_json,status) "
                        "VALUES(?,?,?,?,?, 'waiting')",
                        (job_id, "leisure_collection", f"{period_id}:{city['city_id']}", run_at,
                         canonical_json(payload)),
                    )
                    job_ids.append(job_id)
        return job_ids

    # ---- 内部校验 -------------------------------------------------------

    def _confirmed_conversions(self, connection, period_id: str, city_id: str) -> dict[tuple[str, str], sqlite3.Row]:
        result = {}
        rows = connection.execute(
            "SELECT * FROM leisure_adjustments WHERE period_id=? AND city_id=? AND kind='conversion' AND state='confirmed'",
            (period_id, city_id)).fetchall()
        for row in rows:
            result[(row["target"], row["from_value"])] = row
        return result

    def _can_city(self, connection: sqlite3.Connection, context: AccessContext, city_id: str) -> bool:
        if context.allows("read:leisure") and context.org_id == "org:secretariat":
            return True
        if context.allows("*"):
            return True
        row = connection.execute(
            "SELECT 1 FROM leisure_city_grants WHERE org_id=? AND city_id=?",
            (context.org_id, city_id)).fetchone()
        return row is not None

    def _require_city(self, connection: sqlite3.Connection, context: AccessContext, city_id: str) -> None:
        if not self._can_city(connection, context, city_id):
            raise PermissionDenied(f"未获授权访问城市 {city_id}")

    @staticmethod
    def _city_row(connection: sqlite3.Connection, city_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM leisure_cities WHERE city_id=?", (city_id,)).fetchone()
        if not row:
            raise NotFoundError(f"城市 {city_id} 不存在")
        return row

    @staticmethod
    def _period_row(connection: sqlite3.Connection, period_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM leisure_periods WHERE period_id=?", (period_id,)).fetchone()
        if not row:
            raise NotFoundError(f"统计期 {period_id} 不存在")
        return row
