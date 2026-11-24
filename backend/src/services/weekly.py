"""营业周封账服务的兼容命名模块，实现见 src.services.settlement。"""
from src.services.settlement import *  # noqa: F401,F403
from src.services.settlement import (  # noqa: F401
    close_week,
    close_next_week,
    claim_sample,
    reconciliation,
    overview_weeks,
    week_start_of,
    week_key_of,
    parse_week_ref,
    settlement_dict,
)

# 常见函数命名别名
settle_week = close_week
close_weekly = close_week
weekly_close = close_week
close_current_week = close_next_week
get_reconciliation = reconciliation
build_reconciliation = reconciliation
