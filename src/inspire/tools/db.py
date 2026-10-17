"""Tool database situs: baca-saja, lewat tombol, maks 20 baris, kolom pribadi disamarkan.

Kredensial diambil dari .env situs di server dan tak pernah dikirim ke model.
Baca-saja dijaga dua lapis: SQL harus satu pernyataan SELECT/WITH/SHOW/DESCRIBE/
EXPLAIN, DAN dijalankan dalam transaksi READ ONLY (ditolak server kalau menulis).
"""

from __future__ import annotations

import re
import shlex

from . import Registry, Tool, ToolContext
from .host import Sites

MAX_ROWS = 20
PII = re.compile(
    r"(^|_)(nik|nama|name|username|alamat|address|email|e?mail|phone|hp|no_?hp|telp|telepon|"
    r"no_?telp|whatsapp|wa|password|pass|token|secret|remember|ktp|npwp|kk|no_?kk|tgl_?lahir|"
    r"tanggal_?lahir|tempat_?lahir|birth\w*|ibu_?kandung|rekening|account|ip_?address|"
    r"latitude|longitude|lat|lng)($|_)",
    re.I,
)
STRUCTURE = re.compile(r"^\s*(show|describe|desc|explain)\b", re.I)
READ = re.compile(r"^\s*(select|with)\b", re.I)
FORBIDDEN = re.compile(
    r"\binto\s+(outfile|dumpfile)\b|\bload_file\s*\(|\bfor\s+(update|share)\b|\block\s+in\b|"
    r"\bsleep\s*\(|\bbenchmark\s*\(|\bpg_sleep\b|\bexplain\s+analyze\b|\bpg_read_file\b|\bcopy\b",
    re.I,
)


def check_sql(sql: str) -> tuple[str, str]:
    """→ (jenis 'struktur'|'data', sql bersih). ValueError kalau ditolak."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise ValueError("SQL kosong")
    if ";" in sql:
        raise ValueError("hanya satu pernyataan, tanpa ';'")
    if FORBIDDEN.search(sql):
        raise ValueError("pernyataan / fungsi ini tak diizinkan")
    if STRUCTURE.match(sql):
        return "struktur", sql
    if READ.match(sql):
        return "data", sql
    raise ValueError("hanya SELECT / WITH / SHOW / DESCRIBE / EXPLAIN")


def mask_rows(tsv: str, limit: int = MAX_ROWS) -> tuple[str, list[str]]:
    lines = [ln for ln in tsv.splitlines() if ln.strip()]
    if not lines:
        return "(0 baris)", []
    header = lines[0].split("\t")
    hidden = [i for i, h in enumerate(header) if PII.search(h.strip())]
    out = [lines[0]]
    for ln in lines[1:limit + 1]:
        cells = ln.split("\t")
        for i in hidden:
            if i < len(cells) and cells[i] not in ("", "NULL"):
                cells[i] = "***"
        out.append("\t".join(cells))
    return "\n".join(out), [header[i] for i in hidden]


def build(conn: dict, sql: str, kind: str) -> tuple[str, str]:
    """→ (skrip bash, stdin). Password lewat baris pertama stdin, bukan argumen."""
    if kind == "data":
        sql = f"SELECT * FROM ({sql}) AS _q LIMIT {MAX_ROWS}"
    driver = conn.get("DB_CONNECTION", "mysql")
    q = shlex.quote
    if driver in ("mysql", "mariadb"):
        script = (f'read -r p; MYSQL_PWD="$p" mysql --batch -h {q(conn.get("DB_HOST", "127.0.0.1"))} '
                  f'-P {q(conn.get("DB_PORT", "3306"))} -u {q(conn.get("DB_USERNAME", ""))} '
                  f'{q(conn.get("DB_DATABASE", ""))}')
        body = f"SET SESSION TRANSACTION READ ONLY;\nSTART TRANSACTION READ ONLY;\n{sql};\nROLLBACK;\n"
    elif driver == "pgsql":
        script = (f'read -r p; PGPASSWORD="$p" psql -X -q -A -F "$(printf \'\\t\')" --pset footer=off '
                  f'-v ON_ERROR_STOP=1 -h {q(conn.get("DB_HOST", "127.0.0.1"))} '
                  f'-p {q(conn.get("DB_PORT", "5432"))} -U {q(conn.get("DB_USERNAME", ""))} '
                  f'-d {q(conn.get("DB_DATABASE", ""))}')
        body = f"BEGIN READ ONLY;\n{sql};\nROLLBACK;\n"
    elif driver == "sqlite":
        db = conn.get("DB_DATABASE") or "database/database.sqlite"
        script = f'read -r p; sqlite3 -readonly -header -separator "$(printf \'\\t\')" {q(db)}'
        body = f"{sql};\n"
    else:
        raise ValueError(f"DB_CONNECTION '{driver}' belum didukung")
    return script, f"{conn.get('DB_PASSWORD', '')}\n{body}"


def register_db(reg: Registry, sites: Sites) -> None:
    async def db_query(ctx: ToolContext, site: str, sql: str) -> str:
        try:
            st = sites.get(site)
            kind, sql = check_sql(sql)
        except ValueError as e:
            return f"Ditolak: {e}"
        env = await sites.env(st)
        if not env.get("DB_CONNECTION") and not env.get("DB_DATABASE"):
            return "Error: .env situs tak punya konfigurasi DB_*."
        if kind == "data":
            if ctx.is_job or not ctx.confirm:
                return "Ditolak: SELECT data butuh persetujuan user, tak bisa dari jadwal."
            if not await ctx.confirm(
                f"🗄 DB · {site}\n{sql[:2000]}\n\n(baca-saja, maks {MAX_ROWS} baris, kolom pribadi disamarkan)"
            ):
                return "User menolak. Query TIDAK dijalankan."
        try:
            script, stdin = build(env, sql, kind)
        except ValueError as e:
            return f"Error: {e}"
        root = shlex.quote(st["path"].rstrip("/"))
        code, out, err = await sites.owner_run(st, f"cd {root} && {script}", stdin=stdin.encode(), timeout=60)
        if code != 0:
            return f"Error DB: {(await sites.redact(st, err.strip()))[:1500]}"
        if kind == "struktur":
            return await sites.redact(st, out.strip()[:8000]) or "(kosong)"
        table, hidden = mask_rows(out)
        if sites.journal:
            sites.journal.add(ctx.chat_id, "db", "db_query", sql[:300], site=site)
        note = f"\n(kolom disamarkan: {', '.join(hidden)})" if hidden else ""
        return (await sites.redact(st, table))[:8000] + note

    reg.add(Tool(
        "db_query",
        "Query baca-saja ke database situs (kredensial dari .env situs). SHOW TABLES / DESCRIBE t langsung "
        f"jalan; SELECT minta persetujuan user, hasil maks {MAX_ROWS} baris, kolom data pribadi disamarkan. "
        "Satu pernyataan saja.",
        {"type": "object", "properties": {
            "site": {"type": "string"}, "sql": {"type": "string"}}, "required": ["site", "sql"]},
        db_query,
    ))
