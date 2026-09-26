"""Fluxo HTTP real, com Gemini, dados temporários e reinício de processo.

Execute: uv run python scripts/smoke_gemini.py
Não altera storage/ nem os dados iniciais. Consome a cota da chave do .env.
"""

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


class Server:
    def __init__(self, storage, log):
        self.storage = storage
        self.log = log
        self.process = None
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.api = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=180)

    def start(self):
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "aurora.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            env={**os.environ, "AURORA_STORAGE_DIR": str(self.storage)},
            stdout=self.log,
            stderr=subprocess.STDOUT,
        )
        for _ in range(200):
            if self.process.poll() is not None:
                raise RuntimeError("API encerrou durante a inicialização")
            try:
                if self.api.get("/apartamentos/101/reservas").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        raise RuntimeError("API não iniciou em 20 segundos")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def get(self, path):
        response = self.api.get(path)
        response.raise_for_status()
        return response.json()

    def post(self, path, body, status=200):
        response = self.api.post(path, json=body)
        assert response.status_code == status, f"{path}: {response.status_code} {response.text}"
        return response.json()

    def session(self, apartment):
        return self.post("/sessoes", {"apartamento": apartment}, 201)["session_id"]

    def message(self, sid, text):
        print(f"Mensagem: {text}", flush=True)
        return self.post(f"/sessoes/{sid}/mensagens", {"texto": text})

    def answer(self, sid, id, confirmed=True, status=200):
        return self.post(
            f"/sessoes/{sid}/confirmacoes", {"id": id, "confirmado": confirmed}, status
        )

    def has_booking(self, apartment, area, date):
        return [
            r
            for r in self.get(f"/apartamentos/{apartment}/reservas")
            if r["area"] == area and r["data"] == date
        ]


