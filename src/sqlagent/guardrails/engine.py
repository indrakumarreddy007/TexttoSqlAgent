"""Deterministic SQL guardrail engine: the security boundary.

    result = engine.check(sql, user)   # -> GuardrailResult(allowed, violations, enforced_sql)

The LLM is treated as an untrusted SQL author. Every property below is enforced on the
parsed AST, independent of prompts:

  1. statement safety   - one read-only SELECT / set operation; no DDL/DML/PRAGMA/COPY/ATTACH/SET,
                          no table functions, no denied functions
  2. object access      - every table is catalogued, schema-qualified, permitted for the role,
                          not deprecated
  3. column access      - queries are qualified against a ROLE-SCOPED schema (denied columns removed),
                          so `SELECT *` can never expand to restricted columns and explicit references
                          to them are refused; whole-row references are rejected
  4. semantic rules     - required filters (SCD2 current/point-in-time, large tables), no cartesian joins
  5. privacy            - aggregate-only tables with minimum group size (k-anonymity)
  6. rewrite            - every base table is replaced by a policy view: permitted columns only,
                          masks applied, default + row-level filters injected; LIMIT capped

Rules are small functions registered in RULES so new ones can be added without touching
the engine (see docs/architecture.html, "Extensibility").
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from sqlagent.core.schemas import GuardrailResult, UserContext, Violation
from sqlagent.guardrails.codes import violation
from sqlagent.metadata import Catalog, Policy, Role, Table

DIALECT = "duckdb"
ALWAYS_DENIED_FUNCTIONS = {"columns"}  # DuckDB COLUMNS(regex) star expression
_FORBIDDEN_NODE_NAMES = ["Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Pragma", "Copy",
                         "Attach", "Detach", "Set", "Merge", "Use", "Transaction", "Commit", "Rollback", "Into",
                         "Export", "Describe", "Show", "Grant", "Revoke", "TruncateTable", "Install", "Load"]
FORBIDDEN_NODES = tuple(c for n in _FORBIDDEN_NODE_NAMES if (c := getattr(exp, n, None)) is not None)
ALLOWED_ROOTS = (exp.Select, exp.Union, exp.Intersect, exp.Except)
_FILE_REF_RE = re.compile(r"[/\\:]|\.(csv|tsv|parquet|json|ndjson|txt|gz|zst|db|duckdb|sqlite)$", re.IGNORECASE)
_UNRESOLVED_RE = re.compile(r"Column '([^']+)' could not be resolved|Unknown column: ([\w$]+)")


@dataclass
class CheckContext:
    sql: str
    user: UserContext
    role: Role
    catalog: Catalog
    policy: Policy
    tree: exp.Expression | None = None          # raw parse
    qualified: exp.Expression | None = None     # after qualify
    scopes: list[Scope] = field(default_factory=list)
    tables: dict[str, Table] = field(default_factory=dict)  # lowercase fqn -> Table
    masked: list[str] = field(default_factory=list)


Rule = Callable[[CheckContext], list[Violation]]


class GuardrailEngine:
    def __init__(self, catalog: Catalog, policy: Policy):
        self.catalog = catalog
        self.policy = policy
        self._schema_cache: dict[str, dict] = {}
        # rule pipelines: pre-qualify (raw AST) and post-qualify (resolved AST)
        self.pre_rules: list[Rule] = [rule_statement_safety, rule_tables, rule_denied_columns]
        self.post_rules: list[Rule] = [rule_unresolved_refs, rule_required_filters, rule_cartesian,
                                       rule_aggregate_only]

    # ------------------------------------------------------------------ public API
    def check(self, sql: str, user: UserContext) -> GuardrailResult:
        try:
            role = self.policy.role(user.role)
        except KeyError:
            return GuardrailResult(allowed=False, violations=[
                violation("V011", f"role {user.role!r} is not defined in policy")])
        ctx = CheckContext(sql=sql, user=user, role=role, catalog=self.catalog, policy=self.policy)

        parsed = self._parse(ctx)
        if isinstance(parsed, list):
            return GuardrailResult(allowed=False, violations=parsed)

        for rule in self.pre_rules:
            vs = rule(ctx)
            if vs:
                return GuardrailResult(allowed=False, violations=vs, tables=sorted(ctx.tables))

        qv = self._qualify(ctx)
        if qv:
            return GuardrailResult(allowed=False, violations=qv, tables=sorted(ctx.tables))

        violations: list[Violation] = []
        for rule in self.post_rules:
            violations.extend(rule(ctx))
        if violations:
            return GuardrailResult(allowed=False, violations=violations, tables=sorted(ctx.tables))

        enforced, filters = rewrite(ctx)
        try:  # the rewrite must itself produce exactly one valid statement
            reparsed = sqlglot.parse(enforced, read=DIALECT)
            assert len(reparsed) == 1 and isinstance(reparsed[0], ALLOWED_ROOTS)
        except Exception as e:  # pragma: no cover - defensive
            return GuardrailResult(allowed=False, violations=[violation("V001", f"rewrite failed: {e}")])
        return GuardrailResult(allowed=True, enforced_sql=enforced, tables=sorted(ctx.tables),
                               masked_columns=sorted(set(ctx.masked)), applied_row_filters=filters)

    # ------------------------------------------------------------------ steps
    def _parse(self, ctx: CheckContext) -> list[Violation] | None:
        sql = ctx.sql.strip().rstrip(";").strip()
        if not sql:
            return [violation("V001", "empty statement")]
        try:
            stmts = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        except (ParseError, TokenError) as e:
            return [violation("V001", str(e).splitlines()[0][:300], "Write a single valid DuckDB SELECT.")]
        if len(stmts) != 1:
            return [violation("V002", f"{len(stmts)} statements found; exactly one SELECT is allowed")]
        tree = stmts[0]
        while isinstance(tree, exp.Subquery) and isinstance(tree.this, ALLOWED_ROOTS):
            tree = tree.this
        ctx.tree = tree
        return None

    def _role_schema(self, role: Role) -> dict:
        if role.name not in self._schema_cache:
            exclude = {(tkey, ckey) for tkey, t in self.catalog.tables.items()
                       for ckey, c in t.columns.items() if role.action_for(c.sensitivity) == "deny"}
            permitted = {k for k, t in self.catalog.tables.items() if role.can_access_schema(t.schema)}
            self._schema_cache[role.name] = self.catalog.sqlglot_schema(exclude=exclude, tables=permitted)
        return self._schema_cache[role.name]

    def _qualify(self, ctx: CheckContext) -> list[Violation]:
        try:
            ctx.qualified = qualify(ctx.tree.copy(), schema=self._role_schema(ctx.role), dialect=DIALECT,
                                    validate_qualify_columns=True, quote_identifiers=True)
        except OptimizeError as e:
            msg = str(e)
            m = _UNRESOLVED_RE.search(msg)
            if m:
                col = (m.group(1) or m.group(2)).lower()
                for t in ctx.tables.values():
                    c = t.column(col)
                    if c and ctx.role.action_for(c.sensitivity) == "deny":
                        return [violation("V021", f"column {t.fqn}.{c.name} ({c.sensitivity}) is not permitted "
                                                  f"for role {ctx.role.name}", table=t.fqn, column=c.name)]
                return [violation("V020", f"column {col!r} does not exist in the referenced tables",
                                  _suggest_columns(col, ctx), column=col)]
            return [violation("V020", msg.splitlines()[0][:300],
                              "Qualify every column with its table alias and check names against the schema.")]
        ctx.scopes = traverse_scope(ctx.qualified)
        for scope in ctx.scopes:
            for col in scope.columns:
                src = scope.sources.get(col.table)
                if isinstance(src, exp.Table):
                    t = ctx.catalog.get(f"{src.db}.{src.name}")
                    c = t.column(col.name) if t else None
                    if c and ctx.role.action_for(c.sensitivity) == "mask":
                        ctx.masked.append(f"{t.fqn}.{c.name}")
        return []


# ====================================================================== rules
def _func_name(f: exp.Func) -> str:
    if isinstance(f, exp.Anonymous):
        return str(f.this).lower()
    return f.sql_name().lower()


def rule_statement_safety(ctx: CheckContext) -> list[Violation]:
    tree = ctx.tree
    if not isinstance(tree, ALLOWED_ROOTS):
        return [violation("V003", f"statement type {type(tree).__name__.upper()} is not allowed; only SELECT")]
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            return [violation("V003", f"{type(node).__name__.upper()} is not allowed inside a query")]
    denied = ctx.policy.denied_functions | ALWAYS_DENIED_FUNCTIONS
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            return [violation("V004", f"table functions are not allowed ({t.this.sql(DIALECT)[:60]})")]
        if _FILE_REF_RE.search(t.name):
            return [violation("V004", f"direct file references are not allowed ({t.name[:60]})")]
    for f in tree.find_all(exp.Func):
        name = _func_name(f)
        if name in denied or name.startswith(("pragma_", "duckdb_", "read_")):
            return [violation("V004", f"function {name}() is not allowed")]
    return []


def rule_tables(ctx: CheckContext) -> list[Violation]:
    tree = ctx.tree
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out: list[Violation] = []
    for t in tree.find_all(exp.Table):
        name, db, cat = t.name, t.db, t.catalog
        if not db and name.lower() in cte_names:
            continue
        if cat:
            out.append(violation("V014", f"cross-database reference {t.sql(DIALECT)} is not allowed"))
            continue
        if not db:
            matches = [x.fqn for x in ctx.catalog.tables.values() if x.name.lower() == name.lower()]
            out.append(violation("V013", f"table {name!r} must be schema-qualified",
                                 f"Use {matches[0]}" if len(matches) == 1 else "Use schema.table", table=name))
            continue
        table = ctx.catalog.get(f"{db}.{name}")
        if table is None:
            out.append(violation("V010", f"table {db}.{name} does not exist", _suggest_tables(f"{db}.{name}", ctx),
                                 table=f"{db}.{name}"))
            continue
        if not ctx.role.can_access_schema(table.schema):
            out.append(violation("V011", f"role {ctx.role.name} may not read {table.fqn} "
                                         f"(schema {table.schema})", table=table.fqn))
            continue
        if table.deprecated:
            out.append(violation("V012", f"{table.fqn} is deprecated", f"Use {table.replaced_by} instead.",
                                 table=table.fqn))
            continue
        ctx.tables[table.key] = table
    # refusals first so the agent stops instead of retrying around them
    return sorted(out, key=lambda v: v.correctable)


def rule_denied_columns(ctx: CheckContext) -> list[Violation]:
    """Refuse any reference to a column the role may not read: in projections, filters, joins,
    subqueries or CTEs. A qualifier that names a base table is checked against that table only;
    any other reference (unqualified, CTE or subquery alias) is checked against every table in
    the query (fails closed)."""
    alias_to_table: dict[str, Table] = {}
    for t in ctx.tree.find_all(exp.Table):
        table = ctx.catalog.get(f"{t.db}.{t.name}") if t.db else None
        if table is not None:
            alias_to_table[t.alias_or_name.lower()] = table
    out: list[Violation] = []
    seen: set[tuple[str, str]] = set()
    for col in ctx.tree.find_all(exp.Column):
        name = col.name.lower()
        qual = col.table.lower() if col.table else None
        candidates = [alias_to_table[qual]] if qual in alias_to_table else list(ctx.tables.values())
        for t in candidates:
            c = t.column(name)
            if c and ctx.role.action_for(c.sensitivity) == "deny" and (t.key, name) not in seen:
                seen.add((t.key, name))
                out.append(violation("V021", f"column {t.fqn}.{c.name} ({c.sensitivity}) is not permitted for "
                                             f"role {ctx.role.name}", table=t.fqn, column=c.name))
    return out


def rule_unresolved_refs(ctx: CheckContext) -> list[Violation]:
    """qualify() leaves whole-row references (`SELECT c FROM t AS c`) unqualified. Those return
    every column of the row as a STRUCT, so they are rejected."""
    q = ctx.qualified
    out = []
    table_column = getattr(exp, "TableColumn", None)
    if table_column is not None:
        for node in q.find_all(table_column):
            out.append(violation("V022", f"whole-row reference {node.sql(DIALECT)} is not allowed",
                                 "Reference explicit columns as alias.column."))
    aliases = {a.alias.lower() for a in q.find_all(exp.Alias)}
    for col in q.find_all(exp.Column):
        if col.table or col.find_ancestor(exp.Lambda):
            continue
        if col.name.lower() in aliases and not isinstance(col.parent, exp.Alias):
            continue  # reference to a projection alias (ORDER BY / HAVING)
        out.append(violation("V022", f"unresolved reference {col.name!r} (whole-row or unknown identifier)",
                             "Reference explicit columns as alias.column."))
    return out


def _filter_columns(scope: Scope, alias: str) -> set[str]:
    cols = set()
    for col in scope.columns:
        if col.table == alias and col.find_ancestor(exp.Where, exp.Join, exp.Having, exp.Qualify):
            cols.add(col.name.lower())
    return cols


def rule_required_filters(ctx: CheckContext) -> list[Violation]:
    out = []
    for scope in ctx.scopes:
        for alias, src in scope.sources.items():
            if not isinstance(src, exp.Table):
                continue
            t = ctx.catalog.get(f"{src.db}.{src.name}")
            if t is None or not t.required_filter_any:
                continue
            if not (_filter_columns(scope, alias) & set(t.required_filter_any)):
                hint = (f"Add a filter or join condition on one of {list(t.required_filter_any)} for {t.fqn}"
                        + (" (use is_current = TRUE for current attributes)" if "is_current" in
                           t.required_filter_any else ""))
                out.append(violation("V030", f"{t.fqn} requires a filter on one of {list(t.required_filter_any)}",
                                     hint, table=t.fqn))
    return out


def rule_cartesian(ctx: CheckContext) -> list[Violation]:
    out = []
    for j in ctx.qualified.find_all(exp.Join):
        if j.args.get("on") or j.args.get("using"):
            continue
        target = j.this
        if isinstance(target, (exp.Unnest, exp.Lateral)) or (
                isinstance(target, exp.Subquery) and isinstance(target.this, exp.Unnest)):
            continue
        out.append(violation("V032", f"join to {target.sql(DIALECT)[:80]} has no join condition",
                             "Use explicit JOIN ... ON with the documented join keys."))
    return out


def rule_aggregate_only(ctx: CheckContext) -> list[Violation]:
    if not ctx.role.aggregate_only:
        return []
    root = ctx.scopes[-1] if ctx.scopes else None
    out = []
    for scope in ctx.scopes:
        for alias, src in scope.sources.items():
            if not isinstance(src, exp.Table):
                continue
            key = f"{src.db}.{src.name}".lower()
            rule = ctx.role.aggregate_only.get(key)
            if rule is None:
                continue
            t = ctx.catalog.get(key)
            if scope is not root or not isinstance(scope.expression, exp.Select):
                out.append(violation("V041", f"{t.fqn} must be read directly in the outermost SELECT "
                                             "(not in a CTE, subquery or set operation)",
                                     "Restructure: aggregate the table in the top-level query.", table=t.fqn))
                continue
            sel: exp.Select = scope.expression
            sensitive = {c for c, col in t.columns.items() if col.sensitivity == "pii_sensitive"}
            grouped = sel.args.get("group") is not None or any(
                e.find(exp.AggFunc) for e in sel.expressions)
            if not grouped:
                out.append(violation("V040", f"{t.fqn} may only be queried as an aggregate "
                                             f"(groups of at least {rule['min_group_size']})", table=t.fqn))
                continue
            for col in scope.columns:
                if col.table != alias or col.name.lower() not in sensitive:
                    continue
                agg = col.find_ancestor(exp.AggFunc, exp.Select)
                win = col.find_ancestor(exp.Window, exp.Select)
                in_group = col.find_ancestor(exp.Group, exp.Select)
                if isinstance(in_group, exp.Group) or isinstance(win, exp.Window) or not isinstance(agg, exp.AggFunc):
                    if col.find_ancestor(exp.Where, exp.Select) and isinstance(
                            col.find_ancestor(exp.Where, exp.Select), exp.Where):
                        continue  # filtering on the value is fine; groups are still k-anonymous
                    out.append(violation("V040", f"{t.fqn}.{col.name} may only appear inside an aggregate "
                                                 "function (not grouped, windowed or listed per row)",
                                         table=t.fqn, column=col.name))
    return out


# ====================================================================== rewrite
def _regions_sql(user: UserContext) -> str:
    # regions are validated against a fixed allow-list in UserContext, so quoting is safe
    return ", ".join(f"'{r}'" for r in user.regions) if user.regions else "NULL"


def rewrite(ctx: CheckContext) -> tuple[str, list[str]]:
    q = ctx.qualified.copy()
    applied: list[str] = []
    base_tables = [t for t in q.find_all(exp.Table)
                   if t.db and isinstance(t.this, exp.Identifier) and ctx.catalog.get(f"{t.db}.{t.name}")]
    for node in base_tables:
        t = ctx.catalog.get(f"{node.db}.{node.name}")
        cols = []
        for c in t.columns.values():
            action = ctx.role.action_for(c.sensitivity)
            if action == "deny":
                continue
            ident = f'"{c.name.lower()}"'
            cols.append(f"'***' AS {ident}" if action == "mask" else ident)
        preds = []
        if t.default_filter:
            preds.append(f"({t.default_filter})")
        rf = ctx.role.row_filters.get(t.key)
        if rf:
            preds.append("(" + rf.replace("{regions}", _regions_sql(ctx.user)) + ")")
            applied.append(f"{t.fqn}: {rf.replace('{regions}', _regions_sql(ctx.user))}")
        sub_sql = f'SELECT {", ".join(cols)} FROM "{t.schema.lower()}"."{t.name.lower()}"'
        if preds:
            sub_sql += " WHERE " + " AND ".join(preds)
        sub = sqlglot.parse_one(sub_sql, read=DIALECT)
        alias = node.alias_or_name
        node.replace(exp.Subquery(this=sub, alias=exp.TableAlias(this=exp.to_identifier(alias, quoted=True))))

    # k-anonymity: HAVING COUNT(DISTINCT alias.entity_key) >= k on the outer SELECT
    if ctx.role.aggregate_only and isinstance(q, exp.Select):
        root_scope = ctx.scopes[-1]
        for alias, src in root_scope.sources.items():
            if isinstance(src, exp.Table):
                rule = ctx.role.aggregate_only.get(f"{src.db}.{src.name}".lower())
                if rule:
                    cond = sqlglot.parse_one(
                        f'COUNT(DISTINCT "{alias}"."{rule["entity_key"]}") >= {int(rule["min_group_size"])}',
                        read=DIALECT)
                    q.having(cond, append=True, copy=False)
                    applied.append(f"k-anonymity: groups with >= {rule['min_group_size']} distinct "
                                   f"{rule['entity_key']}")

    cap = ctx.role.max_rows
    if isinstance(q, exp.Select):
        lim = q.args.get("limit")
        cur = lim.expression if lim is not None else None
        if not (isinstance(cur, exp.Literal) and cur.is_int and int(cur.name) <= cap):
            q.limit(cap, copy=False)
        sql = q.sql(dialect=DIALECT)
    else:  # set operation: wrap
        sql = f"SELECT * FROM ({q.sql(dialect=DIALECT)}) AS _q LIMIT {cap}"
    return sql, applied


# ====================================================================== hints
def _suggest_columns(name: str, ctx: CheckContext) -> str | None:
    candidates = {}
    for t in ctx.tables.values():
        for c in t.columns.values():
            if ctx.role.action_for(c.sensitivity) != "deny":
                candidates[c.name.lower()] = t.fqn
    close = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.5)
    if close:
        return "Did you mean " + ", ".join(f"{candidates[c]}.{c}" for c in close) + "?"
    return "Available columns: " + ", ".join(sorted(candidates))[:400]


def _suggest_tables(name: str, ctx: CheckContext) -> str | None:
    keys = [t.fqn for t in ctx.catalog.tables.values() if not t.deprecated and ctx.role.can_access_schema(t.schema)]
    close = difflib.get_close_matches(name.lower(), [k.lower() for k in keys], n=3, cutoff=0.4)
    lookup = {k.lower(): k for k in keys}
    return ("Did you mean " + ", ".join(lookup[c] for c in close) + "?") if close else None
