"""
AWX database query helpers for dashboard reports filter dropdowns.

Provides low-level SQL helpers and higher-level fetch functions for retrieving
organizations, job templates, projects, and labels directly from the AWX database.
"""

import enum
import logging
from typing import Any

from apps.tasks.retention import RETENTION_SETTINGS_QUERY

logger = logging.getLogger(__name__)


class AWXQuery(enum.Enum):
    """Enumeration of allowed AWX read-only SELECT queries."""

    ORGANIZATIONS = "SELECT id, name FROM main_organization"
    TEMPLATES = (
        "SELECT ujt.id, ujt.name "
        "FROM main_unifiedjobtemplate ujt "
        "JOIN main_jobtemplate jt on jt.unifiedjobtemplate_ptr_id = ujt.id"
    )
    JOB_TYPES_FOR_IDS = (
        "SELECT uj.id, dct.model "
        "FROM main_unifiedjob uj "
        "JOIN django_content_type dct ON dct.id = uj.polymorphic_ctype_id "
        "WHERE uj.id = ANY(%s)"
    )
    PROJECTS = (
        "SELECT ujt.id, ujt.name "
        "FROM main_unifiedjobtemplate ujt "
        "JOIN main_project pj on pj.unifiedjobtemplate_ptr_id = ujt.id"
    )
    # Labels are organization-scoped in AWX: unique_together = (name, organization), so the same
    # name can exist as distinct labels (different ids) across organizations. Join organization so
    # duplicate names can be disambiguated in Python (see format_label_rows / AAP-85133).
    LABELS = (
        "SELECT l.id, l.name, o.name AS organization_name "
        "FROM main_label l "
        "JOIN main_organization o ON o.id = l.organization_id"
    )

    RETENTION_SETTINGS = RETENTION_SETTINGS_QUERY


def _build_where_clause(
    join_alias: str, search_str: str | None, pk: Any, org_scope_sql: str | None = None, org_ids: list[int] | None = None
) -> tuple[str, list[Any]]:
    """Build WHERE clause and parameters for SQL query."""
    where_clauses = []
    params = []
    if search_str:
        # Escape backslash first, then ILIKE wildcards, so user-supplied % and _ are treated literally.
        escaped = search_str.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_clauses.append(f"{join_alias}name ilike %s ESCAPE E'\\\\'")
        params.append("%" + escaped + "%")
    if pk is not None:
        where_clauses.append(f"{join_alias}id = %s")
        params.append(pk)
    if org_scope_sql is not None and org_ids is not None:
        # Org-scoped dashboard access: restrict rows to the given organizations
        where_clauses.append(org_scope_sql)
        params.append(list(org_ids))
    clause = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    return clause, params


def _execute_db_query(db_connection, query: str, params: list[Any]) -> tuple[list[str], list[Any]]:
    """Execute SQL query and return columns and data."""
    with db_connection.cursor() as cursor:
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        data = cursor.fetchall()
    return columns, data


def _execute_count_query(db_connection, count_query: str, params: list[Any]) -> int:
    """Execute a COUNT query and return the integer result."""
    with db_connection.cursor() as cursor:
        cursor.execute(count_query, params)
        return cursor.fetchone()[0]


def fetch_data_from_db(
    awx_query: AWXQuery, join_alias: str = "", org_scope_sql: str | None = None, **kwargs: Any
) -> tuple[list[Any], int]:
    """
    Execute a parameterized SQL query against the AWX database with optional search, pk, limit, and offset filters.

    When ``limit`` is provided, runs a COUNT(*) subquery first to obtain the total matching row count,
    then fetches only the requested page via LIMIT/OFFSET — keeping full table scans out of Python memory.
    When ``limit`` is omitted (e.g. single-row retrieve by pk), returns all matching rows and derives
    the total from the result length.

    When ``org_scope_sql`` is given and the caller passes ``org_ids`` (dashboard org-scoped access),
    the scope clause is ANDed into the WHERE clause.

    Returns ``(rows, total_count)``.
    """
    db_connection = kwargs.get("db_connection")
    search_str = kwargs.get("search_str")
    pk = kwargs.get("pk")
    limit = kwargs.get("limit")
    offset = kwargs.get("offset", 0)
    org_ids = kwargs.get("org_ids")

    base_query = awx_query.value
    where_clause, params = _build_where_clause(join_alias, search_str, pk, org_scope_sql, org_ids)
    order_clause = f" ORDER BY {join_alias}name"

    if limit is not None:
        count_query = f"SELECT COUNT(*) FROM ({base_query}{where_clause}) AS _count_subq"  # noqa: S608 base_query is an AWXQuery enum value (hardcoded literal); where_clause uses %s placeholders
        total = _execute_count_query(db_connection, count_query, params)
        query = base_query + where_clause + order_clause + " LIMIT %s OFFSET %s"
        _, data = _execute_db_query(db_connection, query, params + [limit, offset])
    else:
        query = base_query + where_clause + order_clause
        _, data = _execute_db_query(db_connection, query, params)
        total = len(data)

    return data, total


