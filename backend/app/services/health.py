import redis
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


def check_db(db: Session) -> bool:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True


def check_redis(url: str) -> bool:
    try:
        with redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2) as client:
            return bool(client.ping())
    except redis.RedisError:
        return False
