"""共享依赖（认证等），供 main 与各路由模块复用。"""
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .db import get_db


def current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)):
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "缺少访问令牌")
    user = db.execute(select(models.User).where(models.User.token == token)).scalar_one_or_none()
    if not user:
        raise HTTPException(401, "令牌无效")
    return user
