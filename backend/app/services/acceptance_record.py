"""验收记录领域服务：签署验收后联动工序完成状态、修缮日志、归档和质量评定。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AcceptanceRecordService(BaseModuleService):
    MODULE = "acceptance_record"
    ENTITY = "验收报告"
    OPERATION_TERM = "开始验收"
    LIST_FIELDS = ["验收工序", "验收标准", "验收结论", "遗留问题", "整改期限", "验收人", "验收日期", "备注说明"]
    REQUIRED_FIELDS = ["验收工序", "验收标准", "验收结论"]
    STATUS_ORDER = ["待验收", "验收中", "已签署", "待整改"]
    ACTION_RULES = {"开始验收": "验收中", "签署验收": "已签署", "退回整改": "待整改"}
    NEGATIVE_ACTIONS = []

    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        process = entry.get("验收工序", "")
        if action == "签署验收":
            # 1. 必须有对应的质量检查已通过
            quality_records = store.query_by_field("quality_inspect", "检查工序", process)
            if not quality_records:
                return False, f"工序「{process}」缺少质量检查记录"
            if not any(r.get("status") == "已通过" for r in quality_records):
                return False, "关联质量检查未通过，无法签署验收"
            # 2. 关联排期中工期是否合理
            schedules = store.query_by_field("construction_schedule", "计划工序", process)
            for s in schedules:
                if s.get("status") == "已延期":
                    return False, "关联排期已延期，需先获取变更签证再验收"
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        effects = []
        if action == "签署验收":
            process = entry.get("验收工序", "")
            # 1. 联动工序完成状态
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["status"] = "已完成"
                    ws_row["pending"] = False
                    effects.append(dict(ws_row))

            # 2. 联动修缮日志归档
            for log in store.rows("restoration_log"):
                if log.get("对应工序", "") == process:
                    log["status"] = "已归档"
                    log["pending"] = False
                    effects.append(dict(log))

            # 3. 联动施工排期里程碑更新
            for sched in store.rows("construction_schedule"):
                if sched.get("计划工序", "") == process:
                    sched["里程碑节点"] = f"{process}-验收已签署"
                    effects.append(dict(sched))

            # 4. 触发档案归档（自动创建归档条目）
            arch_rows = store.rows("archive_mgmt")
            new_arch = {
                "id": max((int(r.get("id", 0)) for r in arch_rows), default=0) + 1,
                "档案名称": f"{process}-验收报告",
                "档案类别": "验收档案",
                "形成日期": entry.get("验收日期", ""),
                "保管期限": "永久",
                "密级等级": "内部",
                "归档人": entry.get("验收人", ""),
                "归档状态": "待整理",
                "备注说明": f"验收签署#{entry_id}自动触发",
                "status": "待整理",
                "pending": True,
                "abnormal": False,
            }
            arch_rows.append(new_arch)
            effects.append(new_arch)

            # 5. 更新竣工总结质量评定
            for summary in store.rows("completion_summary"):
                if summary.get("status") in ("编制中", "已提交"):
                    if summary.get("质量评定", "") == "":
                        summary["质量评定"] = f"工序{process}已验收"
                    effects.append(dict(summary))

        elif action == "退回整改":
            process = entry.get("验收工序", "")
            for ws_row in store.rows("work_sequence"):
                if ws_row.get("工序名称", "") == process:
                    ws_row["abnormal"] = True
                    effects.append(dict(ws_row))
        return effects