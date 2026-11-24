from .user import User
from .market import Stall,CampaignWindow,FootfallSample,AnalysisRun,Brief,AuditLog
from .attribution import AttributionCorrection
from .settlement import WeeklySettlement,SettlementItem,SettlementGuard

# 命名别名，兼容不同的领域命名习惯
WeeklyClose = WeeklySettlement
WeekSettlement = WeeklySettlement
Settlement = WeeklySettlement
WeeklySettlementEntry = SettlementItem
SettlementEntry = SettlementItem
WeekCloseItem = SettlementItem

__all__ = [
    'User', 'Stall', 'CampaignWindow', 'FootfallSample', 'AnalysisRun', 'Brief',
    'AuditLog', 'AttributionCorrection', 'WeeklySettlement', 'SettlementItem',
    'SettlementGuard', 'WeeklyClose', 'WeekSettlement', 'Settlement',
]
