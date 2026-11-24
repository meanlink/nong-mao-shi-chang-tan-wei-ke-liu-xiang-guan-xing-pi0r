"""营业周封账 / 周结 / 补账领域实体。

一份有效客流（FootfallSample）在 settlement_items 中至多出现一次，
数据库唯一约束是“每份有效客流只进一张周结”的最终兜底。
"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.config.database import Base


class WeeklySettlement(Base):
    """一张营业周结单。open=未封周结（可收补账），closed=已封账（数字冻结）。"""

    __tablename__ = 'weekly_settlements'
    __table_args__ = (
        UniqueConstraint('week_start', name='uq_weekly_settlements_week_start'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    week_start: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    week_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    week_key: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(20), default='open', index=True)

    # 当周正账（本期客流）封账时冻结的合计
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    total_footfall: Mapped[int] = mapped_column(Integer, default=0)
    total_transactions: Mapped[int] = mapped_column(Integer, default=0)
    total_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    total_dwell_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    # 补账合计（按原营业周分组的明细在 items 中）
    supplement_count: Mapped[int] = mapped_column(Integer, default=0)
    supplement_footfall: Mapped[int] = mapped_column(Integer, default=0)
    supplement_transactions: Mapped[int] = mapped_column(Integer, default=0)
    supplement_revenue: Mapped[float] = mapped_column(Float, default=0.0)

    # 封账时的完整数字与采样依据快照（JSON 冗余，便于只读核对）
    totals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    basis_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)

    closed_by: Mapped[int | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    closed_by_name: Mapped[str] = mapped_column(String(120), default='')
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SettlementItem(Base):
    """周结明细。sample_id 全局唯一：一份有效客流只进一张周结。"""

    __tablename__ = 'settlement_items'
    __table_args__ = (
        UniqueConstraint('sample_id', name='uq_settlement_items_sample_id'),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[int] = mapped_column(
        ForeignKey('weekly_settlements.id'), index=True, nullable=False
    )
    sample_id: Mapped[int] = mapped_column(
        ForeignKey('footfall_samples.id'), index=True, nullable=False
    )
    # primary=封账时已有效的本周采样（本期/正账）；supplement=迟到记录补账
    kind: Mapped[str] = mapped_column(String(20), default='primary', index=True)
    # 采样自身所属营业周；补账时它与 settlement.week_start 不同，用于“按原营业周”分组
    original_week_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    original_week_key: Mapped[str] = mapped_column(String(16), default='')

    # —— 入账当时的采样快照（封账后即使源记录变化，周结仍保留当时数字与依据）——
    stall_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stall_code: Mapped[str] = mapped_column(String(30), default='')
    stall_name: Mapped[str] = mapped_column(String(100), default='')
    sampled_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    window_label: Mapped[str] = mapped_column(String(30), default='')
    footfall: Mapped[int] = mapped_column(Integer, default=0)
    transactions: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
    dwell_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    weather: Mapped[str] = mapped_column(String(30), default='')
    source: Mapped[str] = mapped_column(String(50), default='')
    status_at_entry: Mapped[str] = mapped_column(String(20), default='valid')
    notes: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SettlementGuard(Base):
    """单行互斥锁：封账与补录/更正入账在关键区串行化（MySQL 行锁生效）。"""

    __tablename__ = 'settlement_guard'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
