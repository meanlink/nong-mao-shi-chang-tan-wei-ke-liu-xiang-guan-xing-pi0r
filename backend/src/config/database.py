from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .settings import DATABASE_URL
class Base(DeclarativeBase): pass
_engine_kwargs=dict(pool_pre_ping=True, pool_recycle=1800)
# 封账/补账的“加锁后读取最新已封周”依赖 READ COMMITTED，避免 RR 快照读到旧状态
if DATABASE_URL.startswith(('mysql','mariadb')):
    _engine_kwargs['isolation_level']='READ COMMITTED'
if DATABASE_URL.startswith('sqlite'):
    _engine_kwargs['connect_args']={'timeout':30}
engine = create_engine(DATABASE_URL, **_engine_kwargs)
Session = sessionmaker(bind=engine, expire_on_commit=False)
def get_db():
    with Session() as db: yield db
