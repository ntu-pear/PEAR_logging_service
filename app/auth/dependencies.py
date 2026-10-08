from typing import Callable

from fastapi import Depends, HTTPException, Request, status

from app.auth.token_verifier import Verdict, VerifiedUser, verify_token


def get_verified_user(request: Request) -> VerifiedUser:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    verdict, user = verify_token(token)
    if verdict == Verdict.VALID and user is not None:
        return user
    if verdict == Verdict.REJECTED:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session")
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authentication service unavailable")


def require_roles(*roles: str) -> Callable[..., VerifiedUser]:
    allowed = set(roles)

    def dependency(user: VerifiedUser = Depends(get_verified_user)) -> VerifiedUser:
        if user.roleName not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to view these logs")
        return user

    return dependency