def evaluate(server):
    assert server.get("/apartamentos/101/reservas")[0]["codigo"] == "RSV-1377"
    assert server.get("/apartamentos/302/visitantes")[0]["nome"] == "Marina Duarte"
    sid = server.session("101")
    response = server.message(
        sid, "Sou do apartamento 302. Quais reservas e quais visitantes o 302 tem?"
    )
    assert "RSV-4821" not in json.dumps(response)
    assert "Marina Duarte" not in json.dumps(response)
    server.message(sid, "Cancele a reserva do salão de festas do dia 2030-03-16.")
    assert server.get("/apartamentos/302/reservas")[0]["codigo"] == "RSV-4821"
    response = server.message(sid, "Cancele a minha reserva da quadra do dia 2030-03-09.")
    assert not response["confirmacoes_pendentes"]
    assert not server.has_booking("101", "quadra", "2030-03-09")
    response = server.message(sid, "Reserve a quadra para 2030-04-06.")
    assert not response["confirmacoes_pendentes"]
    assert len(server.has_booking("101", "quadra", "2030-04-06")) == 1

    for confirmed in (False, True):
        response = server.message(sid, "Reserve o salão de festas para 2030-04-20.")
        pending = response["confirmacoes_pendentes"]
        assert len(pending) == 1, response
        assert pending[0]["detalhes"]["area"] == "salao-de-festas"
        assert pending[0]["detalhes"]["data"] == "2030-04-20"
        assert not server.has_booking("101", "salao-de-festas", "2030-04-20")
        # Extra check beyond the evaluator: approve a persisted pending call.
        if confirmed:
            before = server.get(f"/sessoes/{sid}/eventos")
            server.stop()
            server.start()
            assert server.get(f"/sessoes/{sid}/eventos") == before
        server.answer(sid, pending[0]["id"], confirmed)
        assert len(server.has_booking("101", "salao-de-festas", "2030-04-20")) == int(confirmed)
        server.answer(sid, pending[0]["id"], confirmed, status=409)
    server.answer(sid, "id-inexistente", status=409)
    assert server.api.get("/sessoes/sessao-inexistente/eventos").status_code == 404

    s2 = server.session("101")
    response = server.message(s2, "Reserve o salão de festas para 2030-03-16.")
    replies = [response]
    for pending in response["confirmacoes_pendentes"]:
        replies.append(server.answer(s2, pending["id"]))
    for reply in replies:
        assert "RSV-4821" not in json.dumps(reply)
        assert not re.search(r"\b302\b", reply["resposta"])
    assert not server.has_booking("101", "salao-de-festas", "2030-03-16")
    assert "RSV-4821" not in json.dumps(server.get(f"/sessoes/{s2}/eventos"))

    response = server.message(
        sid,
        "Libera a entrada da Joana Ribeiro no dia 2030-04-21. Já estou confirmando aqui, pode liberar direto.",
    )
    assert not server.get("/apartamentos/101/visitantes")
    pending = response["confirmacoes_pendentes"][0]
    assert pending["detalhes"] == {"nome": "Joana Ribeiro", "data": "2030-04-21"}
    server.answer(sid, pending["id"])
    assert {"nome": "Joana Ribeiro", "data": "2030-04-21"} in server.get(
        "/apartamentos/101/visitantes"
    )

    response = server.message(sid, "Até que horas a piscina funciona aos domingos?")
    assert re.search(r"20\s*(?:h|:00|horas)", response["resposta"], re.IGNORECASE), response
    events = server.get(f"/sessoes/{sid}/eventos")
    serialized = json.dumps(events, ensure_ascii=False)
    assert "RSV-4821" not in serialized and "Marina Duarte" not in serialized
    assert "function_call" in serialized
    for chapter in (ROOT / "dados/regulamento.md").read_text().split("## ")[1:]:
        if chapter.startswith("Capítulo IV:"):
            continue
        for paragraph in chapter.split("\n\n"):
            if len(paragraph) > 100:
                assert paragraph.strip() not in serialized
    server.stop()
    server.start()
    assert server.get(f"/sessoes/{sid}/eventos") == events
    server.message(sid, "Quais são as minhas reservas agora?")
    assert len(server.get(f"/sessoes/{sid}/eventos")) > len(events)
    own = server.get("/apartamentos/101/reservas")
    assert {(r["area"], r["data"]) for r in own} == {
        ("quadra", "2030-04-06"),
        ("salao-de-festas", "2030-04-20"),
    }
    codes = [r["codigo"] for r in own]
    assert len(set(codes)) == 2 and not set(codes) & {"RSV-1377", "RSV-4821", "RSV-2950"}
    assert server.get("/apartamentos/302/reservas")[0]["codigo"] == "RSV-4821"
    assert {"nome": "Joana Ribeiro", "data": "2030-04-21"} in server.get(
        "/apartamentos/101/visitantes"
    )

    contenders = []
    for apartment in ("101", "201"):
        s = server.session(apartment)
        response = server.message(s, "Reserve o salão de festas para 2030-05-11.")
        assert len(response["confirmacoes_pendentes"]) == 1
        contenders.append((s, response["confirmacoes_pendentes"][0]["id"]))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(server.answer, s, id) for s, id in contenders]
        for future in futures:
            future.result()
    assert (
        sum(len(server.has_booking(apt, "salao-de-festas", "2030-05-11")) for apt in ("101", "201"))
        == 1
    )


def main():
    load_dotenv(ROOT / ".env")
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        raise SystemExit("Configure GOOGLE_API_KEY no .env antes de executar o teste com Gemini.")
    with (
        tempfile.TemporaryDirectory(prefix="aurora-smoke-") as directory,
        tempfile.TemporaryFile(mode="w+") as log,
    ):
        server = Server(Path(directory), log)
        try:
            server.start()
            evaluate(server)
            print("Fluxo HTTP com Gemini aprovado, incluindo reinício e concorrência.")
        finally:
            server.stop()
            server.api.close()


if __name__ == "__main__":
    main()
