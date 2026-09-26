"""Dados do condomínio. Nenhuma consulta de morador aceita identidade do LLM."""

import json
import sqlite3
from contextlib import contextmanager
from uuid import uuid4

from .config import Settings


class ConfirmationConflict(Exception):
    pass


class Store:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.storage.mkdir(parents=True, exist_ok=True)
        self.path = settings.storage / "condominio.sqlite3"
        self.areas = {x["id"]: x for x in self.seed("areas")}
        self.apartments = {x["numero"] for x in self.seed("apartamentos")}
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS bindings (
                    session_id TEXT PRIMARY KEY, apartamento TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS immutable_apartment
                BEFORE UPDATE OF apartamento ON bindings
                BEGIN SELECT RAISE(ABORT, 'Apartamento imutavel'); END;
                CREATE TABLE IF NOT EXISTS reservas (
                    codigo TEXT PRIMARY KEY, apartamento TEXT NOT NULL,
                    area TEXT NOT NULL, data TEXT NOT NULL,
                    ativa INTEGER NOT NULL DEFAULT 1 CHECK (ativa IN (0, 1))
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uma_reserva_ativa
                    ON reservas(area, data) WHERE ativa = 1;
                CREATE TABLE IF NOT EXISTS visitantes (
                    apartamento TEXT NOT NULL, nome TEXT NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY (apartamento, nome, data)
                );
                CREATE TABLE IF NOT EXISTS confirmacoes (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    invocation_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                    acao TEXT NOT NULL, detalhes TEXT NOT NULL,
                    respondida INTEGER NOT NULL DEFAULT 0,
                    confirmado INTEGER, resultado TEXT,
                    UNIQUE(session_id, tool_call_id)
                );
            """)
            # Startup never resets live data. Seeding is a one-time transaction.
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM metadata WHERE key='seeded'").fetchone():
                for row in self.seed("reservas"):
                    db.execute(
                        "INSERT INTO reservas(codigo,apartamento,area,data) VALUES(?,?,?,?)",
                        (row["codigo"], row["apartamento"], row["area"], row["data"]),
                    )
                for row in self.seed("visitantes"):
                    db.execute(
                        "INSERT INTO visitantes VALUES(?,?,?)",
                        (row["apartamento"], row["nome"], row["data"]),
                    )
                db.execute("INSERT INTO metadata VALUES('seeded')")

    def seed(self, name):
        return json.loads((self.settings.data / f"{name}.json").read_text())

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, session_id, apartment):
        if apartment not in self.apartments:
            raise ValueError("Apartamento inexistente")
        with self.connect() as db:
            db.execute("INSERT INTO bindings VALUES(?,?)", (session_id, apartment))

    def apartment(self, session_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT apartamento FROM bindings WHERE session_id=?", (session_id,)
            ).fetchone()
        return row[0] if row else None

    def reservations(self, apartment):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT codigo,area,data FROM reservas WHERE apartamento=? AND ativa=1 "
                    "ORDER BY data,codigo",
                    (apartment,),
                )
            ]

    def visitors(self, apartment):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT nome,data FROM visitantes WHERE apartamento=? ORDER BY data,nome",
                    (apartment,),
                )
            ]

    def available(self, area, date):
        with self.connect() as db:
            return not db.execute(
                "SELECT 1 FROM reservas WHERE area=? AND data=? AND ativa=1", (area, date)
            ).fetchone()

    def cancel(self, apartment, area, date):
        with self.connect() as db:
            result = db.execute(
                "UPDATE reservas SET ativa=0 WHERE apartamento=? AND area=? AND data=? AND ativa=1",
                (apartment, area, date),
            )
            return {"status": "cancelada" if result.rowcount else "reserva_propria_nao_encontrada"}

    def register_confirmation(self, *, id, session_id, invocation_id, tool_call_id, acao, detalhes):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO confirmacoes "
                "(id,session_id,invocation_id,tool_call_id,acao,detalhes) VALUES(?,?,?,?,?,?)",
                (
                    id,
                    session_id,
                    invocation_id,
                    tool_call_id,
                    acao,
                    json.dumps(detalhes, sort_keys=True),
                ),
            )

    def pending(self, session_id):
        with self.connect() as db:
            return [
                {"id": r["id"], "acao": r["acao"], "detalhes": json.loads(r["detalhes"])}
                for r in db.execute(
                    "SELECT * FROM confirmacoes WHERE session_id=? AND respondida=0 ORDER BY rowid",
                    (session_id,),
                )
            ]

    def answer(self, session_id, id, confirmed):
        # Claim once, scoped to the session, before calling the ADK Runner.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM confirmacoes WHERE id=? AND session_id=? AND respondida=0",
                (id, session_id),
            ).fetchone()
            if not row:
                raise ConfirmationConflict
            db.execute(
                "UPDATE confirmacoes SET respondida=1,confirmado=? WHERE id=?",
                (int(confirmed), id),
            )
            return dict(row)

    def result(self, id):
        with self.connect() as db:
            row = db.execute("SELECT resultado FROM confirmacoes WHERE id=?", (id,)).fetchone()
            return json.loads(row[0]) if row and row[0] else None

    def execute(self, *, session_id, apartment, call_id, action, details, native_confirmed=False):
        """Consume approval and commit its effect in the SAME transaction.

        The approval binds session, original call, action and exact arguments.
        Neither a model-provided boolean nor a recycled native response grants access.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            binding = db.execute(
                "SELECT apartamento FROM bindings WHERE session_id=?", (session_id,)
            ).fetchone()
            if not binding or binding[0] != apartment:
                return {"status": "identidade_invalida"}
            needs_approval = (
                action == "autorizar_visitante" or self.areas[details["area"]]["taxa"] > 0
            )
            grant = None
            if needs_approval:
                grant = db.execute(
                    "SELECT * FROM confirmacoes WHERE session_id=? AND tool_call_id=? "
                    "AND acao=? AND detalhes=? AND respondida=1",
                    (session_id, call_id, action, json.dumps(details, sort_keys=True)),
                ).fetchone()
                if not grant:
                    return {"status": "confirmacao_obrigatoria"}
                if grant["resultado"]:
                    return json.loads(grant["resultado"])
                if not grant["confirmado"]:
                    result = {"status": "negada", "mensagem": "Ação não executada."}
                elif not native_confirmed:
                    return {"status": "confirmacao_obrigatoria"}
                else:
                    result = self._insert(db, apartment, action, details)
                db.execute(
                    "UPDATE confirmacoes SET resultado=? WHERE id=?",
                    (json.dumps(result), grant["id"]),
                )
                return result
            return self._insert(db, apartment, action, details)

    @staticmethod
    def _insert(db, apartment, action, details):
        if action == "autorizar_visitante":
            db.execute(
                "INSERT OR IGNORE INTO visitantes VALUES(?,?,?)",
                (apartment, details["nome"], details["data"]),
            )
            return {"status": "autorizado", **details}
        code = f"RSV-{uuid4().hex}"
        try:
            db.execute(
                "INSERT INTO reservas(codigo,apartamento,area,data) VALUES(?,?,?,?)",
                (code, apartment, details["area"], details["data"]),
            )
        except sqlite3.IntegrityError:
            # The unique index is the arbiter, including across processes.
            # No competing apartment/code is ever returned to the conversation.
            return {"status": "ocupada", "area": details["area"], "data": details["data"]}
        return {"status": "reservada", "codigo": code, **details}
