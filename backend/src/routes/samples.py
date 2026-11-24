from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import get_db
from src.models import FootfallSample,Stall,SettlementItem,WeeklySettlement
from src.schemas.market import SampleIn
from src.middlewares.auth import current_user,require_roles
from src.services.audit import record
from src.services.market import classify_sample,sample_dict
from src.services.attribution import is_current_sample
from src.services.settlement import claim_sample, week_start_of
router=APIRouter(prefix='/samples')
@router.get('')
def list_samples(status:str|None=None,db:Session=Depends(get_db),u=Depends(current_user)):
    q=select(FootfallSample).where(is_current_sample()).order_by(FootfallSample.sampled_at.desc(),FootfallSample.id.desc())
    if status:q=q.where(FootfallSample.status==status)
    rows=list(db.scalars(q.limit(250)))
    item_map={i.sample_id:i for i in db.scalars(select(SettlementItem).where(SettlementItem.sample_id.in_([s.id for s in rows])))} if rows else {}
    settlement_ids={i.settlement_id for i in item_map.values()}
    week_map={w.id:w for w in db.scalars(select(WeeklySettlement).where(WeeklySettlement.id.in_(settlement_ids)))} if settlement_ids else {}
    result=[]
    for s in rows:
        d=sample_dict(db,s)
        item=item_map.get(s.id)
        ws=week_start_of(s.sampled_at)
        d['business_week_start']=ws.date().isoformat()
        d['week_start']=ws.date().isoformat()
        if item is not None:
            target=week_map.get(item.settlement_id)
            d['settlement_id']=item.settlement_id
            d['entry_kind']=item.kind
            d['settlement_kind']=item.kind
            d['settlement_week_start']=target.week_start.date().isoformat() if target else None
            d['settlement_status']=target.status if target else None
            d['original_week_start']=item.original_week_start.date().isoformat() if item.original_week_start else ws.date().isoformat()
            d['settled']=True
        else:
            d['settlement_id']=None
            d['entry_kind']=None
            d['settlement_kind']=None
            d['settlement_week_start']=None
            d['settlement_status']=None
            d['original_week_start']=ws.date().isoformat()
            d['settled']=False
        result.append(d)
    return result
@router.post('',status_code=201)
def create_sample(p:SampleIn,db:Session=Depends(get_db),u=Depends(require_roles('admin','analyst'))):
    if not db.get(Stall,p.stall_id):raise HTTPException(404,'摊位不存在')
    data=p.model_dump(); data['sampled_at']=data['sampled_at'].replace(tzinfo=None); s=FootfallSample(**data);classify_sample(db,s);db.add(s);db.flush()
    # 若补录时样本已有效且其营业周已封账，同一事务内记入下一张未封周结的补账
    claim_sample(db,s)
    record(db,u.email,'sample.created',{'id':s.id,'status':s.status,'issue_reason':s.issue_reason});db.commit();db.refresh(s);return sample_dict(db,s)
