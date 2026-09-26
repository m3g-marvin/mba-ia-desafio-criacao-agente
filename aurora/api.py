import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .config import Settings
from .runtime import Runtime
from .store import ConfirmationConflict

logger = logging.getLogger(__name__)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewSession(Input):
    apartamento: str = Field(min_length=1, max_length=20)


class Message(Input):
    texto: str = Field(min_length=1, max_length=16000)


class Confirmation(Input):
    id: str = Field(min_length=1, max_length=200)
    confirmado: StrictBool


class Pending(BaseModel):
    id: str
    acao: str
    detalhes: dict


class Reply(BaseModel):
    resposta: str
    confirmacoes_pendentes: list[Pending]


def create_app(settings: Settings | None = None, model=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.runtime = Runtime(settings or Settings.from_env(), model=model)
        yield
        await app.state.runtime.close()

    app = FastAPI(title="Residencial Aurora", version="1.0.0", lifespan=lifespan)

    async def existing(session_id):
        runtime = app.state.runtime
        if await runtime.session(session_id) is None:
            raise HTTPException(404, "Sessão inexistente")
        return runtime

    @app.post("/sessoes", status_code=201)
    async def create_session(body: NewSession):
        try:
            return {"session_id": await app.state.runtime.create(body.apartamento)}
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/sessoes/{session_id}/mensagens", response_model=Reply)
    async def message(session_id: str, body: Message):
        runtime = await existing(session_id)
        async with runtime.lock(session_id):
            try:
                return await runtime.message(session_id, body.texto)
            except Exception as exc:
                logger.error("Falha na conversa: %s", type(exc).__name__)
                raise HTTPException(
                    503,
                    "Não foi possível concluir a conversa. Consulte os dados e tente novamente.",
                ) from exc

    @app.post("/sessoes/{session_id}/confirmacoes", response_model=Reply)
    async def confirm(session_id: str, body: Confirmation):
        runtime = await existing(session_id)
        async with runtime.lock(session_id):
            try:
                return await runtime.confirm(session_id, body.id, body.confirmado)
            except ConfirmationConflict as exc:
                raise HTTPException(409, "Confirmação não está pendente nesta sessão") from exc
            except Exception as exc:
                logger.error("Falha na retomada: %s", type(exc).__name__)
                raise HTTPException(
                    503,
                    "Falha na retomada. Consulte as rotas de verificação antes de um novo pedido.",
                ) from exc

    @app.get("/sessoes/{session_id}/eventos")
    async def events(session_id: str):
        runtime = await existing(session_id)
        async with runtime.lock(session_id):
            session = await runtime.session(session_id)
            return [event.model_dump(mode="json") for event in session.events]

    @app.get("/apartamentos/{apartamento}/reservas")
    async def reservations(apartamento: str):
        return app.state.runtime.store.reservations(apartamento)

    @app.get("/apartamentos/{apartamento}/visitantes")
    async def visitors(apartamento: str):
        return app.state.runtime.store.visitors(apartamento)

    return app


app = create_app()
