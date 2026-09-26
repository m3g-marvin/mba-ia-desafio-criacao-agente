from datetime import date

from google.adk.tools import ToolContext

from .regulation import Regulation
from .store import Store


class CondoTools:
    def __init__(self, store: Store):
        self.store = store
        self.regulation = Regulation(store.settings.data / "regulamento.md")

    def identity(self, context: ToolContext) -> tuple[str, str]:
        session_id = context.session.id
        apartment = context.state.get("apartamento")
        if not apartment or self.store.apartment(session_id) != apartment:
            raise ValueError("Identidade da sessão inválida")
        return session_id, apartment

    @staticmethod
    def valid_date(value: str):
        try:
            return date.fromisoformat(value).isoformat() == value
        except (ValueError, TypeError):
            return False

    def validate_booking(self, area, data):
        if area not in self.store.areas:
            return {"status": "area_invalida", "areas": list(self.store.areas.values())}
        if not self.valid_date(data):
            return {"status": "data_invalida", "formato": "AAAA-MM-DD"}
        return None

    def listar_areas(self) -> dict:
        """Lista IDs, nomes e taxas das áreas reserváveis."""
        return {"areas": list(self.store.areas.values())}

    def listar_reservas(self, tool_context: ToolContext) -> dict:
        """Consulta as reservas ativas exclusivamente do apartamento da sessão."""
        _, apartment = self.identity(tool_context)
        return {"reservas": self.store.reservations(apartment)}

    def consultar_disponibilidade(self, area: str, data: str) -> dict:
        """Consulta se uma área está livre na data AAAA-MM-DD, sem identificar moradores."""
        if error := self.validate_booking(area, data):
            return error
        return {"area": area, "data": data, "disponivel": self.store.available(area, data)}

    def reservar_area(self, area: str, data: str, tool_context: ToolContext) -> dict:
        """Reserva área na data AAAA-MM-DD. Áreas pagas exigem confirmação pela API."""
        session_id, apartment = self.identity(tool_context)
        if error := self.validate_booking(area, data):
            return error
        details = {"area": area, "data": data, "taxa": self.store.areas[area]["taxa"]}
        confirmation = tool_context.tool_confirmation
        if details["taxa"] > 0 and confirmation is None:
            if not self.store.available(area, data):
                return {"status": "ocupada", "area": area, "data": data}
            tool_context.request_confirmation(
                hint="Confirme a reserva e sua taxa pela rota de confirmações.", payload=details
            )
            return {"status": "aguardando_confirmacao", **details}
        return self.store.execute(
            session_id=session_id,
            apartment=apartment,
            call_id=tool_context.function_call_id,
            action="reservar_area",
            details=details,
            native_confirmed=bool(confirmation and confirmation.confirmed),
        )

    def cancelar_reserva(self, area: str, data: str, tool_context: ToolContext) -> dict:
        """Cancela somente a reserva própria por área e data, sem confirmação."""
        _, apartment = self.identity(tool_context)
        if error := self.validate_booking(area, data):
            return error
        return self.store.cancel(apartment, area, data)

    def listar_visitantes(self, tool_context: ToolContext) -> dict:
        """Lista autorizações de visitantes exclusivamente do apartamento da sessão."""
        _, apartment = self.identity(tool_context)
        return {"visitantes": self.store.visitors(apartment)}

    def autorizar_visitante(self, nome: str, data: str, tool_context: ToolContext) -> dict:
        """Solicita acesso de visitante na data AAAA-MM-DD. Sempre exige confirmação na API."""
        session_id, apartment = self.identity(tool_context)
        nome = nome.strip()
        if not nome or len(nome) > 200 or not self.valid_date(data):
            return {"status": "dados_invalidos", "mensagem": "Informe nome e data AAAA-MM-DD."}
        details = {"nome": nome, "data": data}
        confirmation = tool_context.tool_confirmation
        if confirmation is None:
            tool_context.request_confirmation(
                hint="Confirme a autorização de entrada pela rota de confirmações.", payload=details
            )
            return {"status": "aguardando_confirmacao", **details}
        return self.store.execute(
            session_id=session_id,
            apartment=apartment,
            call_id=tool_context.function_call_id,
            action="autorizar_visitante",
            details=details,
            native_confirmed=bool(confirmation.confirmed),
        )

    def consultar_regulamento(self, tool_context: ToolContext) -> dict:
        """Busca artigos pertinentes à última pergunta do morador, em um só capítulo."""
        # No model-selected query/chapter: retrieval is bound to the user's question.
        for event in reversed(tool_context.session.events):
            if event.author == "user" and event.content:
                text = " ".join(p.text for p in event.content.parts or [] if p.text)
                if text:
                    return self.regulation.search(text)
        return {"status": "pergunta_ausente"}
