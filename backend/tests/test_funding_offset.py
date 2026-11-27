"""变更签证减项冲减与修缮支出台账的对账测试。

覆盖东跨挑檐场景：本工序已核准 600 元、待核准 400 元，监理签认 700 元减项。
"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.seed import SEED_ROWS
from app.services import funding_ledger
from app.services.change_order import ChangeOrderService
from app.services.construction_schedule import ConstructionScheduleService
from app.services.funding_use import FundingUseService
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store._idempotent_log.clear()
    store._resource_locks.clear()
    for name in list(store._tables.keys()):
        store._tables[name] = [dict(row) for row in SEED_ROWS.get(name, [])]
    yield


def add_funding(entry_id, process, amount, status="已核准"):
    store.rows("funding_use").append({
        "id": entry_id,
        "支出项目": f"{process}支出",
        "支出类别": "材料",
        "对应工序": process,
        "预算金额": amount,
        "实际金额": amount,
        "status": status,
        "pending": status != "已核准",
        "abnormal": False,
    })
    return entry_id


def prepare_process(process="东跨挑檐"):
    schedule, _ = ConstructionScheduleService().create_entry({
        "计划工序": process,
        "开始日期": "2026-12-01",
        "结束日期": "2026-12-10",
    })
    ConstructionScheduleService().run_action(schedule["id"], "发布排期")
    return schedule


def raise_change(amount, process="东跨挑檐"):
    service = ChangeOrderService()
    entry, missing = service.create_entry({
        "变更事项": f"{process}减项",
        "变更原因": "减料",
        "影响工序": process,
        "费用增减": amount,
    })
    assert missing == []
    service.run_action(entry["id"], "提出变更")
    return entry["id"]


def refund_rows():
    return [row for row in store.rows("funding_use") if funding_ledger.is_refund_row(row)]


# ── 核心回归：不得借用其他工序的已核准金额 ──

def test_negative_change_cannot_borrow_other_process_balance():
    prepare_process()
    add_funding(900, "西跨木料", 2000, "已核准")
    add_funding(901, "东跨挑檐", 600, "已核准")
    change_id = raise_change(-700)

    entry, message = ChangeOrderService().run_action(change_id, "签认变更")
    assert entry is None, "东跨挑檐没有可冲减的支出，不应借用西跨木料余额"
    assert "东跨挑檐" in message and "尚缺100.00元" in message
    assert refund_rows() == []
    assert funding_ledger.available_balance("东跨挑檐") == 600.0
    assert funding_ledger.available_balance("西跨木料") == 2000.0


def test_unapproved_funding_does_not_count_towards_balance():
    prepare_process()
    add_funding(901, "东跨挑檐", 600, "已核准")
    add_funding(902, "东跨挑檐", 400, "已提交")
    assert funding_ledger.available_balance("东跨挑檐") == 600.0


def test_full_scenario_approve_400_then_resubmit_700():
    prepare_process()
    add_funding(901, "东跨挑檐", 600, "已核准")
    add_funding(902, "东跨挑檐", 400, "已提交")
    change_id = raise_change(-700)

    rejected, message = ChangeOrderService().run_action(change_id, "签认变更")
    assert rejected is None and "尚缺100.00元" in message

    approved, msg = FundingUseService().run_action(902, "核准支出")
    assert approved is not None, msg
    assert funding_ledger.available_balance("东跨挑檐") == 1000.0

    signed, msg = ChangeOrderService().run_action(change_id, "签认变更")
    assert signed is not None, msg
    assert len(refund_rows()) == 1
    refund = refund_rows()[0]
    assert refund["退款金额"] == 700.0
    assert refund["对应工序"] == "东跨挑檐"
    assert refund["冲减回执"] == signed["冲减回执"]
    assert funding_ledger.available_balance("东跨挑檐") == 300.0

    # 财务凭冲减回执登记实际退款，双方对账一致。
    booked, msg = FundingUseService().run_action(refund["id"], "核准支出")
    assert booked is not None, msg
    assert refund["入账状态"] == "已入账"
    signed_now = store.find("change_order", change_id)
    assert signed_now["冲减状态"] == funding_ledger.OFFSET_BOOKED
    assert signed_now["冲减回执"] == refund["冲减回执"]
    reconciliation = funding_ledger.reconcile_process("东跨挑檐")
    assert reconciliation["signed_offset_amount"] == 700.0
    assert reconciliation["refunds"][0]["冲减回执"] == signed_now["冲减回执"]


# ── 并发：同工序两张减项签证只能各自占用真实余额 ──

def test_two_concurrent_negative_orders_respect_same_balance():
    prepare_process()
    add_funding(901, "东跨挑檐", 600, "已核准")
    first = raise_change(-500)
    second = raise_change(-500)

    results: list[tuple[int, bool, str]] = []

    def worker(change_id):
        entry, message = ChangeOrderService().run_action(change_id, "签认变更")
        results.append((change_id, entry is not None, message))

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(worker, [first, second]))

    successes = [item for item in results if item[1]]
    assert len(successes) == 1, f"余额600只应放行一张500减项: {results}"
    assert len(refund_rows()) == 1
    assert funding_ledger.available_balance("东跨挑檐") == 100.0


def test_concurrent_sign_and_funding_approval_share_one_balance():
    prepare_process()
    add_funding(901, "东跨挑檐", 600, "已核准")
    add_funding(902, "东跨挑檐", 400, "已提交")
    change_id = raise_change(-700)

    outcomes: dict[str, tuple[bool, str]] = {}

    def sign():
        entry, message = ChangeOrderService().run_action(change_id, "签认变更")
        outcomes["sign"] = (entry is not None, message)

    def approve():
        entry, message = FundingUseService().run_action(902, "核准支出")
        outcomes["approve"] = (entry is not None, message)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(sign), executor.submit(approve)]
        concurrent.futures.wait(futures)

    assert outcomes["approve"][0] is True
    # 两种串行顺序都成立：先核准则减项成功，先签认则余额不足被拒。
    assert funding_ledger.available_balance("东跨挑檐") in (300.0, 1000.0)
    assert len(refund_rows()) == (1 if outcomes["sign"][0] else 0)


# ── 幂等 ──

def test_duplicate_sign_deducts_once():
    prepare_process()
    add_funding(901, "东跨挑檐", 1000, "已核准")
    change_id = raise_change(-300)
    ChangeOrderService().run_action(change_id, "签认变更")

    entry, message = ChangeOrderService().run_action(change_id, "签认变更")
    assert entry is not None
    assert "已执行过" in message
    assert len(refund_rows()) == 1
    assert funding_ledger.available_balance("东跨挑檐") == 700.0


def test_concurrent_duplicate_sign_deducts_once():
    prepare_process()
    add_funding(901, "东跨挑檐", 1000, "已核准")
    change_id = raise_change(-300)

    def worker(_):
        return ChangeOrderService().run_action(change_id, "签认变更")

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(worker, range(5)))

    assert len(refund_rows()) == 1
    assert funding_ledger.available_balance("东跨挑檐") == 700.0


# ── 驳回与已入账退款 ──

def test_reject_releases_unbooked_offset():
    prepare_process()
    add_funding(901, "东跨挑檐", 1000, "已核准")
    change_id = raise_change(-400)
    ChangeOrderService().run_action(change_id, "签认变更")
    assert funding_ledger.available_balance("东跨挑檐") == 600.0

    ChangeOrderService().run_action(change_id, "驳回变更")
    entry = store.find("change_order", change_id)
    assert entry["冲减状态"] == funding_ledger.OFFSET_RELEASED
    assert entry["已冲减金额"] == 0
    assert refund_rows() == []
    assert funding_ledger.available_balance("东跨挑檐") == 1000.0


def test_reject_keeps_booked_refund_receipt():
    prepare_process()
    add_funding(901, "东跨挑檐", 1000, "已核准")
    change_id = raise_change(-400)
    ChangeOrderService().run_action(change_id, "签认变更")
    refund = refund_rows()[0]
    FundingUseService().run_action(refund["id"], "核准支出")

    ChangeOrderService().run_action(change_id, "驳回变更")
    entry = store.find("change_order", change_id)
    assert entry["冲减状态"] == funding_ledger.OFFSET_BOOKED
    # 已入账退款仍保留在支出明细，回执可追溯。
    kept = funding_ledger.find_refund_row(change_id)
    assert kept is not None
    assert kept["status"] == "已核准"
    assert kept["入账状态"] == "已入账"
    assert kept["冲减回执"]
    assert "保留原冲减回执" in kept["备注说明"]


def test_released_quota_can_be_signed_again_without_duplicate_row():
    prepare_process()
    add_funding(901, "东跨挑檐", 1000, "已核准")
    change_id = raise_change(-400)
    ChangeOrderService().run_action(change_id, "签认变更")
    ChangeOrderService().run_action(change_id, "驳回变更")

    signed, message = ChangeOrderService().run_action(change_id, "签认变更")
    assert signed is not None, message
    assert len(refund_rows()) == 1
    assert funding_ledger.available_balance("东跨挑檐") == 600.0


# ── 对账口径 ──

def test_reconcile_exposes_same_offset_for_signer_and_ledger():
    prepare_process()
    add_funding(901, "东跨挑檐", 600, "已核准")
    change_id = raise_change(-200)
    signed, msg = ChangeOrderService().run_action(change_id, "签认变更")
    assert signed is not None, msg

    reconciliation = funding_ledger.reconcile_process("东跨挑檐")
    assert reconciliation["approved_amount"] == 600.0
    assert reconciliation["signed_offset_amount"] == 200.0
    assert reconciliation["available_balance"] == 400.0
    assert reconciliation["change_orders"][0]["冲减回执"] == signed["冲减回执"]
    assert reconciliation["refunds"][0]["关联签证"] == change_id
