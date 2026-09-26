import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    storage: Path
    model: str = "gemini-2.5-flash"
    data: Path = ROOT / "dados"

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        return cls(
            storage=Path(os.getenv("AURORA_STORAGE_DIR") or ROOT / "storage").resolve(),
            model=os.getenv("GEMINI_MODEL") or "gemini-2.5-flash",
        )
