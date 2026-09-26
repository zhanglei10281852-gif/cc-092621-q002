from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import audit, auth, maintenance, roles, system, users
from app.core.errors import DomainError
from app.database import close_connection, get_connection, init_db
from app.temple.router import router as temple_router
from app.temple.operations_router import router as operations_router
from app.temple.schema import ensure_temple_schema


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_db()
    ensure_temple_schema(get_connection())
    yield
    close_connection()


app = FastAPI(title="寺院香火与修缮协同服务", version="2.0.0", lifespan=lifespan)


@app.exception_handler(DomainError)
async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
    del request
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "context": exc.context}},
    )


app.include_router(auth.router)
app.include_router(users.router)
app.include_router(roles.router)
app.include_router(audit.router)
app.include_router(system.router)
app.include_router(maintenance.router)
app.include_router(temple_router)
app.include_router(operations_router)


@app.get("/")
def root() -> dict:
    return {"service": "寺院香火与修缮协同服务", "version": "2.0.0"}
