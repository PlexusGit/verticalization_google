"""ASGI entry point for Plexus Vertical Cuts."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.routers import api, auth, views

BASE_DIR = Path(__file__).resolve().parent.parent
app = FastAPI(
    title="Plexus Vertical Cuts",
    description="Criação assistida de cortes jornalísticos verticais",
    version="1.0.0",
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    same_site="lax",
    https_only=settings.DOMAIN.startswith("https://"),
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "templates" / "static"), name="static")
app.state.templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
app.include_router(auth.router)
app.include_router(api.router)
app.include_router(views.router)


@app.get("/logo.svg", include_in_schema=False)
async def logo():
    """Serve the single approved application logo from the repository root."""

    return FileResponse(BASE_DIR / "PlexusLogo-vetor.svg", media_type="image/svg+xml")


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "plexus-vertical-cuts"}
