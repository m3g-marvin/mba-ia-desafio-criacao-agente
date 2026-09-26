import uvicorn

from .config import Settings
from .store import Store


def serve():
    Settings.from_env()
    uvicorn.run("aurora.api:app", host="0.0.0.0", port=8000, workers=1)


def reset():
    """Execute com a API parada. Apaga também sessões e confirmações."""
    settings = Settings.from_env()
    for name in ("condominio.sqlite3", "sessoes.sqlite3"):
        for suffix in ("", "-wal", "-shm", "-journal"):
            (settings.storage / f"{name}{suffix}").unlink(missing_ok=True)
    Store(settings)
    print("Dados iniciais restaurados. Sessões e confirmações removidas.")
