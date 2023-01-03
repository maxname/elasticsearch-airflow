import json
from typing import Dict, Any

from prefect import flow, task
from elasticsearch import Elasticsearch
from elastic_transport import NodeConfig
from io import StringIO
import psycopg2
import yaml


class Config:
    def __init__(self, filename: str):
        with open(filename, "r") as stream:
            config = yaml.safe_load(stream)
        self._config = config["es2pg"]

    @property
    def configuration_table(self):
        return self._config["postgresql"]["configuration_table"]

    @property
    def requests(self):
        return self._config["requests"]

    def pg(self):
        return psycopg2.connect(**self._config["postgresql"]["node"])

    def es(self):
        c = self._config["elasticsearch"]
        nodes = []
        for n in c["nodes"]:
            nodes.append(NodeConfig(**n))
        auth = (c["auth"]["username"], c["auth"]["password"])
        return Elasticsearch(nodes, basic_auth=auth)

    def get_config(self, pg, code: str) -> Dict[str, Any]:
        query = "select value from {0} where code=%s limit 1".format(self.configuration_table)

        with pg.cursor() as cur:
            cur.execute(query, (code,))
            res = cur.fetchone()
            return res[0] if res else {}

    def set_config(self, pg, code: str, value: Dict[str, Any]):
        query = "insert into {0}(code, value) values (%s, %s) on conflict (code) do update set value = excluded.value"\
            .format(self.configuration_table)

        with pg.cursor() as cur:
            cur.execute(query, (code, json.dumps(value)))


@task
def check_table(config: Config, table: str):
    with config.pg() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                 SELECT count(1)
                 FROM information_schema.tables
                 WHERE table_schema = 'public' and table_name = %(table)s
            """, dict(table=table))
            res = cur.fetchone()
            return res[0] == 1


@task
def create_destination_table(config: Config, request):
    cols = []
    for prop_name, options in request["destination"]["schema"].items():
        print(options)
        col = '"{0}" {1}'.format(prop_name, options["type"])
        if "primary_key" in options and options["primary_key"]:
            col += " primary key"
        cols.append(col)
    sql_query = "create table {0}(\n\t{1}\n)".format(request["destination"]["table"], ",\n\t".join(cols))
    with config.pg() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_query)


@task
def create_meta_table(config: Config):
    with config.pg() as conn:
        with conn.cursor() as cur:
            cur.execute("create table {0}(code varchar primary key, value json null)".format(config.configuration_table))


def parse_value(row, parts):
    a = parts[0]
    b = parts[1:]
    if a in row:
        res = row[a]
        return str(res) if len(b) == 0 else parse_value(res, b)
    return None


def map_row(row, req):
    result = []
    props = req["destination"]["schema"]
    for prop_name, options in props.items():
        val = parse_value(row, options["source"])
        if val is None:
            val = "\\N"
        result.append(val)
    return "\t".join(result)


@task
def load_data(config: Config, req):
    table_name = req["destination"]["table"]
    index_pattern = req["source"]["pattern"]
    page_size = req["source"]["page_size"]
    query = req["source"]["query"]
    sort = req["source"]["sort"]
    req_name = req["name"]

    with config.pg() as pg, config.es() as es:
        opts = config.get_config(pg, req_name)
        search_after = opts["cursor"] if "cursor" in opts else None
        total = 0
        while True:
            res = es.search(index=index_pattern, size=page_size, query=query, sort=sort, search_after=search_after)
            items = res.body["hits"]["hits"]
            rows = []
            total += len(items)
            for r in items:
                rows.append(map_row(r, req))
                search_after = r["sort"]

            f = StringIO("\n".join(rows))
            with pg.cursor() as cur:
                cur.copy_from(f, table_name)

            opts["cursor"] = search_after
            config.set_config(pg, req_name, opts)
            pg.commit()

            print("Load {0} rows".format(total))

            if len(items) < page_size:
                break


@flow(name="From Elastic to PostgreSQL")
def my_flow():
    config = Config("./config.yaml")

    exists = check_table(config, config.configuration_table)
    if not exists:
        create_meta_table(config)

    for req in config.requests:
        exists = check_table(config, req["destination"]["table"])
        if not exists:
            create_destination_table(config, req)
        load_data(config, req)


my_flow()
