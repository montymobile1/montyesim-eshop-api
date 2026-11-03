"""supabase_ddl.sql is the only schema file (team decision): no migration folder next to it, and
no table, function, trigger, index, view, type or policy defined twice in it. A second definition
hides which one is current; with CREATE OR REPLACE the later one silently wins."""
import re
import subprocess
from collections import Counter

import pytest

from tests.rls.test_rls_ddl_assertion import DDL, ROOT, statements

NAME = r'(?:"?public"?\.)?("?[A-Za-z_0-9]+"?(?:\."?[A-Za-z_0-9]+"?)?)'


def _arg_types(args: str) -> str:
    """``p_id integer, p_tags uuid[] default null`` -> ``integer,uuid[]`` (an overload is another object)."""
    types = []
    for arg in filter(None, (a.strip() for a in re.split(r",(?![^(]*\))", args))):
        arg = re.split(r"\s+default\s+|\s*=\s*", arg)[0]
        words = re.sub(r"^(in|out|inout|variadic)\s+", "", arg).split(" ", 1)
        types.append(words[-1].replace(" ", ""))
    return ",".join(types)


def definitions(sql: str):
    """(kind, name) of every CREATE in ``sql``, schema public left out of the name."""
    found = []
    for stmt in statements(sql):
        if m := re.match(rf"create (?:unlogged )?table (?:if not exists )?{NAME}", stmt):
            found.append(("table", m[1]))
        elif m := re.match(rf"create (?:or replace )?(?:function|procedure) {NAME} ?\((.*?)\) (?:returns|language|as)", stmt):
            found.append(("function", f"{m[1]}({_arg_types(m[2])})"))
        elif m := re.match(rf'create policy ("[^"]*"|\S+) on {NAME}', stmt):
            found.append(("policy", f"{m[2]}.{m[1]}"))
        elif m := re.match(rf"create (?:or replace )?(?:constraint )?trigger (\S+) .*? on {NAME}", stmt):
            found.append(("trigger", f"{m[2]}.{m[1]}"))
        elif m := re.match(r"create (?:unique )?index (?:concurrently )?(?:if not exists )?(\S+) on", stmt):
            found.append(("index", m[1]))
        elif m := re.match(rf"create (?:or replace )?(?:materialized )?view (?:if not exists )?{NAME}", stmt):
            found.append(("view", m[1]))
        elif m := re.match(rf"create (?:type|sequence (?:if not exists )?){NAME}", stmt):
            found.append(("type or sequence", m[1]))
    return found


def test_nothing_is_defined_twice():
    twice = sorted(f"{kind} {name}" for (kind, name), n in Counter(definitions(DDL.read_text())).items() if n > 1)
    assert twice == []


def test_supabase_ddl_is_the_only_sql_file():
    try:
        tracked = subprocess.run(["git", "ls-files", "*.sql"], cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    assert tracked == ["supabase_ddl.sql"]


@pytest.mark.parametrize("sql,expected", [
    ("create function f(a int) returns int as $$ $$; create or replace function f(b int) returns int as $$ $$;",
     ["function f(int)"]),
    ("create table t (id int); create table if not exists public.t (id int);", ["table t"]),
    ('create policy "p" on storage.objects using (true); create policy "p" on storage.buckets using (true);', []),
    ("create function f(a int) returns int as $$ $$; create function f(a int, b text default 'x') returns int as $$ $$;",
     []),
])
def test_duplicates_are_recognised(sql, expected):
    assert sorted(f"{k} {n}" for (k, n), c in Counter(definitions(sql)).items() if c > 1) == expected
