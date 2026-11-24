from src.config.database import Base,engine,Session
from src.models import User,Stall,CampaignWindow,FootfallSample,AnalysisRun,Brief,AuditLog,WeeklySettlement,SettlementItem,SettlementGuard
def migrate():
    Base.metadata.create_all(bind=engine)
    # 封账互斥守卫单行，确保并发首封时该行已存在
    with Session() as db:
        if db.get(SettlementGuard,1) is None:
            db.add(SettlementGuard(id=1)); db.commit()
