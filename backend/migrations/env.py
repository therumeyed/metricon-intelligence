from alembic import context

from app import models  # noqa: F401  (register tables)
from app.db import Base, get_engine

target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