def format_id_name_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Format rows as list of dicts with 'id' and 'name' keys."""
    return [{"id": row[0], "name": row[1]} for row in rows]


def format_label_rows(rows: list[Any], duplicate_names: set[str]) -> list[dict[str, Any]]:
    """
    Format label rows ``(id, name, organization_name)`` as list of dicts with 'id' and 'name' keys.

    Labels are organization-scoped in AWX (unique by name + organization), so the same name can
    legitimately belong to distinct labels in different organizations. Names present in
    ``duplicate_names`` are disambiguated by appending the organization name in parentheses
    (e.g. "production (Org A)", "production (Org B)"). ``duplicate_names`` must be computed from the
    complete matching dataset (not just the current page) so that a name split across pages is
    still disambiguated consistently on every page. Labels with a unique name are left unchanged.
    """
    items = []
    for row_id, name, organization_name in rows:
        display_name = f"{name} ({organization_name})" if name in duplicate_names else name
        items.append({"id": row_id, "name": display_name})
    return items


def _fetch_duplicate_label_names(
    db_connection,
    join_alias: str,
    search_str: str | None,
    pk: Any,
    org_scope_sql: str | None = None,
    org_ids: list[int] | None = None,
) -> set[str]:
    """
    Return the set of label names that occur more than once across the *complete* matching dataset.

    Uses the same WHERE filters (search/pk/org scope) as the main label query but ignores LIMIT/OFFSET, so
    that duplicate detection is correct even when results are paginated.
    """
    where_clause, params = _build_where_clause(join_alias, search_str, pk, org_scope_sql, org_ids)
    query = (
        f"SELECT name FROM ({AWXQuery.LABELS.value}{where_clause}) AS _dup_subq "  # noqa: S608 AWXQuery enum value is hardcoded; where_clause uses %s placeholders
        "GROUP BY name HAVING COUNT(*) > 1"
    )
    with db_connection.cursor() as cursor:
        cursor.execute(query, params)
        return {row[0] for row in cursor.fetchall()}


def fetch_id_name(
    awx_query: AWXQuery, join_alias: str = "", error_msg: str = "", org_scope_sql: str | None = None, **kwargs
) -> tuple[list[dict[str, Any]], int]:
    """
    Fetch id/name pairs from the AWX database and return ``(items, total_count)``.

    ``total_count`` is the DB-level COUNT when pagination params (``limit``/``offset``) are supplied,
    or the length of the result set otherwise.  Raises the underlying exception after logging on failure.
    """
    try:
        rows, total = fetch_data_from_db(awx_query, join_alias=join_alias, org_scope_sql=org_scope_sql, **kwargs)
    except Exception:
        logger.exception(error_msg)
        raise
    return format_id_name_rows(rows), total


def fetch_organizations(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch organizations from DB, returning ``(items, total_count)``."""
    return fetch_id_name(
        AWXQuery.ORGANIZATIONS,
        error_msg="Error fetching organizations from AWX database",
        org_scope_sql="id = ANY(%s)",
        **kwargs,
    )


def fetch_templates(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch job templates from DB, returning ``(items, total_count)``."""
    return fetch_id_name(
        AWXQuery.TEMPLATES,
        join_alias="ujt.",
        error_msg="Error fetching job templates from AWX database",
        org_scope_sql="ujt.organization_id = ANY(%s)",
        **kwargs,
    )


def fetch_job_types_for_ids(db_connection, job_ids: list[int]) -> dict[int, str]:
    """Return the Controller model name for each requested unified-job ID."""
    if not job_ids:
        return {}
    try:
        with db_connection.cursor() as cursor:
            cursor.execute(AWXQuery.JOB_TYPES_FOR_IDS.value, [job_ids])
            return {job_id: model for job_id, model in cursor.fetchall() if model}
    except Exception:
        logger.exception("Error fetching job types from AWX database")
        raise


def fetch_projects(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch projects from DB, returning ``(items, total_count)``."""
    return fetch_id_name(
        AWXQuery.PROJECTS,
        join_alias="ujt.",
        error_msg="Error fetching projects from AWX database",
        org_scope_sql="ujt.organization_id = ANY(%s)",
        **kwargs,
    )


def fetch_labels(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """
    Fetch labels from DB, returning ``(items, total_count)``.

    Labels are organization-scoped in AWX (unique by name + organization), so the same name can
    appear as distinct labels across organizations. Duplicate names are disambiguated by appending
    the organization name, e.g. "production (Org A)", "production (Org B)". Duplicate detection is
    computed against the full matching dataset (ignoring limit/offset) so a name split across pages
    is still disambiguated consistently.
    """
    join_alias = "l."
    try:
        rows, total = fetch_data_from_db(
            AWXQuery.LABELS, join_alias=join_alias, org_scope_sql="l.organization_id = ANY(%s)", **kwargs
        )
        duplicate_names = _fetch_duplicate_label_names(
            kwargs.get("db_connection"),
            join_alias,
            kwargs.get("search_str"),
            kwargs.get("pk"),
            "l.organization_id = ANY(%s)",
            kwargs.get("org_ids"),
        )
    except Exception:
        logger.exception("Error fetching labels from AWX database")
        raise
    return format_label_rows(rows, duplicate_names), total


def fetch_retention_settings(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch retention settings from DB, returning ``(items, total_count)``."""
    rows, total = fetch_data_from_db(
        AWXQuery.RETENTION_SETTINGS,
        join_alias="s.",
        db_connection=kwargs.get("db_connection"),
    )
    items = sorted(
        [
            {
                "job_type": row[0],
                "template_name": row[1],
                "schedule_name": row[2],
                "schedule_enabled": row[3],
                "rrule": row[4],
                "next_run": row[5],
                "retention_days": row[6],
            }
            for row in rows
        ],
        key=lambda r: (r["job_type"] or "", r["schedule_name"] or ""),
    )
    return items, total
