import asyncio
import json
from uuid import uuid4

from google.adk.agents.run_config import RunConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from .agents import APP_NAME, build_app
from .config import Settings
from .store import Store
from .tools import CondoTools

USER_ID = "morador"  # Isolation is by unguessable session_id and immutable binding.


class Runtime:
    def __init__(self, settings: Settings, model=None):
        self.store = Store(settings)
        self.tools = CondoTools(self.store)
        self.sessions = DatabaseSessionService(
            db_url=f"sqlite+aiosqlite:///{settings.storage / 'sessoes.sqlite3'}",
            connect_args={"timeout": 30},
        )
        self.app = build_app(self.tools, model or settings.model)
        self.runner = Runner(app=self.app, session_service=self.sessions)
        self.locks: dict[str, asyncio.Lock] = {}

    def lock(self, session_id):
        return self.locks.setdefault(session_id, asyncio.Lock())

    async def close(self):
        await self.runner.close()
        await self.sessions.close()

    async def create(self, apartment):
        session_id = uuid4().hex
        if apartment not in self.store.apartments:
            raise ValueError("Apartamento inexistente")
        await self.sessions.create_session(
            app_name=APP_NAME,
            user_id=USER_ID,
            session_id=session_id,
            state={"apartamento": apartment},
        )
        self.store.bind(session_id, apartment)
        return session_id

    async def session(self, session_id):
        if not self.store.apartment(session_id):
            return None
        return await self.sessions.get_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )

    def sync_confirmations(self, session):
        # Rebuild the pending projection from persisted native ADK events too:
        # restarting between event persistence and this projection loses nothing.
        for event in session.events:
            for call in event.get_function_calls():
                if call.name != "adk_request_confirmation":
                    continue
                original = call.args["originalFunctionCall"]
                if original["name"] not in {"reservar_area", "autorizar_visitante"}:
                    continue
                details = call.args["toolConfirmation"]["payload"]
                self.store.register_confirmation(
                    id=call.id,
                    session_id=session.id,
                    invocation_id=event.invocation_id,
                    tool_call_id=original["id"],
                    acao=original["name"],
                    detalhes=details,
                )

    async def response(self, session_id, text):
        session = await self.session(session_id)
        self.sync_confirmations(session)
        return {"resposta": text, "confirmacoes_pendentes": self.store.pending(session_id)}

    async def message(self, session_id, text):
        session = await self.session(session_id)
        self.sync_confirmations(session)
        if self.store.pending(session_id):
            return await self.response(
                session_id, "Responda à confirmação pendente pela rota de confirmações."
            )
        message = types.Content(role="user", parts=[types.Part(text=text)])
        result = await self._run(session_id, message)
        return await self.response(session_id, result)

    async def confirm(self, session_id, id, confirmed):
        session = await self.session(session_id)
        self.sync_confirmations(session)
        pending = self.store.answer(session_id, id, confirmed)
        message = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=id,
                        name="adk_request_confirmation",
                        response={
                            "confirmed": confirmed,
                            "payload": json.loads(pending["detalhes"]),
                        },
                    )
                )
            ],
        )
        try:
            text = await self._run(session_id, message, pending["invocation_id"])
        except Exception:
            # An unavailable model after the tool committed must not turn a
            # completed approval into a misleading server error.
            result = self.store.result(id)
            if result is None:
                raise
            text = self.describe(result)
        result = self.store.result(id)
        if result is None:
            raise RuntimeError("O ADK não retomou a tool que solicitou a confirmação")
        return await self.response(session_id, text or self.describe(result))

    @staticmethod
    def describe(result):
        status = result["status"]
        return {
            "reservada": "Reserva concluída.",
            "autorizado": "Entrada do visitante autorizada.",
            "negada": "Ação negada. Nenhuma alteração foi realizada.",
            "ocupada": "A área já está ocupada nessa data. A reserva não foi realizada.",
        }.get(status, "Ação não executada.")

    async def _run(self, session_id, message, invocation_id=None):
        texts = []
        async for event in self.runner.run_async(
            user_id=USER_ID,
            session_id=session_id,
            new_message=message,
            invocation_id=invocation_id,
            run_config=RunConfig(max_llm_calls=12),
        ):
            if event.is_final_response() and event.content:
                texts.extend(p.text for p in event.content.parts or [] if p.text and not p.thought)
        return "\n".join(texts)
