from collections import deque

import pytest
from fastapi.testclient import TestClient
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from aurora.api import create_app
from aurora.config import Settings


class ScriptedModel(BaseLlm):
    """Only the network/model is replaced; Runner, tools and SQLite are real."""

    model: str = "scripted"
    _responses: deque = PrivateAttr(default_factory=deque)

    def enqueue(self, *responses):
        self._responses.extend(responses)

    async def generate_content_async(self, llm_request, stream=False):
        assert self._responses, "Modelo chamado sem resposta preparada"
        value = self._responses.popleft()
        owners = {
            "reservas": "reservar_area",
            "portaria": "autorizar_visitante",
            "regulamento": "consultar_regulamento",
        }
        if isinstance(value, list) and value[0][0] == "transfer_to_agent":
            target = value[0][1]["agent_name"]
            if owners.get(target) in llm_request.tools_dict:
                value = self._responses.popleft()
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            parts = [types.Part(text=value)]
        else:
            parts = [
                types.Part(function_call=types.FunctionCall(name=name, args=args))
                for name, args in value
            ]
        yield LlmResponse(content=types.Content(role="model", parts=parts))

    def action(self, specialist, tool, args, *, pending=False):
        self.enqueue([("transfer_to_agent", {"agent_name": specialist})], [(tool, args)])
        if not pending:
            self.enqueue("Concluído.")


@pytest.fixture
def settings(tmp_path):
    return Settings(storage=tmp_path / "storage")


@pytest.fixture
def model():
    return ScriptedModel()


@pytest.fixture
def client(settings, model):
    with TestClient(create_app(settings, model=model)) as client:
        yield client


def session(client, apartment="101"):
    response = client.post("/sessoes", json={"apartamento": apartment})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def message(client, sid, text="Execute o pedido"):
    response = client.post(f"/sessoes/{sid}/mensagens", json={"texto": text})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, sid, id, approved=True):
    return client.post(f"/sessoes/{sid}/confirmacoes", json={"id": id, "confirmado": approved})
