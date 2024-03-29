import importlib.metadata
import signal
import subprocess
import sys
from pathlib import Path

import click
import toml
from dotenv import find_dotenv, load_dotenv
from hologram import ValidationError

from datawagon.commands.compare import (
    compare_local_files_to_bucket,
    compare_local_files_to_postgres,
)
from datawagon.commands.file_zip_to_gzip import file_zip_to_gzip
from datawagon.commands.files_in_database import files_in_database
from datawagon.commands.files_in_local_fs import files_in_local_fs
from datawagon.commands.files_in_storage import files_in_storage
from datawagon.commands.import_all_csv import import_all_csv
from datawagon.commands.import_single_csv import import_selected_csv
from datawagon.commands.reset_database import reset_database
from datawagon.commands.upload_to_storage import upload_all_gzip_csv
from datawagon.database.postgres_database_manager import PostgresDatabaseManager
from datawagon.objects.app_config import AppConfig
from datawagon.objects.parameter_validator import ParameterValidator
from datawagon.objects.source_config import SourceConfig


@click.group(chain=True)
@click.option("--db-url", type=str, help="Database URL", envvar="DW_POSTGRES_DB_URL")
@click.option("--db-schema", type=str, help="Schema name to use", envvar="DW_DB_SCHEMA")
@click.option(
    "--csv-source-dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, readable=True),
    help="Source directory containing .csv, .csv.zip or .csv.gz files.",
    envvar="DW_CSV_SOURCE_DIR",
)
@click.option(
    "--csv-source-config",
    type=str,
    help="Location of source_config.toml",
    envvar="DW_CSV_SOURCE_TOML",
)
@click.option(
    "--gcs-project-id",
    type=str,
    help="Project ID for Google Cloud Storage",
    envvar="DW_GCS_PROJECT_ID",
)
@click.option(
    "--gcs-bucket",
    type=str,
    help="Bucket used for Google Cloud Storage",
    envvar="DW_GCS_BUCKET",
)
@click.pass_context
def cli(
    ctx: click.Context,
    db_url: str,
    db_schema: str,
    csv_source_dir: Path,
    csv_source_config: Path,
    gcs_project_id: str,
    gcs_bucket: str,
) -> None:
    if not ParameterValidator(
        db_url, db_schema, csv_source_dir, csv_source_config
    ).are_valid_parameters:
        ctx.abort()

    # TODO: fix error handling, this is not working
    # load config from toml file
    try:
        source_config_file = toml.load(csv_source_config)
        valid_config = SourceConfig(**source_config_file)

    except ValidationError as e:
        raise ValueError(f"Validation Failed for source_config.toml\n{e}")

    if not valid_config:
        ctx.abort()

    ctx.obj["FILE_CONFIG"] = valid_config

    app_config = AppConfig(
        db_schema=db_schema,
        csv_source_dir=csv_source_dir,
        csv_source_config=csv_source_config,
        db_url=db_url,
        gcs_project_id=gcs_project_id,
        gcs_bucket=gcs_bucket,
        # bucket_storage_url=bucket_storage_url
    )

    db_manager = PostgresDatabaseManager(app_config)

    # if on mac, prevent computer from sleeping (display, system, disk)
    if "darwin" in sys.platform:
        proc = subprocess.Popen(["caffeinate", "-dim"])

    ctx.obj["DB_CONNECTION"] = db_manager

    is_valid_db = check_db_connection(db_manager=db_manager)
    is_valid_schema = (
        check_schema(db_manager=db_manager, schema_name=db_schema)
        if is_valid_db
        else False
    )

    if not is_valid_schema:
        ctx.abort()

    if is_valid_db and is_valid_schema:
        db_manager.create_log_table()

    ctx.obj["CONFIG"] = app_config
    ctx.obj["GLOBAL"] = {}

    def on_exit() -> None:
        if is_valid_db:
            db_manager.close()
        if proc:
            proc.send_signal(signal.SIGTERM)

    ctx.call_on_close(on_exit)


cli.add_command(reset_database)
cli.add_command(files_in_database)
cli.add_command(files_in_local_fs)
cli.add_command(compare_local_files_to_postgres)
cli.add_command(compare_local_files_to_bucket)
cli.add_command(upload_all_gzip_csv)
cli.add_command(file_zip_to_gzip)
cli.add_command(import_all_csv)
cli.add_command(import_selected_csv)
cli.add_command(reset_database)
cli.add_command(files_in_storage)


def start_cli() -> click.Group:
    env_file = find_dotenv()

    load_dotenv(env_file)

    click.secho("DataWagon", fg="magenta", bold=True)
    click.echo(f"Version: {importlib.metadata.version('datawagon')}")
    click.secho(f"Configuration loaded from: {env_file}")
    click.echo(nl=True)

    return cli(obj={})  # type: ignore


def check_db_connection(db_manager: PostgresDatabaseManager) -> bool:
    """Test the connection to the database."""

    if db_manager.is_valid_connection:
        click.secho("Successfully connected to the database.", fg="green")
        click.echo(nl=True)
        return True
    else:
        click.secho("Failed to connect to the database.", fg="red")
        click.echo(nl=True)
        return False


def check_schema(db_manager: PostgresDatabaseManager, schema_name: str) -> bool:
    """Check if the schema exists and prompt to create if it does not."""

    # This will try to create schema if it does not exist
    if not ensure_schema_exists(db_manager, schema_name):
        click.secho(f"Schema '{schema_name}' must exist. Exiting.", fg="red")
        click.echo(nl=True)
        return False

    click.echo(nl=True)
    click.secho(f"'{schema_name}' is valid schema.", fg="green")
    click.echo(nl=True)
    return True


def ensure_schema_exists(db_manager: PostgresDatabaseManager, schema_name: str) -> bool:
    if not db_manager.check_schema():
        click.secho(f"Schema '{schema_name}' does not exist in the database.", fg="red")
        if click.confirm("Create the schema?"):
            db_manager.ensure_schema_exists()
            if db_manager.check_schema():
                click.secho(f"Schema '{schema_name}' created.", fg="green")
                return True
            else:
                click.secho("Schema creation failed.", fg="red")
                return False
        else:
            return False
    else:
        return True


if __name__ == "__main__":
    start_cli()
