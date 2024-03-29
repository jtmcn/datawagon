import csv
from io import StringIO
from typing import Any, Iterable, List, Literal, Union

import pandas as pd
import psycopg2
from psycopg2.sql import SQL, Identifier
from sqlalchemy import Numeric, create_engine

from datawagon.objects.app_config import AppConfig


class PostgresDatabaseManager:
    """
    Database handler for PostgreSQL databases.

    There are two ways to load data into a PostgreSQL database:
    1. Using the pandas to_sql method
    2. Using the psycopg2 copy_from method

    to_sql loads data line by line, which is not fault tolerant.
    A failure will result in a partial load. Therefore,
    copy_from should be used for loading csv files into the database
    as it loads the entire file at once or not at all.

    There are SQL queries with f-strings in this class. This is not ideal, but
    the only user input is the schema name, which is validated before usage.
    """

    CNAME_FILE_NAME = "_file_name"

    INDEX_COLUMN_NAME = CNAME_FILE_NAME

    LOG_TABLE_NAME = "log"

    def __init__(self, app_config: AppConfig) -> None:
        self.connection_error = ""
        self.schema = app_config.db_schema
        self.hostname = ""
        self.db_name = app_config.db_url.split("/")[-1]

        try:
            self.connection = psycopg2.connect(app_config.db_url)
            self.connection.set_session(autocommit=True)
            self.hostname = self.connection.info.host
        except psycopg2.OperationalError as e:
            self.connection_error = str(e)

        try:
            self.engine = create_engine(app_config.db_url, isolation_level="AUTOCOMMIT")
        except Exception as e:
            self.connection_error = str(e)

        self.is_valid_connection = self.test_connection()

    def close(self) -> None:
        if not self.connection_error:
            self.connection.close()

    def test_connection(self) -> bool:
        if self.connection_error:
            print(f"Connection error: {self.connection_error}")
            return False
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("select 1")
                cursor.close()
            return True
        except psycopg2.Error:
            return False

    def check_schema(self) -> bool:
        query = SQL("select exists(select 1 from pg_namespace where nspname = %s);")

        with self.connection.cursor() as cursor:
            cursor.execute(query, (self.schema,))
            results = cursor.fetchone()
            exists = results[0] if results is not None else False
            cursor.close()
            return exists

    def does_log_table_exist(self) -> bool:
        return self.check_table(self.LOG_TABLE_NAME)

    def check_table(self, table_name: str) -> bool:
        query = SQL(
            """
            select exists(
                select tablename
                from pg_tables
                where
                    schemaname = %s
                    and
                    tablename = %s
                limit 1
            );
            """
        )

        with self.connection.cursor() as cursor:
            cursor.execute(query, (self.schema, table_name))
            results = cursor.fetchone()
            exists = results[0] if results is not None else False
            cursor.close()
            return exists

    def ensure_schema_exists(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(SQL("create schema if not exists {}".format(self.schema)))
            cursor.close()

    def load_dataframe_into_database(
        self,
        df: pd.DataFrame,
        table_name: str,
        append_or_replace: Literal["append", "replace"] = "append",
    ) -> int:
        # float will cause floating point precision issues in reporting, cast to numeric
        dtype_dict = {}
        for col in df.columns:
            if df[col].dtype == "float64":
                dtype_dict[col] = Numeric(precision=19, scale=7)

        try:
            df.to_sql(
                name=table_name,
                schema=self.schema,
                con=self.engine,
                if_exists=append_or_replace,
                index=False,
                method=self._df_to_pg_copy,
                dtype=dtype_dict,
            )

            self.log_operation(
                f"Loaded {len(df)} rows into {self.schema}.{table_name}",
                df.iloc[0][self.CNAME_FILE_NAME],
            )

            return len(df)

        except (Exception, psycopg2.DatabaseError) as error:
            self.log_operation(
                f"Failed to load dataframe into {table_name}", str(error)
            )
            print(f"Table Update Failed: {error}")
            return -1

    def _df_to_pg_copy(
        self,
        table: Any,
        conn: Any,
        keys: list[str],
        data_iter: Iterable[tuple[Any, ...]],
    ) -> None:
        # Convert the DataFrame iterable (back) into CSV format
        buffer = StringIO()
        writer = csv.writer(buffer)
        writer.writerows(data_iter)
        buffer.seek(0)

        raw_connection = conn.connection
        with raw_connection.cursor() as cursor:
            sql = SQL(
                "copy {} from stdin with csv".format(f"{table.schema}.{table.name}")
            )

            cursor.copy_expert(sql=sql, file=buffer)
            cursor.connection.commit()
            cursor.close()

    def drop_schema(self) -> None:
        with self.connection.cursor() as cursor:
            sql = SQL("drop schema if exists {} cascade;").format(
                Identifier(self.schema)
            )
            cursor.execute(sql)
            cursor.close()

    def drop_all_tables_and_views(self) -> None:
        # 7/20/23 - unused, replaced by drop_schema
        with self.connection.cursor() as cursor:
            sql = f"""
                do $$ declare
                    r record;
                begin
                    for r in (select tablename from pg_tables where schemaname = '{self.schema}')

                    loop
                        execute 'drop table if exists {self.schema}.' || r.tablename || ' cascade';
                    end loop;
                    for r in (select viewname from pg_views where schemaname = '{self.schema}')
                    loop
                        execute 'drop view if exists {self.schema}.' || r.viewname || ' cascade';
                    end loop;
                end $$;
                """

            cursor.execute(sql)
            cursor.close()

    def check_if_file_imported(self, file_name: str, table_name: str) -> bool:
        if self.check_table(table_name):
            with self.connection.cursor() as cursor:
                table = (self.schema, table_name)
                query = SQL(
                    "select exists(select 1 from {} where _file_name=%s)"
                ).format(Identifier(*table))
                cursor.execute(query, (file_name,))
                result = cursor.fetchone()
                cursor.close()
                return result[0] if result else False
        else:
            return False

    def files_in_table_df(self, table_name: str) -> pd.DataFrame:
        query = f"""
                select
                    '{table_name}' as table_name,
                    {self.CNAME_FILE_NAME}
                from {self.schema}.{table_name}
                group by
                    {self.CNAME_FILE_NAME}
            """

        return pd.read_sql(query, self.engine)

    def table_names(self) -> List[str]:
        query = """
                select
                    table_name
                from information_schema.tables
                where
                    table_schema = %(schema)s
                    and not table_name = %(log_table_name)s
                order by
                    table_name
            """
        params = {"schema": self.schema, "log_table_name": self.LOG_TABLE_NAME}
        df = pd.read_sql_query(query, self.engine, params=params)
        return df["table_name"].tolist()

    def create_log_table(self) -> None:
        if not self.check_table(self.LOG_TABLE_NAME):
            query = SQL(
                """
                create table if not exists {} (
                    timestamp timestamp default current_timestamp,
                    operation text not null,
                    details text
                );
                """
            )
            with self.connection.cursor() as cursor:
                cursor.execute(
                    query.format(Identifier(self.schema, self.LOG_TABLE_NAME))
                )
                cursor.close()

    def log_operation(self, operation: str, details: Union[str, None] = None) -> None:
        query = SQL(
            """
            insert into {} (operation, details)
            values (%s, %s);
            """
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                query.format(Identifier(self.schema, self.LOG_TABLE_NAME)),
                (operation, details),
            )
            cursor.close()
