"""Diz, no topo da saída do pytest, se os testes que precisam do PostgreSQL vão rodar."""
import os

from psycopg.conninfo import conninfo_to_dict


def pytest_report_header(config):
    dsn = os.getenv("PRUMO_TESTE_PG")
    if not dsn:
        return "PostgreSQL: PRUMO_TESTE_PG ausente; os testes de banco serão PULADOS (veja -rs)"
    partes = conninfo_to_dict(dsn)
    return (f"PostgreSQL: {partes.get('host', 'local')}:{partes.get('port', 5432)}"
            f"/{partes.get('dbname', '')}; os testes de banco vão rodar")
