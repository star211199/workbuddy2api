"""安全：管理后台 JWT 登录 + API Key 校验与配额判断。"""
import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta

import jwt
from fastapi import Header, HTTPException

from admin.config import settings
from admin.db import SessionLocal
from admin.models import ApiKey, SystemSetting


# ---------------------------------------------------------------------------
# 管理后台登录
# ---------------------------------------------------------------------------

#: 令牌版本在 SystemSetting 里的键名。
#: 每次改密码都会把它 +1，从而**立刻作废所有已签发的令牌**
#: （见 `bump_token_version`）。缓存 5 秒，避免每个请求都打一次库。
_TOKEN_VERSION_KEY = "admin_token_version"
_TOKEN_VERSION_TTL = 5.0
_token_version_cache: tuple[float, int] | None = None


def get_token_version() -> int:
    """读取当前令牌版本；数据库不可用时保守地返回 0。

    数据库不可用时不抛异常：否则数据库一抖，所有后台请求都会 500，
    而这里返回 0 只是让「改密作废」暂时失效，可用性优先。
    """
    global _token_version_cache
    now = time.time()
    if _token_version_cache and (now - _token_version_cache[0]) < _TOKEN_VERSION_TTL:
        return _token_version_cache[1]
    version = 0
    db = None
    try:
        db = SessionLocal()
        row = db.query(SystemSetting).filter(
            SystemSetting.key == _TOKEN_VERSION_KEY).first()
        if row and row.value:
            version = int(row.value)
    except Exception:
        version = 0
    finally:
        if db is not None:
            db.close()
    _token_version_cache = (now, version)
    return version


def bump_token_version() -> int:
    """令牌版本 +1 —— 改密码 / 主动登出时调用，作废此前签发的所有令牌。"""
    global _token_version_cache
    db = None
    new_version = 0
    try:
        db = SessionLocal()
        row = db.query(SystemSetting).filter(
            SystemSetting.key == _TOKEN_VERSION_KEY).first()
        if row:
            new_version = int(row.value or 0) + 1
            row.value = str(new_version)
        else:
            new_version = 1
            db.add(SystemSetting(key=_TOKEN_VERSION_KEY, value=str(new_version)))
        db.commit()
    except Exception:
        # 拿不到版本就退回时间戳，保证"改密后旧令牌一定失效"
        # （宁可让所有登录态一起掉，也不能让泄露的令牌继续可用）。
        new_version = int(time.time())
    finally:
        if db is not None:
            db.close()
    _token_version_cache = (time.time(), new_version)
    return new_version


def create_admin_token() -> str:
    now = datetime.utcnow()
    exp = now + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    return jwt.encode(
        {
            "sub": settings.ADMIN_USERNAME,
            "exp": exp,
            # 版本号：校验时与当前版本比对，改密即可整体作废。
            "ver": get_token_version(),
        },
        settings.JWT_SECRET, algorithm="HS256")


def verify_admin_token(token: str | None):
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(token, settings.JWT_SECRET,
                             algorithms=["HS256"],
                             # 要求 exp 必须存在：否则伪造者可以签一个**永不过期**
                             # 的令牌（PyJWT 默认不强制 exp 存在）。
                             options={"require": ["exp"]})
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="无效令牌")
    if payload.get("sub") != settings.ADMIN_USERNAME:
        raise HTTPException(status_code=401, detail="无效令牌")
    # 令牌版本必须与当前一致：改密码后旧令牌立即失效。
    # 老令牌没有 ver 字段（None），视为版本 0，因此**首次升级后
    # 所有旧令牌都会失效**，需要重新登录一次 —— 这正是我们想要的安全默认。
    if int(payload.get("ver") or 0) != get_token_version():
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")


def require_admin(x_admin_token: str = Header(default=None, alias="X-Admin-Token")):
    """FastAPI 依赖：校验管理后台令牌。"""
    verify_admin_token(x_admin_token)
    return True


# ---------------------------------------------------------------------------
# 管理后台密码（PBKDF2 哈希存储，杜绝明文泄漏）
# ---------------------------------------------------------------------------

_PBKDF2_ITER = 100_000


def hash_password(pw: str) -> str:
    """返回可持久化的哈希串：pbkdf2$<iter>$<salt_b64>$<dk_b64>。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITER)
    return "pbkdf2$" + str(_PBKDF2_ITER) + "$" + \
        base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    """校验密码。仅接受 pbkdf2 哈希。

    关于旧明文兜底：早期版本把密码**明文**存进 `system_settings`，所以这里
    曾保留 `hmac.compare_digest(pw, stored)` 分支。但那是一条实打实的后门 ——
    只要 `system_settings` 里那一行被改成已知值（通过其它漏洞、DB 直连、
    备份恢复、或任何能写库的接口），攻击者不用知道原密码就能直接登录。
    迁移逻辑（server.py 的 `_get_stored_hash`）会在首次读取时把明文转成哈希，
    因此这里直接**拒绝**非 pbkdf2 的值，让明文行无法被当作口令使用。
    """
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        # 拒绝旧明文格式（同时保留恒定时长，避免用响应时间区分两种失败）
        hmac.compare_digest(pw, stored)
        return False
    try:
        _, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        iterations = int(iter_s)
        # 上界保护：iterations 来自数据库，若被篡改成极大值会导致
        # 每次登录都做几十亿次 PBKDF2（CPU 打满 = 拒绝服务）。
        if not (1 <= iterations <= 10_000_000):
            return False
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(cand, expected)
    except Exception:
        return False


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_key_row(db, api_key: str) -> ApiKey | None:
    return db.query(ApiKey).filter(ApiKey.key_hash == hash_key(api_key)).first()


def check_quota(key: ApiKey) -> None:
    """超额则直接拒绝，提示『积分已耗尽』。"""
    if key.status != "active":
        raise HTTPException(status_code=403, detail={"error": {"message": "API Key 已停用", "type": "key_disabled"}})
    if not key.unlimited and key.credit_used >= key.credit_limit:
        raise HTTPException(
            status_code=402,
            detail={"error": {"message": "积分已耗尽", "type": "quota_exceeded"}},
        )
