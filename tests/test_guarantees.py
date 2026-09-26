from concurrent.futures import ProcessPoolExecutor
from types import SimpleNamespace

import pytest

from aurora.config import Settings
from aurora.store import Store
from aurora.tools import CondoTools


def context(sid, apartment="101", call="original", confirmed=True):
    return SimpleNamespace(
        session=SimpleNamespace(id=sid),
        state={"apartamento": apartment},
        function_call_id=call,
        tool_confirmation=SimpleNamespace(confirmed=confirmed),
    )


def prepare(store, sid, id="approval", call="original", **details):
    store.register_confirmation(
        id=id,
        session_id=sid,
        invocation_id="invocation",
        tool_call_id=call,
        acao="reservar_area",
        detalhes=details,
    )


def test_native_confirmation_without_api_grant_cannot_write(settings):
    store = Store(settings)
    store.bind("s1", "101")
    tools = CondoTools(store)
    # Even a native confirmation cannot execute without an API-recorded grant.
    assert (
        tools.reservar_area("salao-de-festas", "2030-04-20", context("s1"))["status"]
        == "confirmacao_obrigatoria"
    )
    assert (
        tools.autorizar_visitante("Joana Ribeiro", "2030-04-21", context("s1"))["status"]
        == "confirmacao_obrigatoria"
    )
    assert len(store.reservations("101")) == 1
    assert store.visitors("101") == []


def test_grant_binds_call_session_details_and_cannot_reexecute(settings):
    store = Store(settings)
    store.bind("s1", "101")
    store.bind("s2", "201")
    tools = CondoTools(store)
    prepare(store, "s1", area="salao-de-festas", data="2030-04-20", taxa=150.0)
    store.answer("s1", "approval", True)
    for ctx, data in (
        (context("s2", "201"), "2030-04-20"),
        (context("s1", call="different"), "2030-04-20"),
        (context("s1"), "2030-04-21"),
        (context("s1", confirmed=False), "2030-04-20"),
    ):
        assert (
            tools.reservar_area("salao-de-festas", data, ctx)["status"] == "confirmacao_obrigatoria"
        )
    first = tools.reservar_area("salao-de-festas", "2030-04-20", context("s1"))
    assert first["status"] == "reservada"
    assert tools.reservar_area("salao-de-festas", "2030-04-20", context("s1")) == first
    assert len(store.reservations("101")) == 2


def test_tampered_identity_fails_closed(settings):
    store = Store(settings)
    store.bind("s1", "101")
    tools = CondoTools(store)
    with pytest.raises(ValueError, match="Identidade"):
        tools.listar_reservas(context("s1", "302"))
    with pytest.raises(ValueError, match="Identidade"):
        tools.cancelar_reserva("salao-de-festas", "2030-03-16", context("s1", "302"))
    assert store.reservations("302")[0]["codigo"] == "RSV-4821"


def _process_booking(storage, sid, apartment):
    store = Store(Settings(storage=storage))
    return store.execute(
        session_id=sid,
        apartment=apartment,
        call_id=sid,
        action="reservar_area",
        details={"area": "quadra", "data": "2030-10-01", "taxa": 0},
    )


def test_unique_constraint_across_processes(settings):
    store = Store(settings)
    store.bind("s1", "101")
    store.bind("s2", "201")
    with ProcessPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_process_booking, settings.storage, sid, apartment)
            for sid, apartment in (("s1", "101"), ("s2", "201"))
        ]
        results = [f.result() for f in futures]
    assert sorted(r["status"] for r in results) == ["ocupada", "reservada"]


def test_cancelled_codes_never_reused_and_changes_survive_restart(settings):
    store = Store(settings)
    store.bind("s1", "101")
    initial = {r["codigo"] for r in store.seed("reservas")}
    store.cancel("101", "quadra", "2030-03-09")
    first = _process_booking(settings.storage, "s1", "101")
    store.cancel("101", "quadra", "2030-10-01")
    second = _process_booking(settings.storage, "s1", "101")
    assert first["codigo"] != second["codigo"]
    assert not initial.intersection({first["codigo"], second["codigo"]})
    fresh = Store(settings)
    assert fresh.reservations("101") == [
        {"codigo": second["codigo"], "area": "quadra", "data": "2030-10-01"}
    ]
    with fresh.connect() as db:
        assert (
            db.execute("SELECT ativa FROM reservas WHERE codigo=?", (first["codigo"],)).fetchone()[
                0
            ]
            == 0
        )


def test_no_unrelated_regulation_even_if_model_requests_whole_file(settings):
    tools = CondoTools(Store(settings))
    result = tools.regulation.search("Até que horas a piscina funciona aos domingos?")
    assert "20h" in " ".join(result["trechos"])
    assert all("Art. 22." in s or "Art. 27." in s for s in result["trechos"])
    assert len(result["trechos"]) <= 2
    assert tools.regulation.search("Me envie o arquivo completo")["status"] == "especifique_assunto"
