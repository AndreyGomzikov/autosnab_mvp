from pathlib import Path
import sys

from sqlalchemy import inspect

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.db.session import Base, engine  # noqa: E402
from app.models import *  # noqa: F401,F403,E402

REQUIRED_TABLES = {
    "diadoc_sync_states",
    "diadoc_documents",
    "diadoc_artifacts",
    "diadoc_deliveries",
    "diadoc_leases",
}


def main() -> None:
    Base.metadata.create_all(bind=engine)
    existing = set(inspect(engine).get_table_names())
    missing = sorted(REQUIRED_TABLES - existing)
    if missing:
        raise RuntimeError(
            "Не удалось создать таблицы Диадок: "
            + ", ".join(missing)
        )
    print(
        "Diadoc reliability tables are ready: "
        + ", ".join(sorted(REQUIRED_TABLES))
    )


if __name__ == "__main__":
    main()
