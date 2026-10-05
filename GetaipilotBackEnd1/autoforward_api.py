"""Authenticated AutoForward web login routes; disabled unless explicitly enabled."""
import os
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field


class LinkRequest(BaseModel):
    token: str = Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]+$")


class StartRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=32)
    link_id: UUID


class VerifyRequest(BaseModel):
    attempt_id: UUID
    value: str = Field(min_length=1, max_length=256)


class CancelRequest(BaseModel):
    attempt_id: UUID


def build_router(auth_dependency, runtime_factory=None):
    router = APIRouter(prefix="/autoforward", tags=["AutoForward login"])
    runtime = None

    def services():
        nonlocal runtime
        if os.getenv("AUTOFORWARD_WEB_LOGIN", "false").lower() != "true":
            raise HTTPException(404, detail={"code": "disabled", "message": "Website login is unavailable. Use the bot's current login instructions."})
        if runtime is None:
            if runtime_factory:
                runtime = runtime_factory()
            else:
                from supabase import create_client, ClientOptions
                from telegram_common import SessionEncryption
                from telegram_common.autoforward import AutoForwardLogin, AutoForwardStore
                from telegram_common.login_links import AutoForwardLoginLinks, SupabaseLinkStore
                try:
                    client = create_client(os.environ["VITE_SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                                           options=ClientOptions(postgrest_client_timeout=8))
                    cipher = SessionEncryption.from_environment()
                    runtime = (AutoForwardLoginLinks(SupabaseLinkStore(client), "https://getaipilot.in/autoforward/login"),
                               AutoForwardLogin(AutoForwardStore(client), cipher, int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"]))
                except Exception:
                    raise HTTPException(503, detail={"code": "configuration_unavailable", "message": "Login is temporarily unavailable."}) from None
        return runtime

    async def run(operation):
        from telegram_common.login import LoginError
        try:
            return await operation
        except LoginError as error:
            raise HTTPException(error.status_code, detail={"code": error.code, "message": error.message,
                "restart": error.restart, "retry_after": error.retry_after}) from None

    @router.post("/login/redeem")
    async def redeem(body: LinkRequest, ctx=Depends(auth_dependency)):
        links, _ = services()
        return await run(links.redeem(ctx["user"].id, body.token))

    @router.post("/login/start")
    async def start(body: StartRequest, ctx=Depends(auth_dependency)):
        _, login = services()
        return await run(login.start(ctx["user"].id, body.phone, str(body.link_id)))

    @router.post("/login/otp")
    async def otp(body: VerifyRequest, ctx=Depends(auth_dependency)):
        _, login = services()
        return await run(login.verify(ctx["user"].id, str(body.attempt_id), "otp", body.value))

    @router.post("/login/password")
    async def password(body: VerifyRequest, ctx=Depends(auth_dependency)):
        _, login = services()
        return await run(login.verify(ctx["user"].id, str(body.attempt_id), "password", body.value))

    @router.get("/login/attempt/{attempt_id}")
    async def resume(attempt_id: UUID, ctx=Depends(auth_dependency)):
        _, login = services()
        return await run(login.resume(ctx["user"].id, str(attempt_id)))

    @router.post("/login/cancel")
    async def cancel(body: CancelRequest, ctx=Depends(auth_dependency)):
        _, login = services()
        return await run(login.cancel(ctx["user"].id, str(body.attempt_id)))

    @router.get("/status")
    async def status(ctx=Depends(auth_dependency)):
        _, login = services()
        row = await run(login.store.credential(ctx["user"].id))
        return {"connected": bool(row and row["state"] == "active"), "state": row["state"] if row else "disconnected"}

    @router.post("/logout")
    async def logout(ctx=Depends(auth_dependency)):
        _, login = services()
        return await run(login.logout(ctx["user"].id))

    return router
