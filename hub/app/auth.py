from fastapi import Header, HTTPException

from . import config


def require_probe(authorization: str = Header(default="")) -> str:
    """Validate the bearer token and return the probe name it maps to.

    The probe name always comes from the token lookup, never from anything
    the request body claims - a probe can't impersonate another probe just
    by putting a different name in its payload.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    name = config.PROBE_TOKENS.get(token)
    if not name:
        raise HTTPException(status_code=401, detail="invalid token")
    return name
