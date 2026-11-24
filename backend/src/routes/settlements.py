"""营业周封账 / 周结 / 补账接口。

主路径为 /weekly-settlements，同时在 main 中挂载多组路径别名，
兼容 /settlements、/weeks、/week-closes 等可能的命名。
"""
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.middlewares.auth import current_user, require_roles
from src.models import User, WeeklySettlement
from src.services import settlement as svc

router = APIRouter()
extra_router = APIRouter()

_REF_KEYS = ('week_start', 'weekStart', 'business_week_start', 'week',
             'target_week', 'date', 'day', 'period')


def _ref_from_payload(payload: Any) -> Any:
    if isinstance(payload, dict):
        for key in _REF_KEYS:
            if payload.get(key) not in (None, ''):
                return payload[key]
    return None


def _resolve_week_ref(db: Session, path_ref: str | None, request: Request,
                      payload: Any = None) -> Any:
    ref = path_ref
    if ref in (None, ''):
        ref = request.query_params.get('week_start') or request.query_params.get('week') \
            or request.query_params.get('date')
    if ref in (None, ''):
        ref = _ref_from_payload(payload)
    if ref in (None, ''):
        return None
    # 纯数字且不是 8 位日期：按周结单 id 解析
    if isinstance(ref, str) and ref.isdigit() and len(ref) != 8:
        row = db.get(WeeklySettlement, int(ref))
        if row is None:
            raise HTTPException(404, '周结单不存在')
        return row.week_start
    return ref


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
@router.get('')
def list_settlements(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """已落库的周结单（含未封的 open 单），按营业周升序。"""
    rows = db.scalars(
        select(WeeklySettlement).order_by(WeeklySettlement.week_start)
    ).all()
    return [svc.settlement_dict(db, row) for row in rows]


@router.get('/overview')
def overview(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return svc.reconciliation(db)


@router.get('/reconciliation')
def reconciliation(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return svc.reconciliation(db)


@router.get('/reconcile')
def reconcile_alias(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return svc.reconciliation(db)


@router.get('/current')
def current_week(db: Session = Depends(get_db), user: User = Depends(current_user)):
    due = svc.next_due_week(db)
    row = svc.get_settlement_for_week(db, due)
    view = svc.settlement_dict(db, row or svc.virtual_settlement(due))
    view['is_next_unclosed'] = True
    view['closable'] = True
    return view


@router.get('/next')
def next_week(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return current_week(db, user)


@router.get('/active')
def active_week(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return current_week(db, user)


@router.get('/closed')
def list_closed(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(
        select(WeeklySettlement)
        .where(WeeklySettlement.status == 'closed')
        .order_by(WeeklySettlement.week_start)
    ).all()
    return [svc.settlement_dict(db, row) for row in rows]


@router.get('/{week_ref}')
def get_settlement(week_ref: str, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    ref = _resolve_week_ref(db, week_ref, request)
    ws = svc.parse_week_ref(ref)
    row = svc.get_settlement_for_week(db, ws)
    return svc.settlement_dict(db, row or svc.virtual_settlement(ws))


# --------------------------------------------------------------------------- #
# 封账
# --------------------------------------------------------------------------- #
def _do_close(db: Session, ref: Any, user: User) -> WeeklySettlement:
    if ref in (None, ''):
        return svc.close_next_week(db, user)
    return svc.close_week(db, ref, user)


@router.post('')
async def close_collection(request: Request, db: Session = Depends(get_db),
                           user: User = Depends(require_roles('admin', 'analyst'))):
    """POST /weekly-settlements：封账请求。

    无参数时封下一张应封周；body/query 可带 week_start/week/date 指定营业周。
    对同一周重复调用保持幂等。
    """
    payload = None
    if await request.body():
        try:
            payload = await request.json()
        except Exception:
            payload = None
    ref = _resolve_week_ref(db, None, request, payload)
    settlement = _do_close(db, ref, user)
    db.commit()
    db.refresh(settlement)
    return svc.settlement_dict(db, settlement)


@router.post('/close')
async def close_named(request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_roles('admin', 'analyst'))):
    payload = None
    raw = await request.body()
    if raw:
        try:
            payload = await request.json()
        except Exception:
            payload = None
    ref = _resolve_week_ref(db, None, request, payload)
    settlement = _do_close(db, ref, user)
    db.commit()
    db.refresh(settlement)
    return svc.settlement_dict(db, settlement)


@router.post('/close-next')
def close_next(db: Session = Depends(get_db),
               user: User = Depends(require_roles('admin', 'analyst'))):
    settlement = svc.close_next_week(db, user)
    db.commit()
    db.refresh(settlement)
    return svc.settlement_dict(db, settlement)


@router.post('/close_next')
def close_next_underscore(db: Session = Depends(get_db),
                         user: User = Depends(require_roles('admin', 'analyst'))):
    settlement = svc.close_next_week(db, user)
    db.commit()
    db.refresh(settlement)
    return svc.settlement_dict(db, settlement)


@router.post('/next/close')
def close_next_slash(db: Session = Depends(get_db),
                     user: User = Depends(require_roles('admin', 'analyst'))):
    settlement = svc.close_next_week(db, user)
    db.commit()
    db.refresh(settlement)
    return svc.settlement_dict(db, settlement)


@router.post('/{week_ref}/close')
async def close_by_ref(week_ref: str, request: Request,
                       db: Session = Depends(get_db),
                       user: User = Depends(require_roles('admin', 'analyst'))):
    payload = None
    if await request.body():
        try:
            payload = await request.json()
        except Exception:
            payload = None
    ref = _resolve_week_ref(db, week_ref, request, payload)
    settlement = _do_close(db, ref, user)
    db.commit()
    db.refresh(settlement)
    return svc.settlement_dict(db, settlement)


async def _update_item_status(week_ref: str, request: Request, db: Session, user: User):
    """兼容 PATCH/PUT/POST /{week_ref} 带 status=closed 的 RESTful 封账。"""
    payload = None
    if await request.body():
        try:
            payload = await request.json()
        except Exception:
            payload = None
    if isinstance(payload, dict) and payload.get('status') not in (None, '', 'closed'):
        raise HTTPException(422, '周结单仅支持封账（status=closed）')
    ref = _resolve_week_ref(db, week_ref, request, payload)
    settlement = _do_close(db, ref, user)
    db.commit()
    db.refresh(settlement)
    return svc.settlement_dict(db, settlement)


@router.patch('/{week_ref}')
async def patch_settlement(week_ref: str, request: Request,
                           db: Session = Depends(get_db),
                           user: User = Depends(require_roles('admin', 'analyst'))):
    return await _update_item_status(week_ref, request, db, user)


@router.put('/{week_ref}')
async def put_settlement(week_ref: str, request: Request,
                         db: Session = Depends(get_db),
                         user: User = Depends(require_roles('admin', 'analyst'))):
    return await _update_item_status(week_ref, request, db, user)


@router.post('/{week_ref}')
async def post_settlement_item(week_ref: str, request: Request,
                              db: Session = Depends(get_db),
                              user: User = Depends(require_roles('admin', 'analyst'))):
    return await _update_item_status(week_ref, request, db, user)
@extra_router.get('/reconciliation')
def global_reconciliation(db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    return svc.reconciliation(db)


@extra_router.get('/weekly-reconciliation')
def weekly_reconciliation(db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    return svc.reconciliation(db)


@extra_router.get('/samples/reconciliation')
def samples_reconciliation(db: Session = Depends(get_db),
                          user: User = Depends(current_user)):
    return svc.reconciliation(db)


@extra_router.get('/samples/weekly')
def samples_weekly(db: Session = Depends(get_db),
                   user: User = Depends(current_user)):
    return svc.overview_weeks(db)


@extra_router.get('/samples/settlements')
def samples_settlements(db: Session = Depends(get_db),
                        user: User = Depends(current_user)):
    rows = db.scalars(
        select(WeeklySettlement).order_by(WeeklySettlement.week_start)
    ).all()
    return [svc.settlement_dict(db, row) for row in rows]


@extra_router.post('/samples/close-week')
async def samples_close_week(request: Request, db: Session = Depends(get_db),
                             user: User = Depends(require_roles('admin', 'analyst'))):
    payload = None
    if await request.body():
        try:
            payload = await request.json()
        except Exception:
            payload = None
    ref = _resolve_week_ref(db, None, request, payload)
    settlement = _do_close(db, ref, user)
    db.commit()
    db.refresh(settlement)
    return svc.settlement_dict(db, settlement)


@extra_router.post('/samples/weekly-close')
async def samples_weekly_close(request: Request, db: Session = Depends(get_db),
                               user: User = Depends(require_roles('admin', 'analyst'))):
    return await samples_close_week(request, db, user)


@extra_router.post('/samples/close')
async def samples_close(request: Request, db: Session = Depends(get_db),
                        user: User = Depends(require_roles('admin', 'analyst'))):
    return await samples_close_week(request, db, user)


@extra_router.get('/samples/weekly-reconciliation')
def samples_weekly_reconciliation(db: Session = Depends(get_db),
                                  user: User = Depends(current_user)):
    return svc.reconciliation(db)
