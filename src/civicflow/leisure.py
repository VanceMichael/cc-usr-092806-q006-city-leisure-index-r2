"""城市休闲化指数复核领域服务。

按城市与统计期固化人口基准（分母版本）、休闲资源/服务覆盖/空间密度
三类观测、样本出处与指标权重；报送批次幂等，换算与权重调整双人确认，
迟到修订只重算相关城市与维度，正式榜单冻结原貌并以勘误版本关联说明。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .audit import AuditLog
from .database import Database
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .identifiers import new_id, require_safe
from .jsonutil import canonical_json, digest_json
from .security import AccessContext, assert_distinct
from .timeutil import Clock, canonical_instant


SCOPES = ("municipal", "urban", "resident")
SCOPE_LABELS = {"municipal": "市域", "urban": "城区", "resident": "常住人口"}
DIMENSIONS = ("resources", "coverage", "density")
# 演示用确定性指标目录：标准单位、首选人口口径、是否人均类、默认权重。
METRICS = {
    "resources": {
        "public_facilities": {"name": "公共设施数", "unit": "个", "basis": "resident", "per_capita": True, "default_weight": 1.0},
        "green_space_area": {"name": "绿地面积", "unit": "万平方米", "basis": "municipal", "per_capita": False, "default_weight": 1.0},
    },
    "coverage": {
        "service_coverage_rate": {"name": "服务覆盖率", "unit": "%", "basis": "urban", "per_capita": False, "default_weight": 1.0},
        "facilities_per_10k": {"name": "每万人设施数", "unit": "个/万人", "basis": "resident", "per_capita": True, "default_weight": 1.0},
    },
    "density": {
        "facility_density": {"name": "设施密度", "unit": "个/平方公里", "basis": "municipal", "per_capita": False, "default_weight": 1.0},
    },
}
DIMENSION_WEIGHTS = {"resources": 1.0 / 3.0, "coverage": 1.0 / 3.0, "density": 1.0 / 3.0}
PERIOD_STATUSES = ("collecting", "review", "closed")
RECORD_FIELDS = ("dimension", "metric_code", "raw_value", "unit", "basis", "coverage_start", "coverage_end", "source_ref")


def _round6(value: float) -> float:
    return round(float(value) + 0.0, 6)


@dataclass(frozen=True)
class LeisureReviewService:
    """休闲化指数报送、复核、重算、发布与勘误。"""

    database: Database
    clock: Clock

    # ---- 基础档案与城市授权 -------------------------------------------------

    def register_city(self, context: AccessContext, city_code: str, name: str) -> dict:
        context.require("write:leisure")
        require_safe(city_code, "城市代码")
        name = self._require_text(name, "城市名称")
        with self.database.transaction() as conn:
            if conn.execute("SELECT 1 FROM lr_cities WHERE city_code=?", (city_code,)).fetchone():
                raise ConflictError("城市已登记")
            conn.execute("INSERT INTO lr_cities(city_code,name,created_at,created_by) VALUES(?,?,?,?)", (city_code, name, self.clock.now(), context.actor_id))
            self._audit(conn, context.actor_id, "lr.register_city", city_code, 1, {"name": name})
        return {"city_code": city_code, "name": name}

    def grant_city(self, context: AccessContext, organization_id: str, city_code: str) -> dict:
        context.require("write:leisure")
        require_safe(organization_id, "机构标识")
        with self.database.transaction() as conn:
            self._require_city(conn, city_code)
            row = conn.execute("SELECT grant_id,revoked_at FROM lr_city_grants WHERE organization_id=? AND city_code=?", (organization_id, city_code)).fetchone()
            if row and not row["revoked_at"]:
                return {"grant_id": row["grant_id"], "status": "active", "replayed": True}
            if row:
                raise ConflictError("授权曾被撤销，如需恢复请重新备案")
            grant_id = new_id("grant")
            conn.execute("INSERT INTO lr_city_grants(grant_id,organization_id,city_code,created_at,created_by) VALUES(?,?,?,?,?)", (grant_id, organization_id, city_code, self.clock.now(), context.actor_id))
            self._audit(conn, context.actor_id, "lr.grant_city", city_code, 1, {"organization_id": organization_id})
            return {"grant_id": grant_id, "status": "active", "replayed": False}

    def revoke_city(self, context: AccessContext, organization_id: str, city_code: str) -> None:
        context.require("write:leisure")
        with self.database.transaction() as conn:
            changed = conn.execute("UPDATE lr_city_grants SET revoked_at=? WHERE organization_id=? AND city_code=? AND revoked_at IS NULL", (self.clock.now(), organization_id, city_code)).rowcount
            if changed != 1:
                raise NotFoundError("有效授权不存在")
            self._audit(conn, context.actor_id, "lr.revoke_city", city_code, 1, {"organization_id": organization_id})

    def partner_context(self, actor_id: str, organization_id: str) -> AccessContext:
        """构造合作机构访问上下文：只能读到获授权城市。"""
        require_safe(actor_id, "操作者")
        with self.database.connect() as conn:
            rows = conn.execute("SELECT city_code FROM lr_city_grants WHERE organization_id=? AND revoked_at IS NULL ORDER BY city_code", (organization_id,)).fetchall()
        return AccessContext(actor_id, permissions=frozenset({"read:leisure"}), scopes=frozenset(row["city_code"] for row in rows))

    # ---- 统计期：催收 → 复核 → 关账 ----------------------------------------

    def open_period(self, context: AccessContext, period: str, *, collect_due_at: str | None = None) -> dict:
        context.require("write:leisure")
        require_safe(period, "统计期")
        with self.database.transaction() as conn:
            if conn.execute("SELECT 1 FROM lr_periods WHERE period=?", (period,)).fetchone():
                raise ConflictError("统计期已经开启")
            conn.execute("INSERT INTO lr_periods(period,status,opened_at) VALUES(?, 'collecting', ?)", (period, self.clock.now()))
            self._audit(conn, context.actor_id, "lr.open_period", period, 1, {"collect_due_at": collect_due_at})
        reminder_job = None
        if collect_due_at:
            # 催收提醒走可恢复任务队列（独立事务），进程重启后仍会到期执行。
            from .jobs import JobQueue
            reminder_job = JobQueue(self.database, self.clock).schedule(job_type="lr:collection-reminder", subject_id=period, run_at=collect_due_at, payload={"period": period})
        return {"period": period, "status": "collecting", "reminder_job": reminder_job}

    def begin_review(self, context: AccessContext, period: str) -> dict:
        context.require("write:leisure")
        with self.database.transaction() as conn:
            self._transition_period(conn, context.actor_id, period, "review", expect="collecting")
            return self._period_dict(conn, period)

    def close_period(self, context: AccessContext, period: str) -> dict:
        context.require("close:leisure")
        with self.database.transaction() as conn:
            pending = conn.execute("SELECT 1 FROM lr_observations WHERE period=? AND status='pending_conversion' LIMIT 1", (period,)).fetchone()
            if pending:
                raise ConflictError("仍有换算提案未完成确认，不能关账")
            pending = conn.execute("SELECT 1 FROM lr_proposals WHERE period=? AND status='proposed' LIMIT 1", (period,)).fetchone()
            if pending:
                raise ConflictError("仍有调整提案等待另一名成员确认，不能关账")
            pending = conn.execute("SELECT 1 FROM lr_recalc_jobs WHERE period=? AND status='pending' LIMIT 1", (period,)).fetchone()
            if pending:
                raise ConflictError("仍有报送触发的重算未完成，复核完成后才能关账")
            self._transition_period(conn, context.actor_id, period, "closed", expect="review")
            return self._period_dict(conn, period)

    def get_period(self, period: str) -> dict:
        with self.database.connect() as conn:
            return self._period_dict(conn, period)

    # ---- 人口基准（分母版本） -----------------------------------------------

    def put_baseline(self, context: AccessContext, period: str, city_code: str, scope: str, *, population: float, unit: str, coverage_start: str, coverage_end: str, reason: str = "") -> dict:
        context.require("write:leisure")
        self._validate_scope(scope)
        unit = self._require_text(unit, "单位")
        population = self._require_number(population, "人口数")
        if population <= 0:
            raise ValidationError("人口数必须大于零")
        coverage_start = canonical_instant(coverage_start)
        coverage_end = canonical_instant(coverage_end)
        if coverage_end < coverage_start:
            raise ValidationError("覆盖期结束时间不能早于开始时间")
        with self.database.transaction() as conn:
            self._require_period(conn, period)
            self._require_city(conn, city_code)
            closed = conn.execute("SELECT status FROM lr_periods WHERE period=?", (period,)).fetchone()["status"] == "closed"
            if closed and not reason.strip():
                raise ValidationError("关账后补录人口基准必须说明原因")
            row = conn.execute("SELECT COALESCE(MAX(version_seq),0) AS v FROM lr_baselines WHERE period=? AND city_code=? AND scope=?", (period, city_code, scope)).fetchone()
            version_seq = int(row["v"]) + 1
            baseline_id = f"base:{period}:{city_code}:{scope}:v{version_seq}"
            conn.execute("UPDATE lr_baselines SET status='superseded' WHERE period=? AND city_code=? AND scope=? AND status='active'", (period, city_code, scope))
            conn.execute(
                "INSERT INTO lr_baselines(baseline_id,period,city_code,scope,version_seq,population,unit,coverage_start,coverage_end,reason,status,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?, 'active', ?,?)",
                (baseline_id, period, city_code, scope, version_seq, population, unit, coverage_start, coverage_end, reason.strip(), self.clock.now(), context.actor_id),
            )
            # 现行观测改用新分母版本；历史结果的分母快照不受影响（已固化在结果谱系里）。
            conn.execute("UPDATE lr_observations SET baseline_id=? WHERE period=? AND city_code=? AND basis=? AND status IN ('active','pending_conversion')", (baseline_id, period, city_code, scope))
            self._audit(conn, context.actor_id, "lr.put_baseline", baseline_id, version_seq, {"scope": scope, "population": population, "late": closed})
            # 分母换版只重算真正使用该口径观测的维度。
            dim_rows = conn.execute("SELECT DISTINCT dimension FROM lr_observations WHERE period=? AND city_code=? AND basis=? AND status IN ('active','pending_conversion')", (period, city_code, scope)).fetchall()
            affected = sorted(row["dimension"] for row in dim_rows)
            job_id = self._enqueue_recalc(conn, period, city_code, affected, reason.strip() or "人口基准更新", context.actor_id) if affected else None
            return {"baseline_id": baseline_id, "version_seq": version_seq, "scope": scope, "status": "active", "recalc_job": job_id}

    # ---- 报送批次（幂等、冲突暂存、迟到修订） --------------------------------

    def submit_batch(self, context: AccessContext, batch_no: str, period: str, city_code: str, records: list[dict], *, reason: str = "") -> dict:
        context.require("write:leisure")
        require_safe(batch_no, "批次编号")
        if not isinstance(records, list) or not records:
            raise ValidationError("报送记录不能为空")
        normalized = [self._validate_record(item) for item in records]
        digest = digest_json({"period": period, "city_code": city_code, "records": normalized})
        with self.database.connect() as conn:
            self._require_city(conn, city_code)
            period_row = conn.execute("SELECT status FROM lr_periods WHERE period=?", (period,)).fetchone()
            if not period_row:
                raise NotFoundError("统计期不存在")
            late = period_row["status"] == "closed"
            existing = conn.execute("SELECT * FROM lr_batches WHERE batch_no=?", (batch_no,)).fetchone()
        if existing:
            # 去重与冲突留痕放在独立事务里：外层调用一旦抛错回滚也不会抹掉记录。
            if existing["payload_digest"] != digest:
                with self.database.transaction() as conn:
                    conn.execute("INSERT INTO lr_batch_conflicts(batch_no,period,city_code,existing_digest,incoming_digest,created_at) VALUES(?,?,?,?,?,?)", (batch_no, existing["period"], existing["city_code"], existing["payload_digest"], digest, self.clock.now()))
                    self._audit(conn, context.actor_id, "lr.batch_conflict", batch_no, 0, {"period": period, "city_code": city_code})
                raise ConflictError("批次编号相同但内容不一致，已暂存冲突且未入库计算")
            return {"status": "duplicate", "batch_no": batch_no, "period": existing["period"], "city_code": existing["city_code"],
                    "processed_at": existing["processed_at"], "result_ids": json.loads(existing["result_ids_json"])}
        if late and not reason.strip():
            raise ValidationError("关账后的迟到修订必须说明原因")
        with self.database.transaction() as conn:
            dimensions = sorted({item["dimension"] for item in normalized})
            observation_ids = []
            for item in normalized:
                observation_ids.append(self._ingest_record(conn, batch_no, period, city_code, item, context.actor_id, late))
            conn.execute(
                "INSERT INTO lr_batches(batch_no,period,city_code,payload_digest,status,record_count,submitted_by,submitted_at,processed_at) VALUES(?,?,?,?,'received',?,?,?,?)",
                (batch_no, period, city_code, digest, len(normalized), context.actor_id, self.clock.now(), None),
            )
            self._audit(conn, context.actor_id, "lr.submit_batch", batch_no, 1, {"period": period, "city_code": city_code, "late": late, "dimensions": dimensions, "reason": reason.strip()})
            job_id = self._enqueue_recalc(conn, period, city_code, dimensions, reason.strip() or ("迟到修订" if late else "批次报送"), context.actor_id)
            return {"status": "accepted", "batch_no": batch_no, "period": period, "city_code": city_code,
                    "observations": observation_ids, "late": late, "recalc_job": job_id}

    # ---- 换算与权重调整：双人确认 -------------------------------------------

    def propose_conversion(self, context: AccessContext, observation_id: str, *, multiplier: float, normalized_unit: str, reason: str) -> dict:
        context.require("write:leisure")
        multiplier = self._require_number(multiplier, "换算系数")
        if multiplier <= 0:
            raise ValidationError("换算系数必须大于零")
        normalized_unit = self._require_text(normalized_unit, "目标单位")
        reason = self._require_text(reason, "换算原因")
        with self.database.transaction() as conn:
            obs = conn.execute("SELECT * FROM lr_observations WHERE observation_id=?", (observation_id,)).fetchone()
            if not obs:
                raise NotFoundError("观测不存在")
            if obs["status"] != "pending_conversion":
                raise ConflictError("该观测无需换算或换算已确认")
            period = conn.execute("SELECT status FROM lr_periods WHERE period=?", (obs["period"],)).fetchone()["status"]
            if period == "closed":
                raise ConflictError("关账后不再受理换算调整，请走迟到修订")
            before = {"unit": obs["unit"], "value": obs["raw_value"]}
            after = {"unit": normalized_unit, "value": _round6(obs["raw_value"] * multiplier), "multiplier": multiplier}
            return self._insert_proposal(conn, context, obs["period"], obs["city_code"], "conversion", obs["dimension"], obs["metric_code"], observation_id, before, after, reason)

    def propose_weight(self, context: AccessContext, period: str, city_code: str, dimension: str, metric_code: str, new_weight: float, reason: str) -> dict:
        context.require("write:leisure")
        self._require_metric(dimension, metric_code)
        new_weight = self._require_number(new_weight, "权重")
        if new_weight < 0:
            raise ValidationError("权重不能为负")
        reason = self._require_text(reason, "调整原因")
        with self.database.transaction() as conn:
            status = conn.execute("SELECT status FROM lr_periods WHERE period=?", (period,)).fetchone()
            if not status:
                raise NotFoundError("统计期不存在")
            if status["status"] == "closed":
                raise ConflictError("关账后权重冻结，调整不再影响已发布结果")
            self._ensure_weight(conn, period, city_code, dimension, metric_code)
            current = conn.execute("SELECT weight_id,weight,version_seq FROM lr_weights WHERE period=? AND city_code=? AND dimension=? AND metric_code=? AND status='active'", (period, city_code, dimension, metric_code)).fetchone()
            if _round6(current["weight"]) == _round6(new_weight):
                raise ValidationError("新权重与当前生效权重相同")
            before = {"weight_id": current["weight_id"], "weight": current["weight"], "version_seq": current["version_seq"]}
            after = {"weight": new_weight}
            return self._insert_proposal(conn, context, period, city_code, "weight", dimension, metric_code, current["weight_id"], before, after, reason)

    def confirm_proposal(self, context: AccessContext, proposal_id: str) -> dict:
        context.require("confirm:leisure")
        with self.database.transaction() as conn:
            row = conn.execute("SELECT * FROM lr_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
            if not row:
                raise NotFoundError("调整提案不存在")
            if row["status"] != "proposed":
                raise ConflictError("提案已处理")
            period_status = conn.execute("SELECT status FROM lr_periods WHERE period=?", (row["period"],)).fetchone()["status"]
            if period_status == "closed":
                raise ConflictError("统计期已关账，待发布结果已经冻结，调整不能再生效")
            # 提出者与确认者必须是两名成员。
            assert_distinct(row["proposed_by"], context.actor_id)
            before = json.loads(row["before_json"]); after = json.loads(row["after_json"])
            target_ref = before.get("observation_id") if row["kind"] == "conversion" else before.get("weight_id")
            if row["kind"] == "conversion":
                changed = conn.execute(
                    "UPDATE lr_observations SET normalized_value=?,normalized_unit=?,status='active' WHERE observation_id=? AND status='pending_conversion'",
                    (after["value"], after["unit"], target_ref),
                )
                if changed.rowcount != 1:
                    raise ConflictError("待换算观测状态已变化")
            else:
                current = conn.execute("SELECT * FROM lr_weights WHERE weight_id=?", (target_ref,)).fetchone()
                if not current or current["status"] != "active":
                    raise ConflictError("被调整权重已不是生效版本")
                conn.execute("UPDATE lr_weights SET status='superseded' WHERE weight_id=?", (target_ref,))
                weight_id = f"weight:{row['period']}:{row['city_code']}:{row['dimension']}:{row['metric_code']}:v{current['version_seq'] + 1}"
                conn.execute(
                    "INSERT INTO lr_weights(weight_id,period,city_code,dimension,metric_code,weight,version_seq,source_proposal_id,status,created_at,created_by) VALUES(?,?,?,?,?,?,?,?, 'active', ?,?)",
                    (weight_id, row["period"], row["city_code"], row["dimension"], row["metric_code"], after["weight"], current["version_seq"] + 1, proposal_id, self.clock.now(), context.actor_id),
                )
            conn.execute("UPDATE lr_proposals SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE proposal_id=?", (context.actor_id, self.clock.now(), proposal_id))
            self._audit(conn, context.actor_id, "lr.confirm_proposal", proposal_id, 1, {"kind": row["kind"], "proposed_by": row["proposed_by"]})
            job_id = self._enqueue_recalc(conn, row["period"], row["city_code"], [row["dimension"]], f"调整确认 {proposal_id}", context.actor_id)
            return {"proposal_id": proposal_id, "status": "confirmed", "recalc_job": job_id}

    def list_proposals(self, context: AccessContext, period: str, *, city_code: str | None = None, status: str | None = None) -> list[dict]:
        context.require("read:leisure")
        sql = "SELECT * FROM lr_proposals WHERE period=?"; params: list[object] = [period]
        if city_code:
            self._require_city_conn(context, city_code)
            sql += " AND city_code=?"; params.append(city_code)
        if status:
            sql += " AND status=?"; params.append(status)
        sql += " ORDER BY created_at,proposal_id"
        with self.database.connect() as conn:
            rows = [row for row in conn.execute(sql, params) if self._can_read_city(context, row["city_code"])]
            return [self._proposal_dict(row) for row in rows]

    # ---- 计算与选择性重算 ----------------------------------------------------

    def calculate(self, context: AccessContext, period: str, city_code: str, dimension: str) -> dict:
        context.require("write:leisure")
        with self.database.transaction() as conn:
            status = conn.execute("SELECT status FROM lr_periods WHERE period=?", (period,)).fetchone()
            if not status:
                raise NotFoundError("统计期不存在")
            if status["status"] == "closed":
                raise ConflictError("关账后不能直接改算，请通过迟到修订触发")
            result = self._calculate_dimension(conn, period, city_code, dimension, post_close=False, actor=context.actor_id, calc_batch_no=None)
            return result

    def process_recalc_jobs(self, *, limit: int = 10) -> list[dict]:
        """处理待重算任务；每个任务独立事务，进程崩溃则整体回滚为待处理。"""
        if limit < 1:
            raise ValidationError("limit 必须大于零")
        done = []
        with self.database.connect() as conn:
            jobs = conn.execute("SELECT job_id FROM lr_recalc_jobs WHERE status='pending' ORDER BY created_at,job_id LIMIT ?", (limit,)).fetchall()
        for row in jobs:
            job_id = row["job_id"]
            try:
                with self.database.transaction() as connection:
                    job = connection.execute("SELECT * FROM lr_recalc_jobs WHERE job_id=? AND status='pending'", (job_id,)).fetchone()
                    if not job:
                        continue
                    period_status = connection.execute("SELECT status FROM lr_periods WHERE period=?", (job["period"],)).fetchone()["status"]
                    post_close = period_status == "closed"
                    result_ids = []
                    for dimension in json.loads(job["dimensions_json"]):
                        result = self._calculate_dimension(connection, job["period"], job["city_code"], dimension, post_close=post_close, actor=job["triggered_by"], calc_batch_no=None)
                        result_ids.append(result["result_id"])
                    self._backfill_batches(connection, job["period"], job["city_code"])
                    connection.execute("UPDATE lr_recalc_jobs SET status='done',processed_at=?,result_ids_json=? WHERE job_id=?", (self.clock.now(), canonical_json(result_ids), job_id))
                    item = {"job_id": job_id, "period": job["period"], "city_code": job["city_code"], "status": "done", "result_ids": result_ids}
                done.append(item)
            except Exception as exc:  # 单任务失败不阻塞其他任务，重启后仍可重试或排查。
                with self.database.transaction() as connection:
                    connection.execute("UPDATE lr_recalc_jobs SET status='failed',last_error=? WHERE job_id=? AND status='pending'", (str(exc)[:500], job_id))
                done.append({"job_id": job_id, "status": "failed", "error": str(exc)[:500]})
        return done

    def reset_failed_job(self, context: AccessContext, job_id: str) -> dict:
        """数据修复（如换算补确认）后，让失败的重算任务重新排队。"""
        context.require("write:leisure")
        with self.database.transaction() as conn:
            row = conn.execute("SELECT status FROM lr_recalc_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                raise NotFoundError("重算任务不存在")
            if row["status"] != "failed":
                raise ConflictError("只有失败的任务可以重新排队")
            conn.execute("UPDATE lr_recalc_jobs SET status='pending',last_error='' WHERE job_id=?", (job_id,))
            self._audit(conn, context.actor_id, "lr.reset_recalc", job_id, 1, {})
            return {"job_id": job_id, "status": "pending"}

    def list_recalc_jobs(self, *, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM lr_recalc_jobs"; params: list[object] = []
        if status:
            sql += " WHERE status=?"; params.append(status)
        sql += " ORDER BY created_at,job_id"
        with self.database.connect() as conn:
            rows = [dict(row) for row in conn.execute(sql, params)]
        for item in rows:
            item["dimensions"] = json.loads(item.pop("dimensions_json"))
            item["result_ids"] = json.loads(item.pop("result_ids_json"))
        return rows

    # ---- 榜单发布（冻结原貌）与勘误 -----------------------------------------

    def publish_ranking(self, context: AccessContext, period: str) -> dict:
        context.require("publish:leisure")
        with self.database.transaction() as conn:
            status = conn.execute("SELECT status FROM lr_periods WHERE period=?", (period,)).fetchone()
            if not status:
                raise NotFoundError("统计期不存在")
            if status["status"] != "closed":
                raise ConflictError("只有关账统计期才能发布年度榜单")
            if conn.execute("SELECT 1 FROM lr_rankings WHERE period=?", (period,)).fetchone():
                raise ConflictError("正式榜单已经发布，原貌冻结，变化只能以勘误说明")
            cities = [row["city_code"] for row in conn.execute("SELECT DISTINCT city_code FROM lr_results WHERE period=? AND status='draft' ORDER BY city_code", (period,))]
            if not cities:
                raise ValidationError("还没有待发布的计算结果")
            entries = []
            for city_code in cities:
                missing = [d for d in DIMENSIONS if not conn.execute("SELECT 1 FROM lr_results WHERE period=? AND city_code=? AND dimension=? AND status='draft'", (period, city_code, d)).fetchone()]
                if missing:
                    raise ConflictError(f"{city_code} 缺少维度结果: {', '.join(missing)}")
                result_rows = [conn.execute("SELECT * FROM lr_results WHERE period=? AND city_code=? AND dimension=? AND status='draft'", (period, city_code, d)).fetchone() for d in DIMENSIONS]
                total = _round6(sum(DIMENSION_WEIGHTS[d] * float(r["score"]) for d, r in zip(DIMENSIONS, result_rows)))
                result_ids = [r["result_id"] for r in result_rows]
                for r in result_rows:
                    conn.execute("UPDATE lr_results SET status='published',published_at=?,published_by=? WHERE result_id=?", (self.clock.now(), context.actor_id, r["result_id"]))
                ranking_id = f"rank:{period}:{city_code}"
                conn.execute(
                    "INSERT INTO lr_rankings(ranking_id,period,city_code,rank_no,total_score,result_ids_json,dimension_weights_json,published_at,published_by) VALUES(?,?,?,?,?,?,?,?,?)",
                    (ranking_id, period, city_code, 0, total, canonical_json(result_ids), canonical_json(DIMENSION_WEIGHTS), self.clock.now(), context.actor_id),
                )
                entries.append({"ranking_id": ranking_id, "city_code": city_code, "total_score": total, "result_ids": result_ids})
            entries.sort(key=lambda item: (-item["total_score"], item["city_code"]))
            for rank, item in enumerate(entries, start=1):
                item["rank_no"] = rank
                conn.execute("UPDATE lr_rankings SET rank_no=? WHERE ranking_id=?", (rank, item["ranking_id"]))
            self._audit(conn, context.actor_id, "lr.publish_ranking", period, 1, {"cities": [e["city_code"] for e in entries]})
            return {"period": period, "status": "published", "rankings": entries}

    def list_rankings(self, context: AccessContext, period: str, *, include_errata: bool = True) -> list[dict]:
        context.require("read:leisure")
        with self.database.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM lr_rankings WHERE period=? ORDER BY rank_no", (period,))]
            errata = [dict(row) for row in conn.execute("SELECT * FROM lr_errata WHERE period=? ORDER BY issued_at,erratum_id", (period,))]
        result = []
        for row in rows:
            if not self._can_read_city(context, row["city_code"]):
                continue
            row["result_ids"] = json.loads(row.pop("result_ids_json"))
            row["dimension_weights"] = json.loads(row.pop("dimension_weights_json"))
            if include_errata:
                row["errata"] = [self._erratum_dict(item) for item in errata if item["city_code"] == row["city_code"]]
            result.append(row)
        return result

    def issue_errata(self, context: AccessContext, period: str, city_code: str, reason: str) -> dict:
        context.require("publish:leisure")
        reason = self._require_text(reason, "勘误原因")
        with self.database.transaction() as conn:
            ranking = conn.execute("SELECT * FROM lr_rankings WHERE period=? AND city_code=?", (period, city_code)).fetchone()
            if not ranking:
                raise NotFoundError("该城市没有已发布的正式榜单记录")
            new_rows = []
            for d in DIMENSIONS:
                latest = conn.execute(
                    "SELECT * FROM lr_results WHERE period=? AND city_code=? AND dimension=? AND status IN ('recalculated','published') ORDER BY CASE status WHEN 'recalculated' THEN 0 ELSE 1 END,created_at DESC,result_id DESC LIMIT 1",
                    (period, city_code, d),
                ).fetchone()
                new_rows.append(latest)
            if not all(new_rows):
                raise ConflictError("该城市仍有维度没有任何已发布或重算结果，暂不能出勘误")
            linked_ids = [r["result_id"] for r in new_rows]
            changed = any(r["status"] == "recalculated" for r in new_rows)
            if not changed:
                raise ConflictError("该城市没有新的重算结果，无需勘误")
            latest = conn.execute("SELECT linked_result_ids_json FROM lr_errata WHERE period=? AND city_code=? ORDER BY issued_at DESC,erratum_id DESC LIMIT 1", (period, city_code)).fetchone()
            if latest and json.loads(latest["linked_result_ids_json"]) == linked_ids:
                return {"status": "duplicate", "period": period, "city_code": city_code, "linked_result_ids": linked_ids}
            new_total = _round6(sum(DIMENSION_WEIGHTS[d] * float(r["score"]) for d, r in zip(DIMENSIONS, new_rows)))
            # 新名次：该城市用最新重算值；其他城市若已有勘误，沿用上一份勘误的更正值，否则用正式榜单原值。
            board = []
            for row in conn.execute("SELECT city_code FROM lr_rankings WHERE period=?", (period,)):
                other = row["city_code"]
                if other == city_code:
                    board.append((other, new_total)); continue
                prior = conn.execute("SELECT new_score FROM lr_errata WHERE period=? AND city_code=? ORDER BY issued_at DESC,erratum_id DESC LIMIT 1", (period, other)).fetchone()
                base = conn.execute("SELECT total_score FROM lr_rankings WHERE period=? AND city_code=?", (period, other)).fetchone()["total_score"]
                board.append((other, prior["new_score"] if prior else base))
            board.sort(key=lambda item: (-item[1], item[0]))
            new_rank = next(i for i, (c, _) in enumerate(board, start=1) if c == city_code)
            erratum_id = new_id("erratum")
            conn.execute(
                "INSERT INTO lr_errata(erratum_id,period,city_code,ranking_id,old_rank,new_rank,old_score,new_score,linked_result_ids_json,reason,issued_by,issued_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (erratum_id, period, city_code, ranking["ranking_id"], ranking["rank_no"], new_rank, ranking["total_score"], new_total, canonical_json(linked_ids), reason, context.actor_id, self.clock.now()),
            )
            self._audit(conn, context.actor_id, "lr.issue_errata", erratum_id, 1, {"period": period, "city_code": city_code, "old_rank": ranking["rank_no"], "new_rank": new_rank})
            return {"erratum_id": erratum_id, "period": period, "city_code": city_code, "ranking_id": ranking["ranking_id"],
                    "old_rank": ranking["rank_no"], "new_rank": new_rank, "old_score": ranking["total_score"], "new_score": new_total,
                    "linked_result_ids": linked_ids, "reason": reason}

    def list_errata(self, context: AccessContext, period: str) -> list[dict]:
        context.require("read:leisure")
        with self.database.connect() as conn:
            rows = conn.execute("SELECT * FROM lr_errata WHERE period=? ORDER BY issued_at,erratum_id", (period,)).fetchall()
        return [self._erratum_dict(row) for row in rows if self._can_read_city(context, row["city_code"])]

    # ---- 溯源：从排名追到指标值、人口版本、来源、调整理由与责任人 -------------

    def trace_city(self, context: AccessContext, period: str, city_code: str) -> dict:
        context.require("read:leisure")
        self._require_city_conn(context, city_code)
        with self.database.connect() as conn:
            ranking = conn.execute("SELECT * FROM lr_rankings WHERE period=? AND city_code=?", (period, city_code)).fetchone()
            if not ranking:
                raise NotFoundError("该城市在此统计期没有正式榜单记录")
            result_rows = conn.execute("SELECT * FROM lr_results WHERE period=? AND city_code=? ORDER BY dimension,created_at", (period, city_code)).fetchall()
            results = []
            for row in result_rows:
                item = dict(row)
                item["lineage"] = json.loads(row["lineage_json"])
                item.pop("lineage_json", None)
                results.append(item)
            errata = [self._erratum_dict(r) for r in conn.execute("SELECT * FROM lr_errata WHERE period=? AND city_code=? ORDER BY issued_at", (period, city_code))]
            baselines = [dict(r) for r in conn.execute("SELECT baseline_id,scope,version_seq,population,unit,coverage_start,coverage_end,reason,status,created_at,created_by FROM lr_baselines WHERE period=? AND city_code=? ORDER BY scope,version_seq", (period, city_code))]
            batches = [dict(r) for r in conn.execute("SELECT batch_no,payload_digest,status,record_count,submitted_by,submitted_at,processed_at,result_ids_json FROM lr_batches WHERE period=? AND city_code=? ORDER BY submitted_at,batch_no", (period, city_code))]
            for item in batches:
                item["result_ids"] = json.loads(item.pop("result_ids_json"))
            ranking_dict = dict(ranking)
            ranking_dict["result_ids"] = json.loads(ranking_dict.pop("result_ids_json"))
            ranking_dict["dimension_weights"] = json.loads(ranking_dict.pop("dimension_weights_json"))
            proposals = [self._proposal_dict(r) for r in conn.execute("SELECT * FROM lr_proposals WHERE period=? AND city_code=? ORDER BY created_at", (period, city_code))]
            return {"period": period, "city_code": city_code, "ranking": ranking_dict, "errata": errata, "baselines": baselines, "batches": batches, "results": results, "proposals": proposals}

    # ---- 内部实现 -----------------------------------------------------------

    def _calculate_dimension(self, conn, period: str, city_code: str, dimension: str, *, post_close: bool, actor: str, calc_batch_no: str | None) -> dict:
        if dimension not in METRICS:
            raise ValidationError(f"未知维度: {dimension}")
        inputs = []
        weight_items = []
        for metric_code, catalog in METRICS[dimension].items():
            obs = self._pick_observation(conn, period, city_code, dimension, metric_code)
            if not obs:
                raise ValidationError(f"{city_code} 缺少指标 {metric_code} 的有效观测")
            if obs["status"] != "active":
                raise ValidationError(f"{metric_code} 仍有待确认换算")
            baseline = conn.execute("SELECT * FROM lr_baselines WHERE baseline_id=?", (obs["baseline_id"],)).fetchone()
            if not baseline:
                raise ValidationError(f"{metric_code} 引用的人口基准版本缺失")
            weight = self._ensure_weight(conn, period, city_code, dimension, metric_code)
            value = float(obs["normalized_value"])
            if catalog["per_capita"]:
                value = value / float(baseline["population"]) * 10000.0
            value = _round6(value)
            inputs.append({
                "metric_code": metric_code, "basis": obs["basis"],
                "raw_value": obs["raw_value"], "unit": obs["unit"],
                "normalized_value": obs["normalized_value"], "normalized_unit": obs["normalized_unit"],
                "metric_value": value,
                "denominator": {"scope": baseline["scope"], "baseline_id": baseline["baseline_id"], "version_seq": baseline["version_seq"],
                                "population": baseline["population"], "unit": baseline["unit"],
                                "coverage_start": baseline["coverage_start"], "coverage_end": baseline["coverage_end"]},
                "coverage_start": obs["coverage_start"], "coverage_end": obs["coverage_end"],
                "source_ref": obs["source_ref"], "batch_no": obs["batch_no"], "observation_id": obs["observation_id"],
                "conversion": self._conversion_lineage(conn, obs["observation_id"]),
            })
            weight_items.append({"metric_code": metric_code, "weight": weight["weight"], "weight_id": weight["weight_id"],
                                 "version_seq": weight["version_seq"],
                                 "change": self._weight_lineage(conn, weight["weight_id"])})
        score = _round6(sum(item["metric_value"] * item_w["weight"] for item, item_w in zip(inputs, weight_items)))
        inputs_digest = digest_json(inputs)
        weight_digest = digest_json(weight_items)
        lineage = {"dimension": dimension, "inputs": inputs, "weights": weight_items,
                   "dimension_weight": DIMENSION_WEIGHTS[dimension], "formula": "Σ 指标值×指标权重（人均类=标准化值/人口版本×10000）",
                   "post_close": post_close}
        if not post_close:
            existing = conn.execute("SELECT * FROM lr_results WHERE period=? AND city_code=? AND dimension=? AND status='draft'", (period, city_code, dimension)).fetchone()
            if existing and existing["inputs_digest"] == inputs_digest and existing["weight_digest"] == weight_digest:
                item = dict(existing); item["lineage"] = json.loads(existing["lineage_json"]); item.pop("lineage_json", None)
                return item
            if existing:
                conn.execute("UPDATE lr_results SET status='superseded' WHERE result_id=?", (existing["result_id"],))
            result_id = new_id("result")
            conn.execute(
                "INSERT INTO lr_results(result_id,period,city_code,dimension,score,inputs_digest,weight_digest,calc_batch_no,status,post_close,lineage_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?, 'draft', 0, ?,?,?)",
                (result_id, period, city_code, dimension, score, inputs_digest, weight_digest, calc_batch_no, canonical_json(lineage), self.clock.now(), actor),
            )
            self._audit(conn, actor, "lr.calculate", result_id, 1, {"period": period, "city_code": city_code, "dimension": dimension, "post_close": False})
        else:
            identical = conn.execute(
                "SELECT * FROM lr_results WHERE period=? AND city_code=? AND dimension=? AND inputs_digest=? AND weight_digest=?",
                (period, city_code, dimension, inputs_digest, weight_digest),
            ).fetchall()
            if identical:
                # 迟到修订未改变该维度的有效输入：沿用既有（已发布或已重算）结果。
                row = max(identical, key=lambda r: (r["created_at"], r["result_id"]))
                item = dict(row); item["lineage"] = json.loads(row["lineage_json"]); item.pop("lineage_json", None)
                return item
            result_id = new_id("result")
            conn.execute(
                "INSERT INTO lr_results(result_id,period,city_code,dimension,score,inputs_digest,weight_digest,calc_batch_no,status,post_close,lineage_json,created_at,created_by) VALUES(?,?,?,?,?,?,?,?, 'recalculated', 1, ?,?,?)",
                (result_id, period, city_code, dimension, score, inputs_digest, weight_digest, calc_batch_no, canonical_json(lineage), self.clock.now(), actor),
            )
            self._audit(conn, actor, "lr.recalculate", result_id, 1, {"period": period, "city_code": city_code, "dimension": dimension, "post_close": True})
        return {"result_id": result_id, "period": period, "city_code": city_code, "dimension": dimension, "score": score,
                "inputs_digest": inputs_digest, "weight_digest": weight_digest, "status": "draft" if not post_close else "recalculated",
                "post_close": post_close, "lineage": lineage}

    def _pick_observation(self, conn, period: str, city_code: str, dimension: str, metric_code: str):
        rows = conn.execute("SELECT * FROM lr_observations WHERE period=? AND city_code=? AND dimension=? AND metric_code=? AND status='active' ORDER BY created_at DESC,observation_id DESC", (period, city_code, dimension, metric_code)).fetchall()
        if not rows:
            return None
        preferred = METRICS[dimension][metric_code]["basis"]
        chosen = [r for r in rows if r["basis"] == preferred] or rows
        return max(chosen, key=lambda r: (r["created_at"], r["observation_id"]))

    def _ensure_weight(self, conn, period: str, city_code: str, dimension: str, metric_code: str):
        row = conn.execute("SELECT * FROM lr_weights WHERE period=? AND city_code=? AND dimension=? AND metric_code=? AND status='active'", (period, city_code, dimension, metric_code)).fetchone()
        if row:
            return row
        default_value = METRICS[dimension][metric_code]["default_weight"]
        weight_id = f"weight:{period}:{city_code}:{dimension}:{metric_code}:v1"
        conn.execute(
            "INSERT INTO lr_weights(weight_id,period,city_code,dimension,metric_code,weight,version_seq,source_proposal_id,status,created_at,created_by) VALUES(?,?,?,?,?,?,?,NULL, 'active', ?, 'system')",
            (weight_id, period, city_code, dimension, metric_code, default_value, 1, self.clock.now()),
        )
        return conn.execute("SELECT * FROM lr_weights WHERE weight_id=?", (weight_id,)).fetchone()

    def _ingest_record(self, conn, batch_no: str, period: str, city_code: str, item: dict, actor: str, late: bool) -> str:
        dimension, metric_code = item["dimension"], item["metric_code"]
        catalog = METRICS[dimension][metric_code]
        baseline = conn.execute("SELECT * FROM lr_baselines WHERE period=? AND city_code=? AND scope=? AND status='active'", (period, city_code, item["basis"])).fetchone()
        if not baseline:
            raise ValidationError(f"{city_code} 缺少 {SCOPE_LABELS[item['basis']]} 口径的人口基准版本，无法固定分母")
        needs_conversion = item["unit"] != catalog["unit"]
        if late and needs_conversion:
            raise ValidationError(f"{metric_code} 的单位 {item['unit']} 与标准单位 {catalog['unit']} 不一致；换算须在关账前完成双人确认，迟到修订请直接报送标准单位数据")
        observation_id = new_id("obs")
        if needs_conversion:
            normalized_value, normalized_unit, status = item["raw_value"], item["unit"], "pending_conversion"
        else:
            normalized_value, normalized_unit, status = item["raw_value"], catalog["unit"], "active"
        # 同城市同指标同口径的旧观测被本次报送取代（统计期内的补录/修订）。
        conn.execute(
            "UPDATE lr_observations SET status='superseded',superseded_by=? WHERE period=? AND city_code=? AND dimension=? AND metric_code=? AND basis=? AND status IN ('active','pending_conversion')",
            (observation_id, period, city_code, dimension, metric_code, item["basis"]),
        )
        conn.execute(
            "INSERT INTO lr_observations(observation_id,period,city_code,dimension,metric_code,basis,raw_value,unit,normalized_value,normalized_unit,denominator_scope,baseline_id,coverage_start,coverage_end,source_ref,batch_no,status,created_at,created_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (observation_id, period, city_code, dimension, metric_code, item["basis"], item["raw_value"], item["unit"], normalized_value, normalized_unit, item["basis"], baseline["baseline_id"], item["coverage_start"], item["coverage_end"], item["source_ref"], batch_no, status, self.clock.now(), actor),
        )
        return observation_id

    def _enqueue_recalc(self, conn, period: str, city_code: str, dimensions: list[str], reason: str, triggered_by: str) -> str:
        job_id = new_id("recalc")
        conn.execute(
            "INSERT INTO lr_recalc_jobs(job_id,period,city_code,dimensions_json,reason,triggered_by,status,created_at) VALUES(?,?,?,?,?,?,'pending',?)",
            (job_id, period, city_code, canonical_json(sorted(set(dimensions))), reason, triggered_by, self.clock.now()),
        )
        return job_id

    def _backfill_batches(self, conn, period: str, city_code: str) -> None:
        batch_rows = conn.execute("SELECT DISTINCT batch_no FROM lr_observations WHERE period=? AND city_code=?", (period, city_code)).fetchall()
        for row in batch_rows:
            batch_no = row["batch_no"]
            dims = [r["dimension"] for r in conn.execute("SELECT DISTINCT dimension FROM lr_observations WHERE batch_no=?", (batch_no,))]
            result_ids = []
            for dimension in dims:
                latest = conn.execute(
                    "SELECT result_id FROM lr_results WHERE period=? AND city_code=? AND dimension=? AND status IN ('recalculated','draft','published') ORDER BY CASE status WHEN 'recalculated' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,created_at DESC,result_id DESC LIMIT 1",
                    (period, city_code, dimension),
                ).fetchone()
                if latest:
                    result_ids.append(latest["result_id"])
            conn.execute("UPDATE lr_batches SET status='processed',processed_at=COALESCE(processed_at,?),result_ids_json=? WHERE batch_no=?", (self.clock.now(), canonical_json(result_ids), batch_no))

    def _insert_proposal(self, conn, context: AccessContext, period: str, city_code: str | None, kind: str, dimension: str, metric_code: str, target_ref: str, before: dict, after: dict, reason: str) -> dict:
        proposal_id = new_id("proposal")
        before = dict(before); before["observation_id" if kind == "conversion" else "weight_id"] = target_ref
        conn.execute(
            "INSERT INTO lr_proposals(proposal_id,period,city_code,kind,dimension,metric_code,before_json,after_json,reason,status,proposed_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,'proposed',?,?)",
            (proposal_id, period, city_code, kind, dimension, metric_code, canonical_json(before), canonical_json(after), reason, context.actor_id, self.clock.now()),
        )
        self._audit(conn, context.actor_id, "lr.propose", proposal_id, 1, {"kind": kind, "period": period, "city_code": city_code, "dimension": dimension, "metric_code": metric_code})
        return {"proposal_id": proposal_id, "kind": kind, "status": "proposed", "period": period, "city_code": city_code,
                "dimension": dimension, "metric_code": metric_code, "before": before, "after": after, "reason": reason,
                "proposed_by": context.actor_id}

    def _conversion_lineage(self, conn, observation_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM lr_proposals WHERE kind='conversion' AND status='confirmed' AND json_extract(before_json,'$.observation_id')=? ORDER BY confirmed_at DESC LIMIT 1", (observation_id,)).fetchone()
        if not row:
            return None
        return {"proposal_id": row["proposal_id"], "reason": row["reason"], "proposed_by": row["proposed_by"], "confirmed_by": row["confirmed_by"], "after": json.loads(row["after_json"])}

    def _weight_lineage(self, conn, weight_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM lr_weights WHERE weight_id=?", (weight_id,)).fetchone()
        if not row["weight_id"] or not row["source_proposal_id"]:
            return None
        proposal = conn.execute("SELECT * FROM lr_proposals WHERE proposal_id=?", (row["source_proposal_id"],)).fetchone()
        if not proposal:
            return None
        return {"proposal_id": proposal["proposal_id"], "reason": proposal["reason"], "proposed_by": proposal["proposed_by"],
                "confirmed_by": proposal["confirmed_by"], "before": json.loads(proposal["before_json"]), "after": json.loads(proposal["after_json"])}

    def _transition_period(self, conn, actor: str, period: str, target: str, *, expect: str) -> None:
        row = conn.execute("SELECT status FROM lr_periods WHERE period=?", (period,)).fetchone()
        if not row:
            raise NotFoundError("统计期不存在")
        if row["status"] != expect:
            raise ConflictError(f"统计期处于 {row['status']}，不能转到 {target}")
        if target == "review":
            conn.execute("UPDATE lr_periods SET status='review',review_at=? WHERE period=?", (self.clock.now(), period))
        else:
            conn.execute("UPDATE lr_periods SET status='closed',closed_at=?,closed_by=? WHERE period=?", (self.clock.now(), actor, period))
        self._audit(conn, actor, f"lr.period_{target}", period, 1, {"from": expect})

    def _audit(self, conn, actor: str, action: str, entity_id: str, version: int, detail: dict) -> None:
        AuditLog(self.clock).append(conn, actor_id=actor, action=action, entity_type="leisure", entity_id=entity_id, version=version, detail=detail)

    def _can_read_city(self, context: AccessContext, city_code: str) -> bool:
        return "*" in context.scopes or city_code in context.scopes

    def _require_city_conn(self, context: AccessContext, city_code: str) -> None:
        if not self._can_read_city(context, city_code):
            raise PermissionDenied(f"合作机构未获授权查阅城市 {city_code}")

    @staticmethod
    def _require_city(conn, city_code: str) -> None:
        if conn is None:
            return
        row = conn.execute("SELECT 1 FROM lr_cities WHERE city_code=?", (city_code,)).fetchone()
        if not row:
            raise NotFoundError("城市未登记")

    @staticmethod
    def _require_period(conn, period: str) -> None:
        if not conn.execute("SELECT 1 FROM lr_periods WHERE period=?", (period,)).fetchone():
            raise NotFoundError("统计期不存在")

    @staticmethod
    def _period_dict(conn, period: str) -> dict:
        row = conn.execute("SELECT * FROM lr_periods WHERE period=?", (period,)).fetchone()
        if not row:
            raise NotFoundError("统计期不存在")
        return dict(row)

    @staticmethod
    def _proposal_dict(row) -> dict:
        item = dict(row)
        item["before"] = json.loads(item.pop("before_json"))
        item["after"] = json.loads(item.pop("after_json"))
        return item

    @staticmethod
    def _erratum_dict(row) -> dict:
        item = dict(row)
        item["linked_result_ids"] = json.loads(item.pop("linked_result_ids_json"))
        return item

    @staticmethod
    def _require_metric(dimension: str, metric_code: str) -> None:
        if dimension not in METRICS or metric_code not in METRICS[dimension]:
            raise ValidationError(f"未知指标: {dimension}/{metric_code}")

    @staticmethod
    def _validate_scope(scope: str) -> None:
        if scope not in SCOPES:
            raise ValidationError("人口口径必须是 municipal、urban 或 resident")

    def _validate_record(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ValidationError("报送记录必须是对象")
        unknown = set(values) - set(RECORD_FIELDS)
        if unknown:
            raise ValidationError("未知字段: " + ", ".join(sorted(unknown)))
        missing = [field for field in RECORD_FIELDS if field not in values]
        if missing:
            raise ValidationError("缺少字段: " + ", ".join(missing))
        dimension, metric_code = values["dimension"], values["metric_code"]
        self._require_metric(dimension, metric_code)
        self._validate_scope(values["basis"])
        unit = self._require_text(values["unit"], "单位")
        source_ref = self._require_text(values["source_ref"], "样本出处")
        raw_value = self._require_number(values["raw_value"], "原始数值")
        if raw_value < 0:
            raise ValidationError("原始数值不能为负")
        coverage_start = canonical_instant(values["coverage_start"])
        coverage_end = canonical_instant(values["coverage_end"])
        if coverage_end < coverage_start:
            raise ValidationError("覆盖期结束时间不能早于开始时间")
        return {"dimension": dimension, "metric_code": metric_code, "raw_value": raw_value, "unit": unit,
                "basis": values["basis"], "coverage_start": coverage_start, "coverage_end": coverage_end, "source_ref": source_ref}

    @staticmethod
    def _require_text(value: object, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{label}不能为空")
        return value.strip()

    @staticmethod
    def _require_number(value: object, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{label}必须是数字")
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValidationError(f"{label}必须是有限数")
        return value
