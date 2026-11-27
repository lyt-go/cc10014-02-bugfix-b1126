"""模块服务基类：共享查询、登记、动作和统计骨架，及跨模块业务联动。

每个领域服务除了基础CRUD外，还可以通过 cross_module_verify / cross_module_effect
注入跨模块业务校验逻辑和副作用。
"""
from __future__ import annotations

from typing import Any

from app.store import store


class BaseModuleService:
    MODULE = ""
    ENTITY = ""
    LIST_FIELDS: list[str] = []
    REQUIRED_FIELDS: list[str] = []
    STATUS_ORDER: list[str] = []
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    # ── 跨模块业务扩展点（子类可覆盖） ──
    def cross_module_verify(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> tuple[bool, str]:
        """执行动作前的跨模块校验。返回 (通过, 失败原因)。"""
        return True, ""

    def cross_module_effect(
        self, entry_id: int, action: str, entry: dict[str, Any]
    ) -> list[dict]:
        """动作执行后的跨模块副作用。返回受影响的联动条目列表。"""
        return []

    def resource_keys(self, entry_id: int, action: str) -> list[str]:
        """动作需要持有的资源锁。

        默认只锁本条记录；涉及跨模块共享余额的领域服务（变更签证、修缮支出）
        会追加同一把台账锁，保证校验与写入读到同一份余额版本。
        """
        return [f"{self.MODULE}:{entry_id}"]

    # ── 基础 CRUD ──
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(self.MODULE))
        for field, expected in (filters or {}).items():
            if field in self.LIST_FIELDS:
                rows = [
                    row for row in rows
                    if expected.casefold() in str(row.get(field, "")).casefold()
                ]
        if keyword and self.LIST_FIELDS:
            field = self.LIST_FIELDS[0]
            rows = [row for row in rows if keyword.casefold() in str(row.get(field, "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        rows = store.rows(self.MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in self.LIST_FIELDS})
        entry["status"] = self.STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作：幂等检查 -> 前置校验 -> 状态变更 -> 跨模块联动。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 按固定顺序获取全部资源锁，避免死锁。
        resource_keys = sorted(set(self.resource_keys(entry_id, action)))
        acquired: list[str] = []
        for key in resource_keys:
            if not store.acquire_resource(key):
                for held in acquired:
                    store.release_resource(held)
                return None, "资源正在被其他操作处理，请稍后重试"
            acquired.append(key)

        try:
            entry = store.find(self.MODULE, entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

            # 幂等检查放在锁内，防止并发的同一张签证重复扣减/重复入账。
            if store.check_idempotent(self.MODULE, entry_id, action):
                return entry, f"操作「{action}」已执行过，当前状态：{entry.get('status', '')}"

            # 跨模块前置校验：与余额写入处于同一临界区，保证读到同一份余额。
            passed, reason = self.cross_module_verify(entry_id, action, entry)
            if not passed:
                return None, reason

            entry["status"] = target
            entry["pending"] = target != self.STATUS_ORDER[-1]
            entry["abnormal"] = action in self.NEGATIVE_ACTIONS

            # 执行跨模块副作用
            self.cross_module_effect(entry_id, action, entry)

            # 标记幂等；反向动作作废同一条记录此前的幂等窗口，
            # 使驳回后能重新签认，而本次驳回的重复提交仍被挡住。
            if action in self.NEGATIVE_ACTIONS:
                store.mark_idempotent(self.MODULE, entry_id, action)
                store.clear_idempotent(self.MODULE, entry_id, keep_action=action)
            else:
                store.mark_idempotent(self.MODULE, entry_id, action)
            return entry, f"{self.ENTITY}已{action}"
        finally:
            for key in reversed(acquired):
                store.release_resource(key)

    def domain_snapshot(self) -> dict[str, Any]:
        """返回本模块的状态统计与业务对象，供其他模块读取。"""
        rows = store.rows(self.MODULE)
        by_status = {
            status: sum(1 for row in rows if row.get("status") == status)
            for status in self.STATUS_ORDER
        }
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}状态记录",
            "count": len(rows),
            "by_status": by_status,
            "abnormal": sum(1 for row in rows if row.get("abnormal")),
            "rows": [dict(row) for row in rows],
        }