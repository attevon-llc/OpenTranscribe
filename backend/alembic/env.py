import os
import sys
from logging.config import fileConfig

# Add the parent directory to Python path BEFORE app imports
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context  # type: ignore[attr-defined]
from app.db.base import Base

# NOTE: Alembic is the sole authority for database schema creation and upgrades.

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Setup the connection string

load_dotenv()

DB_USER = os.getenv("POSTGRES_USER", "postgres")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD", "postgres")
DB_HOST = os.getenv("POSTGRES_HOST", "postgres")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")
DB_NAME = os.getenv("POSTGRES_DB", "transcribe_app")

config.set_main_option(
    "sqlalchemy.url",
    f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}",
)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
#
# `import app.models` is LOAD-BEARING, not tidiness. `Base.metadata` is populated as a side
# effect of importing the model modules, and importing `app.db.base` alone registers NOTHING:
# measured, 0 tables before this import and 54 after. So `target_metadata = Base.metadata`
# compared the database against an EMPTY metadata, and `--autogenerate` has never worked as a
# drift check in this repo — it saw every table as absent from the models and therefore
# reported no model-side differences at all. That is why 24 database constraints could exist
# with no ORM declaration and nothing complained (issue #431, reported by the #403 work).
#
# Two things to know before relying on it now that it works:
#   * The baseline is NOT empty — roughly 800 operations of `ix_`/`idx_` naming drift, since
#     `Base` deliberately has no `naming_convention` (adding one would rename every existing
#     constraint, i.e. a schema change). So "empty autogenerate diff" is not a usable
#     acceptance criterion; "adds zero NEW operations versus the baseline" is.
#   * Model-vs-schema drift is gated by `scripts/check-schema-drift.py` (see
#     `tests/unit/test_schema_drift.py`), not by autogenerate. This import makes autogenerate
#     usable for authoring a revision; it does not replace that gate.
import app.models  # noqa: E402,F401  (side-effect import: registers every table on Base)

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

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

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
