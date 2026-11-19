from alembic import context

from app import models  # Load the mapped tables into Base.metadata.
from app.config import get_settings
from app.database import Base, open_database


database_url = get_settings().database_url

if context.is_offline_mode():
    context.configure(url=database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = open_database(database_url)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
