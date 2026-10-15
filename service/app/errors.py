"""API 오류 응답의 형식을 한곳에서 정한다."""

from typing import NoReturn

from fastapi import HTTPException


def api_error(status: int, code: str, message: str, **extra) -> NoReturn:
    """detail이 {"code", "message", ...extra} 형식인 HTTPException을 낸다."""
    raise HTTPException(status, {"code": code, "message": message, **extra})
