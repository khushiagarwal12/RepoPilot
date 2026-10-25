import os
from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
from dotenv import load_dotenv

# Load .env variables (POSTGRES_URL) if present
load_dotenv()

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
# Optional: load logging configuration if present in alembic.ini.
# Many projects omit a logging section; we guard against missing keys.
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        # Silently ignore missing logging config – Alembic will use default logging.
        pass

# Add your model's MetaData object here for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
# For now we don't have models yet, so set to None – later migrations will be manual.

target_metadata = None

# --- modify the URL -------------------------------------------------------
# Allow the ini file to have a placeholder %(POSTGRES_URL)s which we replace
# with the actual environment variable value.
postgres_url = os.getenv("POSTGRES_URL")
if postgres_url:
    config.set_main_option("sqlalchemy.url", postgres_url.replace("%", "%%"))

def run_migrations_offline():
    """Run migrations in 'offline' mode.
    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we avoid a DB connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.
    In this scenario we create an Engine and associate a connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
