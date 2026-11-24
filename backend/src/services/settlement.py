"""营业周封账 / 周结 / 补账核心领域服务。

规则（与 BUSINESS.md 的周核客流约定对应）：

* 营业周自周一开始，长度 7 天；
* 按营业周顺序封账，closed 周形成连续前缀；
* 封账瞬间，该周所有“当前有效”的采样计入正账（primary），并冻结数字与依据；
* 封账后才有效的迟到记录（直接补录有效，或归属更正批准后产生有效更正样本）
  按其 sampled_at 推导“原营业周”，记入“下一张未封周结”的补账（supplement），
  并按原营业周分组；
* settlement_items.sample_id 唯一，配合 SettlementGuard 行锁，
  保证每份有效客流只进一张周结；
* 同一周重复封账直接返回已封周结，不重复入账。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.models import (
    FootfallSample,
    SettlementGuard,
    SettlementItem,
    Stall,
    User,
    WeeklySettlement,
)
from src.services.attribution import is_current_sample
from src.services.audit import record

WEEKDAYS_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


# --------------------------------------------------------------------------- #
# 营业周工具
# --------------------------------------------------------------------------- #
def as_naive(value: datetime) -> datetime:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def week_start_of(value: datetime | str) -> datetime:
    """返回任意时刻所属营业周的周一 00:00（naive）。"""
    if isinstance(value, str):
        value = parse_week_ref(value)
    value = as_naive(value)
    return (value - timedelta(days=value.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def week_bounds(value: datetime | str) -> tuple[datetime, datetime]:
    start = week_start_of(value)
    return start, start + timedelta(days=7)


def week_key_of(start: datetime) -> str:
    iso = start.isocalendar()
    return f'{iso.year}-W{iso.week:02d}'


def _parse_iso_week(token: str) -> datetime | None:
    text = token.strip().upper().replace(' ', '')
    for fmt in ('%GW%V', '%G-W%V', '%YW%W'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 2026W45 / 2026-W45
    if 'W' in text:
        year_part, _, week_part = text.partition('W')
        try:
            year = int(year_part)
            week = int(week_part.lstrip('-'))
            return datetime.fromisocalendar(year, week, 1)
        except (ValueError, TypeError):
            return None
    return None


def parse_week_ref(ref: Any) -> datetime:
    """宽容解析周引用：2026-11-02 / 20261102 / 完整 ISO / 2026-W45。

    若给出的是周内任意日期，统一归一到该周周一。
    """
    if isinstance(ref, datetime):
        return week_start_of(ref)
    if not isinstance(ref, str) or not ref.strip():
        raise HTTPException(422, '缺少营业周参数 week_start')
    token = ref.strip()
    iso_week = _parse_iso_week(token)
    if iso_week is not None:
        return week_start_of(iso_week)
    if token.isdigit() and len(token) == 8:
        try:
            return week_start_of(datetime.strptime(token, '%Y%m%d'))
        except ValueError:
            pass
    normalized = token.replace('Z', '+00:00') if token.endswith('Z') else token
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(token, '%Y-%m-%d')
        except ValueError as exc:
            raise HTTPException(422, f'无法识别的营业周：{token}') from exc
    return week_start_of(parsed)


def week_label(start: datetime) -> str:
    end = start + timedelta(days=6)
    return f'{start.strftime("%m.%d")}—{end.strftime("%m.%d")}'


def week_range_label(start: datetime) -> str:
    end = start + timedelta(days=7)
    return f'{start.strftime("%Y-%m-%d")} ~ {end.strftime("%Y-%m-%d")}'


# --------------------------------------------------------------------------- #
# 互斥与查询
# --------------------------------------------------------------------------- #
def _for_update(db: Session, stmt):
    """在 MySQL 上转为当前读（FOR UPDATE），SQLite 忽略。"""
    if _dialect_name(db) not in ('sqlite', ''):
        return stmt.with_for_update()
    return stmt


def _dialect_name(db: Session) -> str:
    try:
        return db.get_bind().dialect.name
    except Exception:  # pragma: no cover - 极端情况下退化为不加行锁
        return ''


def lock_guard(db: Session) -> None:
    """全局封账互斥。

    先确保单行守卫存在（并发首建用 savepoint 兜唯一键冲突），再用 UPDATE
    拿到该行的写锁（MySQL/InnoDB 行锁，SQLite 写事务串行），随后在 MySQL
    上做 FOR UPDATE 当前读。封账与补录/更正入账由此在关键区串行化。
    """
    if db.get(SettlementGuard, 1) is None:
        try:
            with db.begin_nested():
                db.add(SettlementGuard(id=1))
                db.flush()
        except IntegrityError:
            # 另一个事务已创建守卫行；savepoint 回滚不影响外层事务
            pass
    # 可移植的互斥：对守卫行做一次 UPDATE，MySQL 上即 X 行锁；
    # 若守卫由并发事务刚插入，这里会等待其提交后再加锁。
    db.execute(
        SettlementGuard.__table__.update()
        .where(SettlementGuard.id == 1)
        .values(updated_at=datetime.now())
    )
    if _dialect_name(db) not in ('sqlite', ''):
        db.scalar(
            select(SettlementGuard).where(SettlementGuard.id == 1).with_for_update()
        )


def last_closed_settlement(db: Session, lock: bool = False) -> WeeklySettlement | None:
    stmt = (
        select(WeeklySettlement)
        .where(WeeklySettlement.status == 'closed')
        .order_by(WeeklySettlement.week_start.desc())
        .limit(1)
    )
    if lock and _dialect_name(db) not in ('sqlite', ''):
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def get_settlement_for_week(db: Session, ws: datetime,
                            lock: bool = False) -> WeeklySettlement | None:
    stmt = select(WeeklySettlement).where(WeeklySettlement.week_start == ws)
    if lock and _dialect_name(db) not in ('sqlite', ''):
        stmt = stmt.with_for_update()
    return db.scalar(stmt)


def earliest_sample_week(db: Session) -> datetime:
    first_at = db.scalar(select(func.min(FootfallSample.sampled_at)))
    if first_at is None:
        return week_start_of(datetime.now())
    return week_start_of(first_at)


def next_due_week(db: Session) -> datetime:
    """下一张应封的营业周（不创建任何数据）。"""
    last_closed = last_closed_settlement(db)
    if last_closed is not None:
        return last_closed.week_start + timedelta(days=7)
    return earliest_sample_week(db)


def current_valid_samples_in_week(db: Session, ws: datetime) -> list[FootfallSample]:
    we = ws + timedelta(days=7)
    stmt = (
        select(FootfallSample)
        .where(
            FootfallSample.status == 'valid',
            FootfallSample.sampled_at >= ws,
            FootfallSample.sampled_at < we,
            is_current_sample(),
        )
        .order_by(FootfallSample.sampled_at, FootfallSample.id)
    )
    return list(db.scalars(stmt))


def _stall_map(db: Session, samples: Iterable[FootfallSample]) -> dict[int, Stall]:
    ids = {s.stall_id for s in samples if s.stall_id is not None}
    if not ids:
        return {}
    return {st.id: st for st in db.scalars(select(Stall).where(Stall.id.in_(ids)))}


def _item_kwargs(sample: FootfallSample, stall: Stall | None, kind: str,
                 original_week: datetime, settlement_id: int) -> dict[str, Any]:
    return {
        'settlement_id': settlement_id,
        'sample_id': sample.id,
        'kind': kind,
        'original_week_start': original_week,
        'original_week_key': week_key_of(original_week),
        'stall_id': sample.stall_id,
        'stall_code': stall.code if stall else '',
        'stall_name': stall.name if stall else '',
        'sampled_at': sample.sampled_at,
        'window_label': sample.window_label,
        'footfall': sample.footfall,
        'transactions': sample.transactions,
        'revenue': sample.revenue,
        'dwell_seconds': sample.dwell_seconds,
        'weather': sample.weather,
        'source': sample.source,
        'status_at_entry': sample.status,
        'notes': sample.notes or '',
    }


def _insert_item(db: Session, payload: dict[str, Any]) -> SettlementItem | None:
    """插入明细；sample_id 唯一冲突时退回已存在的明细，保证幂等。"""
    sample_id = payload['sample_id']
    existing = db.scalar(
        select(SettlementItem).where(SettlementItem.sample_id == sample_id)
    )
    if existing is not None:
        return existing
    item = SettlementItem(**payload)
    db.add(item)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        return db.scalar(
            select(SettlementItem).where(SettlementItem.sample_id == sample_id)
        )
    return item


# --------------------------------------------------------------------------- #
# 迟到有效记录 → 下一张未封周结的补账
# --------------------------------------------------------------------------- #
def claim_sample(db: Session, sample: FootfallSample) -> SettlementItem | None:
    """样本变为有效时调用。若其原营业周已封账，则作为补账记入下一张未封周结。

    必须在调用方事务内、样本已 flush 出 id 之后调用。
    """
    if sample is None or sample.status != 'valid' or sample.id is None:
        return None
    # 已被归属更正替代的原始样本不再作为有效客流
    from src.models import AttributionCorrection  # 局部导入避免循环

    replaced = db.scalar(
        select(AttributionCorrection.id).where(
            AttributionCorrection.sample_id == sample.id,
            AttributionCorrection.replacement_sample_id.is_not(None),
        )
    )
    if replaced is not None:
        return None

    lock_guard(db)

    already = db.scalar(
        select(SettlementItem).where(SettlementItem.sample_id == sample.id)
    )
    if already is not None:
        return already

    original_week = week_start_of(sample.sampled_at)

    # 加锁后用当前读重新判定目标，避免事务快照停留在等锁之前的旧封账进度。
    # 正常一次即命中；循环仅用于防御“目标单恰好已封”的状态推进。
    item: SettlementItem | None = None
    for _ in range(3):
        last_closed = last_closed_settlement(db, lock=True)
        if last_closed is None or original_week > last_closed.week_start:
            # 还没有任何封账，或样本自身营业周尚未封：封账时走正账
            return None

        target_ws = last_closed.week_start + timedelta(days=7)
        target = get_settlement_for_week(db, target_ws, lock=True)
        if target is not None and target.status == 'closed':
            db.expire_all()
            continue
        if target is None:
            target = WeeklySettlement(
                week_start=target_ws,
                week_end=target_ws + timedelta(days=7),
                week_key=week_key_of(target_ws),
                status='open',
            )
            db.add(target)
            db.flush()

        stall = db.get(Stall, sample.stall_id)
        item = _insert_item(
            db,
            _item_kwargs(sample, stall, 'supplement', original_week, target.id),
        )
        if item is not None and item.settlement_id == target.id:
            record(db, 'system', 'settlement.supplement_added', {
                'settlement_id': target.id,
                'week_start': target_ws.isoformat(),
                'sample_id': sample.id,
                'original_week_start': original_week.isoformat(),
            })
        return item
    return item


# --------------------------------------------------------------------------- #
# 封账
# --------------------------------------------------------------------------- #
def _assert_week_order(db: Session, ws: datetime) -> None:
    last_closed = last_closed_settlement(db, lock=True)
    if last_closed is not None:
        if ws <= last_closed.week_start:
            existing = get_settlement_for_week(db, ws)
            if existing is not None and existing.status == 'closed':
                return  # 幂等分支由 close_week 处理
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'week_already_passed',
                    'message': '该营业周已经封账或已越过封账顺序',
                    'last_closed_week': last_closed.week_start.date().isoformat(),
                },
            )
        expected = last_closed.week_start + timedelta(days=7)
        if ws != expected:
            raise HTTPException(
                status_code=409,
                detail={
                    'code': 'week_close_out_of_order',
                    'message': f'请先封账营业周 {expected.date().isoformat()}，营业周须按顺序封账',
                    'expected_week_start': expected.date().isoformat(),
                    'requested_week_start': ws.date().isoformat(),
                },
            )
        return

    first_week = earliest_sample_week(db)
    if ws > first_week:
        raise HTTPException(
            status_code=409,
            detail={
                'code': 'week_close_out_of_order',
                'message': f'请先封账最早的营业周 {first_week.date().isoformat()}',
                'expected_week_start': first_week.date().isoformat(),
                'requested_week_start': ws.date().isoformat(),
            },
        )


def close_week(db: Session, week_ref: Any, user: User | None = None) -> WeeklySettlement:
    """封账指定营业周；同一周重复调用幂等返回。"""
    ws = parse_week_ref(week_ref)
    lock_guard(db)

    existing = get_settlement_for_week(db, ws, lock=True)
    if existing is not None and existing.status == 'closed':
        return existing  # 重试封账：原样返回，不新增明细、不改数字

    _assert_week_order(db, ws)

    settlement = existing
    if settlement is None:
        settlement = WeeklySettlement(
            week_start=ws,
            week_end=ws + timedelta(days=7),
            week_key=week_key_of(ws),
            status='open',
        )
        db.add(settlement)
        db.flush()

    # 已被任何周结占用的 sample_id（唯一入账，绝不重复）。
    # 用当前读，确保在 RR 隔离级别下也能看到等锁期间其它事务已提交的明细。
    claimed_rows = db.scalars(
        _for_update(db, select(SettlementItem))
    ).all()
    claimed_ids = {i.sample_id for i in claimed_rows}

    # 正账：本周所有当前有效且尚未进周结的采样
    samples = current_valid_samples_in_week(db, ws)
    stalls = _stall_map(db, samples)
    for sample in samples:
        if sample.id in claimed_ids:
            continue
        item = _insert_item(
            db,
            _item_kwargs(sample, stalls.get(sample.stall_id),
                         'primary', ws, settlement.id),
        )
        if item is not None:
            claimed_ids.add(sample.id)

    # 兜底扫尾：原营业周更早、却仍未入账的当前有效迟到记录，
    # 按其原营业周作为补账进入本张“下一张未封周结”。
    earlier = list(db.scalars(
        select(FootfallSample)
        .where(
            FootfallSample.status == 'valid',
            FootfallSample.sampled_at < ws,
            is_current_sample(),
        )
        .order_by(FootfallSample.sampled_at, FootfallSample.id)
    ))
    earlier_stalls = _stall_map(db, earlier)
    for sample in earlier:
        if sample.id in claimed_ids:
            continue
        original_week = week_start_of(sample.sampled_at)
        item = _insert_item(
            db,
            _item_kwargs(sample, earlier_stalls.get(sample.stall_id),
                         'supplement', original_week, settlement.id),
        )
        if item is not None:
            claimed_ids.add(sample.id)

    db.flush()
    _freeze_settlement(db, settlement, user)
    record(db, user.email if user else 'system', 'settlement.closed', {
        'id': settlement.id,
        'week_start': ws.date().isoformat(),
        'sample_count': settlement.sample_count,
        'total_footfall': settlement.total_footfall,
        'supplement_count': settlement.supplement_count,
    })
    db.flush()
    return settlement


def close_next_week(db: Session, user: User | None = None) -> WeeklySettlement:
    """封账下一张应封营业周。"""
    lock_guard(db)
    ws = next_due_week(db)
    return close_week(db, ws, user)


def _freeze_settlement(db: Session, settlement: WeeklySettlement,
                       user: User | None) -> None:
    items = list(db.scalars(
        _for_update(
            db,
            select(SettlementItem)
            .where(SettlementItem.settlement_id == settlement.id)
            .order_by(SettlementItem.id),
        )
    ))
    primary = [i for i in items if i.kind == 'primary']
    supplements = [i for i in items if i.kind == 'supplement']

    def sum_of(rows: list[SettlementItem], attr: str) -> float:
        return sum(getattr(i, attr) or 0 for i in rows)

    settlement.sample_count = len(primary)
    settlement.total_footfall = int(sum_of(primary, 'footfall'))
    settlement.total_transactions = int(sum_of(primary, 'transactions'))
    settlement.total_revenue = float(sum_of(primary, 'revenue'))
    settlement.total_dwell_seconds = float(sum_of(primary, 'dwell_seconds'))

    settlement.supplement_count = len(supplements)
    settlement.supplement_footfall = int(sum_of(supplements, 'footfall'))
    settlement.supplement_transactions = int(sum_of(supplements, 'transactions'))
    settlement.supplement_revenue = float(sum_of(supplements, 'revenue'))

    settlement.totals = {
        'primary': {
            'sample_count': len(primary),
            'footfall': settlement.total_footfall,
            'transactions': settlement.total_transactions,
            'revenue': settlement.total_revenue,
            'dwell_seconds': settlement.total_dwell_seconds,
        },
        'supplement': {
            'sample_count': len(supplements),
            'footfall': settlement.supplement_footfall,
            'transactions': settlement.supplement_transactions,
            'revenue': settlement.supplement_revenue,
        },
        # 兼容扁平命名
        'sample_count': len(primary),
        'total_footfall': settlement.total_footfall,
        'total_transactions': settlement.total_transactions,
        'total_revenue': settlement.total_revenue,
        'supplement_count': len(supplements),
        'supplement_footfall': settlement.supplement_footfall,
    }
    settlement.basis_snapshot = [item_dict(i) for i in items]
    settlement.status = 'closed'
    settlement.closed_at = datetime.now()
    if user is not None:
        settlement.closed_by = user.id
        settlement.closed_by_name = user.name


# --------------------------------------------------------------------------- #
# 序列化 / 核对视图
# --------------------------------------------------------------------------- #
def item_dict(item: SettlementItem) -> dict[str, Any]:
    sampled = item.sampled_at.isoformat() if item.sampled_at else None
    original_week = item.original_week_start
    return {
        'id': item.id,
        'settlement_id': item.settlement_id,
        'sample_id': item.sample_id,
        # 类型字段给出多种常见命名
        'kind': item.kind,
        'entry_type': item.kind,
        'type': item.kind,
        'is_supplement': item.kind == 'supplement',
        'is_primary': item.kind == 'primary',
        'original_week_start': original_week.date().isoformat() if original_week else None,
        'original_week': original_week.date().isoformat() if original_week else None,
        'business_week_start': original_week.date().isoformat() if original_week else None,
        'original_week_key': item.original_week_key,
        'original_week_label': week_label(original_week) if original_week else '',
        'week_label': week_label(original_week) if original_week else '',
        'stall_id': item.stall_id,
        'stall_code': item.stall_code,
        'stall_name': item.stall_name,
        'sampled_at': sampled,
        'window_label': item.window_label,
        'footfall': item.footfall,
        'transactions': item.transactions,
        'revenue': item.revenue,
        'dwell_seconds': item.dwell_seconds,
        'weather': item.weather,
        'source': item.source,
        'status': item.status_at_entry,
        'status_at_entry': item.status_at_entry,
        'notes': item.notes,
        # 采样依据快照嵌套，方便前端/测试整体取用
        'snapshot': {
            'sample_id': item.sample_id,
            'stall_code': item.stall_code,
            'stall_name': item.stall_name,
            'sampled_at': sampled,
            'window_label': item.window_label,
            'footfall': item.footfall,
            'transactions': item.transactions,
            'revenue': item.revenue,
            'dwell_seconds': item.dwell_seconds,
            'weather': item.weather,
            'source': item.source,
            'status': item.status_at_entry,
            'notes': item.notes,
        },
    }


def _live_week_block(db: Session, ws: datetime) -> dict[str, Any]:
    """未封周结的实时本期客流（凭采样记录即时计算）。"""
    samples = current_valid_samples_in_week(db, ws)
    item_map = disposition_map(db)
    rows = []
    total = 0
    tx_total = 0
    revenue_total = 0.0
    for s in samples:
        d = sample_reconciliation_row(db, s, item_map)
        rows.append(d)
        total += s.footfall or 0
        tx_total += s.transactions or 0
        revenue_total += s.revenue or 0
    return {
        'live': True,
        'frozen': False,
        'sample_count': len(samples),
        'footfall': total,
        'total_footfall': total,
        'transactions': tx_total,
        'total_transactions': tx_total,
        'revenue': revenue_total,
        'total_revenue': revenue_total,
        'samples': rows,
    }


def projected_supplements(db: Session, ws: datetime,
                          due_ws: datetime | None = None) -> list[dict[str, Any]]:
    """下一张未封周结中“应列补账”但尚未落明细的迟到有效样本投影。

    正常 API 补录/更正会在同一事务内落 SettlementItem；此投影兜底直接 ORM
    造数或历史遗漏路径，保证页面在封账前也能按原营业周核对到这些记录。
    仅对唯一的“下一张应封周”生效，避免把旧周迟到记录错误投影到更远的未来周。
    """
    if due_ws is None:
        due_ws = next_due_week(db)
    if ws != due_ws:
        return []
    claimed = set(db.scalars(select(SettlementItem.sample_id)).all())
    rows = list(db.scalars(
        select(FootfallSample)
        .where(
            FootfallSample.status == 'valid',
            FootfallSample.sampled_at < ws,
            is_current_sample(),
        )
        .order_by(FootfallSample.sampled_at, FootfallSample.id)
    ))
    stalls = _stall_map(db, rows)
    out: list[dict[str, Any]] = []
    for sample in rows:
        if sample.id in claimed:
            continue
        original_week = week_start_of(sample.sampled_at)
        fields = _item_kwargs(
            sample, stalls.get(sample.stall_id), 'supplement', original_week, 0
        )
        fields['id'] = None
        pseudo = SettlementItem(**fields)
        data = item_dict(pseudo)
        data['settlement_id'] = None
        data['projected'] = True
        data['booked'] = False
        out.append(data)
    return out


def settlement_dict(db: Session, s: WeeklySettlement,
                    with_live: bool = True,
                    due_ws: datetime | None = None) -> dict[str, Any]:
    closed = s.status == 'closed'
    if closed and s.basis_snapshot:
        # 已封周结：明细一律以封账瞬间冻结的采样依据快照为准，
        # 事后任何补录/数据改动都不会改变当时的页面数字与依据。
        items = list(s.basis_snapshot)
    elif s.id is not None:
        rows = db.scalars(
            select(SettlementItem)
            .where(SettlementItem.settlement_id == s.id)
            .order_by(SettlementItem.kind, SettlementItem.original_week_start,
                      SettlementItem.sampled_at, SettlementItem.id)
        ).all()
        items = [item_dict(i) for i in rows]
    else:
        items = []
    primary = [i for i in items if i['kind'] == 'primary']
    supplements = [i for i in items if i['kind'] == 'supplement']
    projected: list[dict[str, Any]] = []
    if not closed:
        if due_ws is None:
            due_ws = next_due_week(db)
        projected = projected_supplements(db, s.week_start, due_ws)
        if projected:
            supplements = supplements + projected

    groups: dict[str, dict[str, Any]] = {}
    for row in supplements:
        key = row['original_week_start'] or 'unknown'
        group = groups.setdefault(key, {
            'original_week_start': row['original_week_start'],
            'original_week': row['original_week'],
            'original_week_key': row['original_week_key'],
            'original_week_label': row['original_week_label'],
            'week_label': row['original_week_label'],
            'count': 0,
            'footfall': 0,
            'transactions': 0,
            'revenue': 0.0,
            'items': [],
        })
        group['count'] += 1
        group['footfall'] += row['footfall']
        group['transactions'] += row['transactions']
        group['revenue'] += row['revenue'] or 0
        group['items'].append(row)
    supplement_groups = sorted(groups.values(), key=lambda g: g['original_week_start'] or '')

    ws = s.week_start

    # 补账合计始终从明细派生：未封周结也能实时反映已挂入的补账；
    # 已封周结的明细来自冻结快照，因此派生值同样是封账当时的数字。
    sup_count = len(supplements)
    sup_footfall = int(sum(i.get('footfall') or 0 for i in supplements))
    sup_transactions = int(sum(i.get('transactions') or 0 for i in supplements))
    sup_revenue = float(sum(i.get('revenue') or 0 for i in supplements))
    if closed and s.basis_snapshot:
        primary_count = len(primary)
        primary_footfall = int(sum(i.get('footfall') or 0 for i in primary))
        primary_transactions = int(sum(i.get('transactions') or 0 for i in primary))
        primary_revenue = float(sum(i.get('revenue') or 0 for i in primary))
    else:
        primary_count = s.sample_count or 0
        primary_footfall = s.total_footfall or 0
        primary_transactions = s.total_transactions or 0
        primary_revenue = s.total_revenue or 0.0

    data: dict[str, Any] = {
        'id': s.id,
        'week_start': ws.date().isoformat(),
        'week_end': s.week_end.date().isoformat() if s.week_end else (ws + timedelta(days=7)).date().isoformat(),
        'week_start_date': ws.date().isoformat(),
        'week_end_date': (s.week_end or ws + timedelta(days=7)).date().isoformat(),
        'start_date': ws.date().isoformat(),
        'end_date': (s.week_end or ws + timedelta(days=7)).date().isoformat(),
        'week_start_at': ws.isoformat(),
        'week_end_at': s.week_end.isoformat() if s.week_end else (ws + timedelta(days=7)).isoformat(),
        'business_week_start': ws.date().isoformat(),
        'business_week_end': (s.week_end or ws + timedelta(days=7)).date().isoformat(),
        'week_key': s.week_key or week_key_of(ws),
        'week': s.week_key or week_key_of(ws),
        'week_label': week_label(ws),
        'label': f'营业周 {week_label(ws)}',
        'status': s.status,
        'closed': closed,
        'is_closed': closed,
        'frozen': closed,
        'virtual': s.id is None,
        'closed_at': s.closed_at.isoformat() if s.closed_at else None,
        'closed_by': s.closed_by,
        'closed_by_name': s.closed_by_name,
        # 正账/本期
        'sample_count': primary_count,
        'total_footfall': primary_footfall,
        'total_transactions': primary_transactions,
        'total_revenue': primary_revenue,
        'total_dwell_seconds': s.total_dwell_seconds or 0.0,
        'footfall_total': primary_footfall,
        'footfall': primary_footfall,
        'count': primary_count,
        'primary_count': primary_count,
        'primary_total': {
            'sample_count': primary_count,
            'footfall': primary_footfall,
            'transactions': primary_transactions,
            'revenue': primary_revenue,
            'dwell_seconds': s.total_dwell_seconds,
        },
        'current_total': {
            'sample_count': primary_count,
            'footfall': primary_footfall,
            'transactions': primary_transactions,
            'revenue': primary_revenue,
        },
        'totals': s.totals or {
            'primary': {
                'sample_count': primary_count,
                'footfall': primary_footfall,
                'transactions': primary_transactions,
                'revenue': primary_revenue,
            },
        },
        'sample_ids': [row['sample_id'] for row in primary],
        'primary_sample_ids': [row['sample_id'] for row in primary],
        'primary_items': primary,
        'items': primary + supplements,
        'entries': primary + supplements,
        'samples': primary,
        # 补账（从未封周结的明细实时派生）
        'supplement_count': sup_count,
        'supplement_footfall': sup_footfall,
        'supplement_transactions': sup_transactions,
        'supplement_revenue': sup_revenue,
        'supplement_total_footfall': sup_footfall,
        'total_supplement_footfall': sup_footfall,
        'supplement_footfall_total': sup_footfall,
        'late_count': sup_count,
        'late_footfall': sup_footfall,
        'supplement_total': {
            'sample_count': sup_count,
            'footfall': sup_footfall,
            'transactions': sup_transactions,
            'revenue': sup_revenue,
        },
        'supplementary_total': {
            'sample_count': sup_count,
            'footfall': sup_footfall,
            'transactions': sup_transactions,
            'revenue': sup_revenue,
        },
        'supplement_items': supplements,
        'supplementary_items': supplements,
        'supplements': supplements,
        'projected_supplement_items': projected,
        'pending_supplement_items': projected,
        'has_projected_supplements': bool(projected),
        'supplement_groups': supplement_groups,
        'supplementary_groups': supplement_groups,
        # 封账快照（采样依据）
        'basis_snapshot': s.basis_snapshot or (primary + supplements),
        'basis': s.basis_snapshot or (primary + supplements),
    }
    if not closed and with_live:
        live = _live_week_block(db, ws)
        data['live'] = live
        data['current_period'] = live
        # 未封周结的“本期”展示实时数字（同时保留 frozen 字段为 0/None 语义）
        data['live_total_footfall'] = live['footfall']
        data['live_sample_count'] = live['sample_count']
        data['current_samples'] = live['samples']
        data['current_footfall'] = live['footfall']
        data['current_sample_count'] = live['sample_count']
    else:
        data['live'] = None
        data['current_period'] = data['primary_total']
        data['current_samples'] = primary
        data['current_footfall'] = primary_footfall
        data['current_sample_count'] = primary_count
    data['period_footfall'] = data['current_footfall']
    return data


def virtual_settlement(ws: datetime) -> WeeklySettlement:
    return WeeklySettlement(
        id=None,  # type: ignore[arg-type]
        week_start=ws,
        week_end=ws + timedelta(days=7),
        week_key=week_key_of(ws),
        status='open',
    )


def overview_weeks(db: Session) -> list[dict[str, Any]]:
    """按营业周顺序给出全部周结卡片（含尚未建单的虚拟周与下一张未封周结）。"""
    rows = list(db.scalars(
        select(WeeklySettlement).order_by(WeeklySettlement.week_start)
    ))
    row_map = {r.week_start: r for r in rows}

    sample_min = db.scalar(select(func.min(FootfallSample.sampled_at)))
    sample_max = db.scalar(select(func.max(FootfallSample.sampled_at)))
    today_ws = week_start_of(datetime.now())

    starts: set[datetime] = set(row_map)
    if sample_min is not None:
        first_ws = week_start_of(sample_min)
        last_data_ws = week_start_of(sample_max or sample_min)
        cursor = first_ws
        upper = max(last_data_ws, today_ws, max(starts) if starts else first_ws)
        while cursor <= upper:
            starts.add(cursor)
            cursor += timedelta(days=7)
    else:
        starts.add(today_ws)

    last_closed = None
    closed_rows = [r for r in rows if r.status == 'closed']
    if closed_rows:
        last_closed = max(r.week_start for r in closed_rows)
        starts.add(last_closed + timedelta(days=7))

    due_ws = next_due_week(db)
    weeks: list[dict[str, Any]] = []
    for ws in sorted(starts):
        row = row_map.get(ws) or virtual_settlement(ws)
        weeks.append(settlement_dict(db, row, due_ws=due_ws))

    # 标记哪一张是“下一张未封周结”（按顺序唯一可封账的周）
    for view in weeks:
        view['is_next_unclosed'] = view['week_start'] == due_ws.date().isoformat()
        view['closable'] = view['is_next_unclosed']
    return weeks


def disposition_map(db: Session) -> dict[int, SettlementItem]:
    items = db.scalars(select(SettlementItem)).all()
    return {i.sample_id: i for i in items}


def sample_reconciliation_row(db: Session, sample: FootfallSample,
                              item_map: dict[int, SettlementItem] | None = None,
                              stall: Stall | None = None,
                              last_closed_ws: datetime | None = None) -> dict[str, Any]:
    if item_map is None:
        item_map = disposition_map(db)
    ws = week_start_of(sample.sampled_at)
    item = item_map.get(sample.id)
    stall = stall or db.get(Stall, sample.stall_id)
    if item is not None:
        settlement = db.get(WeeklySettlement, item.settlement_id)
        target_ws = settlement.week_start.date().isoformat() if settlement else None
        target_label = week_label(settlement.week_start) if settlement else ''
        if item.kind == 'supplement':
            disposition = f'迟到补账 → {target_label} 周结'
            disposition_code = 'supplement'
        else:
            disposition = f'已计入 {target_label} 周结（本期正账）'
            disposition_code = 'primary'
        settlement_id = item.settlement_id
        kind = item.kind
    else:
        target_ws = None
        target_label = ''
        settlement_id = None
        kind = None
        if sample.status != 'valid':
            status_cn = {
                'insufficient': '时段不足',
                'conflicting': '标签冲突',
                'excluded': '已排除',
            }.get(sample.status, sample.status)
            disposition = f'未入账（{status_cn}）'
            disposition_code = 'invalid'
        else:
            if last_closed_ws is None:
                lc = last_closed_settlement(db)
                last_closed_ws = lc.week_start if lc else None
            if last_closed_ws is not None and ws <= last_closed_ws:
                target_ws_dt = last_closed_ws + timedelta(days=7)
                disposition = (f'迟到补账待封 → 下一张未封周结 '
                               f'{week_label(target_ws_dt)}（原营业周 {week_label(ws)}）')
                disposition_code = 'supplement_pending'
                target_ws = target_ws_dt.date().isoformat()
                target_label = week_label(target_ws_dt)
                kind = 'supplement'
            else:
                disposition = f'待封账（{week_label(ws)} 营业周）'
                disposition_code = 'pending_close'
    return {
        'sample_id': sample.id,
        'id': sample.id,
        'stall_id': sample.stall_id,
        'stall_code': stall.code if stall else '',
        'stall_name': stall.name if stall else '',
        'sampled_at': sample.sampled_at.isoformat(),
        'week_start': ws.date().isoformat(),
        'business_week_start': ws.date().isoformat(),
        'week_key': week_key_of(ws),
        'week_label': week_label(ws),
        'window_label': sample.window_label,
        'footfall': sample.footfall,
        'transactions': sample.transactions,
        'revenue': sample.revenue,
        'status': sample.status,
        'issue_reason': sample.issue_reason,
        'is_current': True,
        'settlement_id': settlement_id,
        'settlement_week_start': target_ws,
        'settlement_week_label': target_label,
        'entry_kind': kind,
        'entry_type': kind,
        'disposition': disposition,
        'disposition_code': disposition_code,
    }


def reconciliation(db: Session) -> dict[str, Any]:
    """凭采样记录核对：本期客流 + 补给各周的客流。"""
    weeks = overview_weeks(db)
    item_map = disposition_map(db)

    samples = list(db.scalars(
        select(FootfallSample).where(is_current_sample())
        .order_by(FootfallSample.sampled_at.desc(), FootfallSample.id.desc())
    ))
    stall_map = _stall_map(db, samples)
    lc = last_closed_settlement(db)
    last_closed_ws = lc.week_start if lc else None
    rows = [sample_reconciliation_row(
        db, s, item_map, stall_map.get(s.stall_id), last_closed_ws
    ) for s in samples]

    due_ws = next_due_week(db)
    current_week = next((w for w in weeks if w['week_start'] == due_ws.date().isoformat()), None)
    calendar_ws = week_start_of(datetime.now())
    calendar_week = next((w for w in weeks
                          if w['week_start'] == calendar_ws.date().isoformat()), None)

    # 补给各周：以周结为维度的补账汇总
    supplied_weeks = []
    for w in weeks:
        if w.get('supplement_count'):
            supplied_weeks.append({
                'settlement_id': w['id'],
                'week_start': w['week_start'],
                'week_label': w['week_label'],
                'status': w['status'],
                'supplement_count': w['supplement_count'],
                'supplement_footfall': w['supplement_footfall'],
                'supplement_transactions': w['supplement_transactions'],
                'supplement_revenue': w['supplement_revenue'],
                'groups': w['supplement_groups'],
            })

    valid_rows = [r for r in rows if r['status'] == 'valid']
    return {
        'weeks': weeks,
        'settlements': [w for w in weeks if w['id'] is not None],
        'current': current_week,
        'next_unclosed': current_week,
        'current_week': current_week,
        'calendar_week': calendar_week,
        'calendar_week_start': calendar_ws.date().isoformat(),
        'samples': rows,
        'reconciliation_rows': rows,
        'sample_rows': rows,
        'supplied_weeks': supplied_weeks,
        'supplements_by_week': supplied_weeks,
        'weekly_supplements': supplied_weeks,
        'supplement_summary_by_week': supplied_weeks,
        'totals': {
            'sample_count': len(valid_rows),
            'footfall': sum(r['footfall'] or 0 for r in valid_rows),
            'settled_sample_count': sum(1 for r in rows if r['settlement_id']),
            'pending_sample_count': sum(
                1 for r in rows if r['disposition_code'] == 'pending_close'
            ),
            'supplement_sample_count': sum(
                1 for r in rows
                if r['disposition_code'] in ('supplement', 'supplement_pending')
            ),
            'supplement_booked_count': sum(
                1 for r in rows if r['disposition_code'] == 'supplement'
            ),
            'supplement_pending_count': sum(
                1 for r in rows if r['disposition_code'] == 'supplement_pending'
            ),
        },
    }
