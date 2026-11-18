"""
Litho - a small ORM for sqlite, libSQL and Turso, in one file.

Quick tour:

    from litho import Database, Model, TextField, IntegerField, ForeignKey, Q

    db = Database("app.db")                      # driver="sqlite" is the default
    # db = Database("app.db", driver="libsql")   # libSQL
    # db = Database("app.db", driver="turso")    # Turso Database (Rust engine)

    class User(Model):
        name = TextField(unique=True)
        age = IntegerField(null=True)

    class Post(Model):
        author = ForeignKey(User, backref="posts")
        title = TextField()

    db.create_tables(User, Post)                 # also binds the models to db

    ann = User.create(name="Ann", age=30)
    Post.create(author=ann, title="Hello")
    User.filter(age__gte=18).order_by("-age").all()
    User.filter(Q(name="Ann") | Q(age__lt=5)).count()
    ann.posts.count()                            # reverse side of the foreign key

    with db.atomic():                            # transaction (nested = savepoint)
        User.create(name="Bob")

Rules of thumb: column names always come from your model fields and every value
is sent as a bound parameter, so user input can never change the SQL.
"""

from __future__ import annotations

import copy
import datetime
import importlib
import json
import re
import sqlite3
import threading
from contextlib import contextmanager

__version__ = "0.0.1b2"

__all__ = [
    "__version__",
    "Database", "Model", "Q", "Query", "Result",
    "Field", "IntegerField", "FloatField", "TextField", "BooleanField",
    "DateTimeField", "DateField", "JSONField", "BlobField", "VectorField", "ForeignKey",
    "LithoError", "DatabaseError", "IntegrityError", "ValidationError", "FieldError",
    "UnsupportedError", "DoesNotExist", "MultipleObjectsReturned",
]


# ======================================================================================
# Errors
# ======================================================================================

class LithoError(Exception):
    """Base class of every error Litho raises."""


class DatabaseError(LithoError):
    """The database rejected a statement (bad SQL, locked, missing table ...)."""


class IntegrityError(DatabaseError):
    """A UNIQUE, NOT NULL, CHECK or FOREIGN KEY rule was broken."""


class ValidationError(LithoError, ValueError):
    """A value does not fit its field (checked before anything reaches the database)."""


class FieldError(LithoError):
    """Unknown field name, bad lookup, or a mistake in a model definition."""


class UnsupportedError(LithoError):
    """The chosen driver cannot do this (for example vectors on plain sqlite)."""


class DoesNotExist(LithoError):
    """get() found nothing. Every model also has its own subclass: User.DoesNotExist."""


class MultipleObjectsReturned(LithoError):
    """get() found more than one row."""


# ======================================================================================
# Small helpers
# ======================================================================================

def quote(name):
    """Quote a table or column name for SQL."""
    return '"' + str(name).replace('"', '""') + '"'


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _literal(value):
    """A value as an SQL literal, for DEFAULT clauses. None if it cannot be one."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


# ======================================================================================
# Fields
# ======================================================================================

class Field:
    """One column. Subclasses decide the SQL type and how values are converted."""

    sql_type = "TEXT"
    placeholder = "?"          # how a value is written in INSERT/UPDATE
    auto_now = False
    auto_now_add = False

    def __init__(self, null=False, default=None, unique=False, index=False,
                 primary_key=False, choices=None):
        self.null = null
        self.default = default
        self.unique = unique
        self.index = index
        self.primary_key = primary_key
        self.choices = choices
        self.name = self.column = None

    def __set_name__(self, owner, name):
        self.name = name
        self.column = self._column_for(name)

    def _column_for(self, name):
        return name

    def get_default(self):
        return self.default() if callable(self.default) else copy.deepcopy(self.default)

    def ph(self, value):
        """Placeholder for one value (NULL is always a plain ?)."""
        return "?" if value is None else self.placeholder

    def select_sql(self):
        return quote(self.column)

    def clean(self, value):
        """Check a Python value and return it in its final Python form."""
        return value

    def to_db(self, value):
        return value

    def from_db(self, value):
        return value


class IntegerField(Field):
    sql_type = "INTEGER"

    def clean(self, value):
        if isinstance(value, float) and not value.is_integer():
            raise ValidationError(f"{self.name}: {value!r} is not a whole number")
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{self.name}: {value!r} is not an integer") from None

    def to_db(self, value):
        # libSQL's Python driver sends ints above 2**53 as floats and loses digits.
        # Text in an INTEGER column is turned into an exact integer by the database.
        return str(value) if abs(value) > 2 ** 53 else value


class FloatField(Field):
    sql_type = "REAL"

    def clean(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{self.name}: {value!r} is not a number") from None


class TextField(Field):
    def __init__(self, max_length=None, **kwargs):
        super().__init__(**kwargs)
        self.max_length = max_length

    def clean(self, value):
        if not isinstance(value, str):
            raise ValidationError(f"{self.name}: expected text, got {type(value).__name__}")
        if self.max_length is not None and len(value) > self.max_length:
            raise ValidationError(f"{self.name}: longer than {self.max_length} characters")
        return value


class BooleanField(Field):
    sql_type = "INTEGER"

    def clean(self, value):
        if value in (0, 1) and not isinstance(value, float):
            return bool(value)
        raise ValidationError(f"{self.name}: {value!r} is not a boolean")

    def to_db(self, value):
        return int(value)

    def from_db(self, value):
        return bool(value)


class DateTimeField(Field):
    """Stored as ISO-8601 text. auto_now_add: set on creation. auto_now: set on every save."""

    def __init__(self, auto_now=False, auto_now_add=False, **kwargs):
        if (auto_now or auto_now_add) and kwargs.get("default") is None:
            kwargs["default"] = utcnow
        super().__init__(**kwargs)
        self.auto_now = auto_now
        self.auto_now_add = auto_now_add

    def clean(self, value):
        if isinstance(value, datetime.datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.datetime.fromisoformat(value)
            except ValueError:
                pass
        raise ValidationError(f"{self.name}: {value!r} is not a datetime")

    def to_db(self, value):
        return value.isoformat()

    def from_db(self, value):
        return datetime.datetime.fromisoformat(value)


class DateField(Field):
    def clean(self, value):
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if isinstance(value, str):
            try:
                return datetime.date.fromisoformat(value)
            except ValueError:
                pass
        raise ValidationError(f"{self.name}: {value!r} is not a date")

    def to_db(self, value):
        return value.isoformat()

    def from_db(self, value):
        return datetime.date.fromisoformat(value)


class JSONField(Field):
    """Any JSON-serialisable value (dict, list, str, number, bool), stored as text."""

    def clean(self, value):
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            raise ValidationError(f"{self.name}: value is not JSON-serialisable") from None
        return value

    def to_db(self, value):
        return json.dumps(value, ensure_ascii=False)

    def from_db(self, value):
        return json.loads(value)


class BlobField(Field):
    sql_type = "BLOB"

    def clean(self, value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        raise ValidationError(f"{self.name}: expected bytes, got {type(value).__name__}")

    def from_db(self, value):
        return bytes(value)


class VectorField(Field):
    """A float32 vector with a fixed size. libSQL and Turso only. Use Query.nearest()."""

    sql_type = "BLOB"
    placeholder = "vector32(?)"

    def __init__(self, dim, **kwargs):
        super().__init__(**kwargs)
        if not isinstance(dim, int) or dim < 1:
            raise FieldError("VectorField needs a size, for example VectorField(3)")
        self.dim = dim

    def select_sql(self):
        col = quote(self.column)
        return f"CASE WHEN {col} IS NULL THEN NULL ELSE vector_extract({col}) END"

    def clean(self, value):
        if not isinstance(value, (list, tuple)) or len(value) != self.dim:
            raise ValidationError(f"{self.name}: expected a list of {self.dim} numbers")
        try:
            return [float(x) for x in value]
        except (TypeError, ValueError):
            raise ValidationError(f"{self.name}: vector must contain only numbers") from None

    def to_db(self, value):
        return json.dumps(value)

    def from_db(self, value):
        return json.loads(value)


class ForeignKey(Field):
    """Points at another model. The column is called <name>_id; <name> loads the row lazily."""

    ACTIONS = ("CASCADE", "SET NULL", "RESTRICT", "NO ACTION")

    def __init__(self, model, on_delete="CASCADE", backref=None, **kwargs):
        kwargs.setdefault("index", True)
        super().__init__(**kwargs)
        if on_delete not in self.ACTIONS:
            raise FieldError(f"on_delete must be one of {self.ACTIONS}")
        if on_delete == "SET NULL" and not self.null:
            raise FieldError("on_delete='SET NULL' needs null=True")
        self.model = model
        self.on_delete = on_delete
        self.backref = backref

    @property
    def sql_type(self):
        return self.model._pk.sql_type

    def _column_for(self, name):
        return name + "_id"

    def clean(self, value):
        if isinstance(value, Model):
            if value.pk is None:
                raise ValidationError(f"{self.name}: save the {type(value).__name__} first")
            value = value.pk
        return self.model._pk.clean(value)

    def to_db(self, value):
        return self.model._pk.to_db(value)

    def from_db(self, value):
        return self.model._pk.from_db(value)

    # post.author  -> loads the User (cached while the id stays the same)
    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        pk = obj.__dict__.get(self.column)
        if pk is None:
            return None
        cache_key = "_cache_" + self.name
        cached = obj.__dict__.get(cache_key)
        if cached is None or cached[0] != pk:
            cached = (pk, self.model.get(pk))
            obj.__dict__[cache_key] = cached
        return cached[1]

    # post.author = user  (or an id, or None)
    def __set__(self, obj, value):
        obj.__dict__[self.column] = value.pk if isinstance(value, Model) else value
        obj.__dict__.pop("_cache_" + self.name, None)


class _BackRef:
    """user.posts -> a Query for the posts of that user."""

    def __init__(self, model, fk_name):
        self.model, self.fk_name = model, fk_name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        return self.model.query().filter(**{self.fk_name: obj})


# ======================================================================================
# Filters: lookups like age__gte=18, and Q objects to combine them
# ======================================================================================

SIMPLE_OPS = {"eq": "=", "ne": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
LOOKUPS = set(SIMPLE_OPS) | {"in", "not_in", "like", "contains", "startswith", "endswith",
                             "isnull", "between"}


def _escape_like(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _lookup(model, key, value):
    """Turn one keyword like age__gte=18 into (sql, params)."""
    parts = key.split("__")
    field = model._field_for(parts[0])
    rest = parts[1:]
    col = quote(field.column)

    # Follow a foreign key:  Post.filter(author__name="Ann")
    if isinstance(field, ForeignKey) and rest and rest[0] not in LOOKUPS:
        sub_sql, params = Q(**{"__".join(rest): value}).compile(field.model)
        target = field.model
        return (f"{col} IN (SELECT {quote(target._pk.column)} FROM {quote(target._table)} "
                f"WHERE {sub_sql})", params)

    if len(rest) > 1 or (rest and rest[0] not in LOOKUPS):
        raise FieldError(f"Bad lookup {key!r} on {model.__name__}")
    op = rest[0] if rest else "eq"

    def dbv(v):
        return field.to_db(field.clean(v))

    if op in SIMPLE_OPS:
        if value is None and op in ("eq", "ne"):
            return f"{col} IS {'NOT ' if op == 'ne' else ''}NULL", []
        return f"{col} {SIMPLE_OPS[op]} ?", [dbv(value)]
    if op in ("in", "not_in"):
        values = [dbv(v) for v in value]
        if not values:
            return ("0=1" if op == "in" else "1=1"), []
        marks = ", ".join("?" for _ in values)
        return f"{col} {'NOT ' if op == 'not_in' else ''}IN ({marks})", values
    if op == "isnull":
        return f"{col} IS {'NULL' if value else 'NOT NULL'}", []
    if op == "between":
        low, high = value
        return f"{col} BETWEEN ? AND ?", [dbv(low), dbv(high)]
    if op == "like":
        return f"{col} LIKE ?", [str(value)]
    text = _escape_like(str(value))
    pattern = {"contains": f"%{text}%", "startswith": f"{text}%", "endswith": f"%{text}"}[op]
    return f"{col} LIKE ? ESCAPE '\\'", [pattern]


class Q:
    """Combine conditions:  Q(a=1) | Q(b=2),  Q(a=1) & Q(b=2),  ~Q(a=1)."""

    def __init__(self, *children, **lookups):
        self.op = "AND"
        self.children = list(children)
        self.lookups = lookups
        self.negated = False

    @staticmethod
    def _combine(op, left, right):
        q = Q(left, right)
        q.op = op
        return q

    def __and__(self, other):
        return Q._combine("AND", self, other)

    def __or__(self, other):
        return Q._combine("OR", self, other)

    def __invert__(self):
        q = Q(self)
        q.negated = True
        return q

    def compile(self, model):
        parts, params = [], []
        for child in self.children:
            sql, p = child.compile(model)
            parts.append(sql)
            params += p
        for key, value in self.lookups.items():
            sql, p = _lookup(model, key, value)
            parts.append(sql)
            params += p
        sql = "(" + f" {self.op} ".join(parts) + ")" if parts else "1=1"
        return (f"NOT {sql}" if self.negated else sql), params


# ======================================================================================
# Query: a lazy description of a SELECT / UPDATE / DELETE
# ======================================================================================

class Query:
    """Build it with filter()/order_by()/limit(); it runs when you ask for results."""

    def __init__(self, model):
        self.model = model
        self._qs = []
        self._order = []
        self._limit = None
        self._offset = 0
        self._distinct = False
        self._extra = None          # (sql, params) of one extra selected column

    def _clone(self):
        c = Query(self.model)
        c._qs, c._order = list(self._qs), list(self._order)
        c._limit, c._offset, c._distinct, c._extra = (
            self._limit, self._offset, self._distinct, self._extra)
        return c

    # ---- building ------------------------------------------------------------------
    def filter(self, *qs, **lookups):
        c = self._clone()
        c._qs.extend(qs)
        if lookups:
            c._qs.append(Q(**lookups))
        return c

    def exclude(self, *qs, **lookups):
        c = self._clone()
        c._qs.append(~Q(*qs, **lookups))
        return c

    def order_by(self, *names):
        c = self._clone()
        c._order = []
        for name in names:
            desc = name.startswith("-")
            field = self.model._field_for(name[1:] if desc else name)
            c._order.append(f"{quote(field.column)} {'DESC' if desc else 'ASC'}")
        return c

    def limit(self, n):
        c = self._clone()
        c._limit = self._whole(n)
        return c

    def offset(self, n):
        c = self._clone()
        c._offset = self._whole(n)
        return c

    def paginate(self, page, per_page=20):
        if self._whole(page) < 1 or self._whole(per_page) < 1:
            raise FieldError("page and per_page must be 1 or more")
        return self.limit(per_page).offset((page - 1) * per_page)

    def distinct(self):
        c = self._clone()
        c._distinct = True
        return c

    def nearest(self, name, vector, k=10, metric="cos"):
        """The k rows closest to `vector` (libSQL / Turso). Each row gets a .distance."""
        field = self.model._field_for(name)
        if not isinstance(field, VectorField):
            raise FieldError(f"{name!r} is not a VectorField")
        if metric not in ("cos", "l2"):
            raise FieldError("metric must be 'cos' or 'l2'")
        if self.model._need_db().driver == "sqlite":
            raise UnsupportedError("Vector search needs driver='libsql' or driver='turso'.")
        value = field.to_db(field.clean(vector))
        c = self.filter(**{f"{field.name}__isnull": False}).limit(k)
        c._extra = (f'vector_distance_{metric}({quote(field.column)}, vector32(?)) '
                    f'AS "_distance"', [value])
        c._order = ['"_distance" ASC']
        return c

    @staticmethod
    def _whole(n):
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise FieldError(f"expected a whole number of 0 or more, got {n!r}")
        return n

    # ---- SQL -----------------------------------------------------------------------
    def _where(self):
        if not self._qs:
            return "", []
        parts, params = [], []
        for item in self._qs:
            sql, p = item.compile(self.model)
            parts.append(sql)
            params += p
        return " WHERE " + " AND ".join(parts), params

    def _select(self, columns, column_params=(), order=True, paging=True):
        where, params = self._where()
        sql = ("SELECT " + ("DISTINCT " if self._distinct else "") + columns
               + " FROM " + quote(self.model._table) + where)
        params = list(column_params) + params
        if order and self._order:
            sql += " ORDER BY " + ", ".join(self._order)
        if paging and (self._limit is not None or self._offset):
            sql += f" LIMIT {self._limit if self._limit is not None else -1}"
            if self._offset:
                sql += f" OFFSET {self._offset}"
        return sql, params

    # ---- running -------------------------------------------------------------------
    def all(self):
        fields = list(self.model._fields.values())
        columns = [f.select_sql() for f in fields]
        extra_params = []
        if self._extra:
            columns.append(self._extra[0])
            extra_params = self._extra[1]
        sql, params = self._select(", ".join(columns), extra_params)
        result = self.model._need_db().execute(sql, params)
        return [self.model._from_row(row) for row in result.rows]

    def __iter__(self):
        return iter(self.all())

    def get(self, *qs, **lookups):
        rows = self.filter(*qs, **lookups).limit(2).all()
        if not rows:
            raise self.model.DoesNotExist(f"{self.model.__name__} matching query does not exist")
        if len(rows) > 1:
            raise MultipleObjectsReturned(f"get() found more than one {self.model.__name__}")
        return rows[0]

    def first(self):
        query = self if self._order else self.order_by("pk")
        rows = query.limit(1).all()
        return rows[0] if rows else None

    def count(self):
        inner, params = self._select(quote(self.model._pk.column))
        return self.model._need_db().execute(f"SELECT COUNT(*) FROM ({inner})", params).scalar()

    def exists(self):
        sql, params = self.limit(1)._select("1", order=False)
        return bool(self.model._need_db().execute(sql, params).rows)

    def values(self, *names):
        """List of dicts. With no names: every column."""
        fields = [self.model._field_for(n) for n in names] or list(self.model._fields.values())
        keys = list(names) or [f.column for f in fields]
        sql, params = self._select(", ".join(f.select_sql() for f in fields))
        rows = self.model._need_db().execute(sql, params).rows
        return [{k: (None if v is None else f.from_db(v)) for k, f, v in zip(keys, fields, row)}
                for row in rows]

    def column(self, name):
        """List of the values of one column."""
        return [row[name] for row in self.values(name)]

    def _aggregate(self, function, name):
        field = self.model._field_for(name)
        sql, params = self._select(f"{function}({quote(field.column)})", order=False, paging=False)
        value = self.model._need_db().execute(sql, params).scalar()
        if value is not None and function in ("MIN", "MAX"):
            value = field.from_db(value)
        return value

    def sum(self, name):
        return self._aggregate("SUM", name)

    def avg(self, name):
        return self._aggregate("AVG", name)

    def min(self, name):
        return self._aggregate("MIN", name)

    def max(self, name):
        return self._aggregate("MAX", name)

    # ---- changing rows (hooks and signals do not run for these) -------------------------
    def _check_bulk(self, what):
        if self._limit is not None or self._offset:
            raise FieldError(f"{what}() cannot be combined with limit()/offset()/paginate()")

    def update(self, **values):
        """UPDATE every matching row. Returns the number of rows changed."""
        self._check_bulk("update")
        model = self.model
        for field in model._fields.values():
            if field.auto_now and field.name not in values:
                values[field.name] = utcnow()
        sets, params = [], []
        for name, value in values.items():
            field = model._field_for(name)
            if field.primary_key:
                raise FieldError("update() cannot change the primary key")
            if value is None:
                if not field.null:
                    raise ValidationError(f"{model.__name__}.{field.name} cannot be null")
                db_value = None
            else:
                db_value = field.to_db(field.clean(value))
            sets.append(f"{quote(field.column)} = {field.ph(db_value)}")
            params.append(db_value)
        if not sets:
            return 0
        where, where_params = self._where()
        sql = f"UPDATE {quote(model._table)} SET {', '.join(sets)}{where}"
        return model._need_db().execute(sql, params + where_params).rowcount

    def increment(self, name, by=1):
        """column = column + by, for every matching row. Returns the number of rows changed."""
        self._check_bulk("increment")
        field = self.model._field_for(name)
        if not isinstance(field, (IntegerField, FloatField)):
            raise FieldError(f"{name!r} is not a number field")
        where, params = self._where()
        col = quote(field.column)
        sql = f"UPDATE {quote(self.model._table)} SET {col} = {col} + ?{where}"
        return self.model._need_db().execute(sql, [field.clean(by)] + params).rowcount

    def delete(self):
        """DELETE every matching row. Returns the number of rows removed."""
        self._check_bulk("delete")
        where, params = self._where()
        sql = f"DELETE FROM {quote(self.model._table)}{where}"
        return self.model._need_db().execute(sql, params).rowcount


# ======================================================================================
# Model
# ======================================================================================

class Model:
    """Subclass it and add fields. An `id` primary key is added for you.

    Optional settings go in an inner class:

        class Meta:
            table = "my_table"                  # default: the class name in snake_case
            indexes = [("title", "price")]      # extra indexes
            unique_together = [("title", "author")]
    """

    _db = None
    _fields = {}
    DoesNotExist = DoesNotExist

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        own = {n: v for n, v in vars(cls).items() if isinstance(v, Field)}
        fields = {}
        for base in reversed(cls.__mro__[1:]):
            fields.update(vars(base).get("_fields", {}))
        for name in own:
            if name.startswith("_") or "__" in name or hasattr(Model, name):
                raise FieldError(f"{cls.__name__}.{name}: this field name is not allowed")
        fields.update(own)

        if sum(f.primary_key for f in fields.values()) > 1:
            raise FieldError(f"{cls.__name__} has more than one primary key")
        if not any(f.primary_key for f in fields.values()):
            if "id" in fields:
                raise FieldError(f"{cls.__name__}.id is taken: add primary_key=True or rename it")
            auto = IntegerField(primary_key=True)
            auto.__set_name__(cls, "id")
            fields = {"id": auto, **fields}
            cls.id = auto
        columns = [f.column for f in fields.values()]
        if len(columns) != len(set(columns)):
            raise FieldError(f"{cls.__name__} has two fields that use the same column")

        cls._fields = fields
        cls._pk = next(f for f in fields.values() if f.primary_key)
        cls._by_column = {f.column: f for f in fields.values()}
        meta = vars(cls).get("Meta")
        cls._table = getattr(meta, "table", None) or _snake(cls.__name__)

        specs = [(False, [f.column]) for f in fields.values()
                 if f.index and not f.unique and not f.primary_key]
        for unique, names in ((False, getattr(meta, "indexes", [])),
                              (True, getattr(meta, "unique_together", []))):
            for group in names:
                specs.append((unique, [cls._field_for(n).column for n in group]))
        cls._index_specs = specs

        cls.DoesNotExist = type("DoesNotExist", (DoesNotExist,), {"__module__": cls.__module__})
        for field in own.values():
            if isinstance(field, ForeignKey) and field.backref:
                if hasattr(field.model, field.backref):
                    raise FieldError(f"{field.model.__name__} already has {field.backref!r}")
                setattr(field.model, field.backref, _BackRef(cls, field.name))

    # ---- creating instances --------------------------------------------------------
    def __init__(self, **values):
        cls = type(self)
        for field in cls._fields.values():
            self.__dict__[field.column] = field.get_default()
        for key, value in values.items():
            try:
                field = cls._field_for(key)
            except FieldError:
                raise TypeError(f"{cls.__name__}() got an unexpected field {key!r}") from None
            if key == "pk":
                self.pk = value
            elif isinstance(field, ForeignKey) and key == field.column:
                self.__dict__[field.column] = value          # author_id=5
            else:
                setattr(self, field.name, value)

    @classmethod
    def _from_row(cls, row):
        obj = cls.__new__(cls)
        fields = list(cls._fields.values())
        for field, raw in zip(fields, row):
            obj.__dict__[field.column] = None if raw is None else field.from_db(raw)
        if len(row) > len(fields):               # the extra column of nearest()
            obj.distance = row[len(fields)]
        return obj

    @classmethod
    def _field_for(cls, name):
        if name == "pk":
            return cls._pk
        field = cls._fields.get(name) or cls._by_column.get(name)
        if field is None:
            raise FieldError(f"{cls.__name__} has no field {name!r}")
        return field

    @classmethod
    def _need_db(cls):
        if cls._db is None:
            raise LithoError(f"{cls.__name__} is not bound to a database yet: "
                             f"call db.create_tables({cls.__name__}) or db.bind({cls.__name__})")
        return cls._db

    @property
    def pk(self):
        return self.__dict__.get(self._pk.column)

    @pk.setter
    def pk(self, value):
        self.__dict__[self._pk.column] = value

    # ---- class-level shortcuts -----------------------------------------------------
    @classmethod
    def query(cls):
        return Query(cls)

    @classmethod
    def all(cls):
        return Query(cls).all()

    @classmethod
    def filter(cls, *qs, **lookups):
        return Query(cls).filter(*qs, **lookups)

    @classmethod
    def exclude(cls, *qs, **lookups):
        return Query(cls).exclude(*qs, **lookups)

    @classmethod
    def order_by(cls, *names):
        return Query(cls).order_by(*names)

    @classmethod
    def count(cls):
        return Query(cls).count()

    @classmethod
    def get(cls, pk=None, **lookups):
        if pk is not None:
            lookups["pk"] = pk
        return Query(cls).get(**lookups)

    @classmethod
    def get_or_none(cls, pk=None, **lookups):
        try:
            return cls.get(pk, **lookups)
        except DoesNotExist:
            return None

    @classmethod
    def create(cls, **values):
        obj = cls(**values)
        obj.save(force_insert=True)
        return obj

    @classmethod
    def get_or_create(cls, defaults=None, **values):
        """Returns (object, created). `values` must be plain field names (no __lookups)."""
        db = cls._need_db()
        with db.atomic():
            found = cls.get_or_none(**values)
            if found is not None:
                return found, False
            try:
                with db.atomic():
                    return cls.create(**{**values, **(defaults or {})}), True
            except IntegrityError:            # someone else created it a moment ago
                return cls.get(**values), False

    @classmethod
    def upsert(cls, conflict, **values):
        """Insert, or update the existing row whose unique field `conflict` matches."""
        db = cls._need_db()
        obj = cls(**values)
        key_field = cls._field_for(conflict)
        pairs = [(f, v) for f, v in obj._values() if not (f.primary_key and v is None)]
        marks = ", ".join(f.ph(v) for f, v in pairs)
        names = ", ".join(quote(f.column) for f, _ in pairs)
        updates = [f"{quote(f.column)} = excluded.{quote(f.column)}" for f, _ in pairs
                   if f is not key_field and not f.primary_key and not f.auto_now_add]
        action = "DO UPDATE SET " + ", ".join(updates) if updates else "DO NOTHING"
        db.execute(f"INSERT INTO {quote(cls._table)} ({names}) VALUES ({marks}) "
                   f"ON CONFLICT ({quote(key_field.column)}) {action}", [v for _, v in pairs])
        return cls.get(**{key_field.column: obj.__dict__[key_field.column]})

    @classmethod
    def bulk_create(cls, items, batch_size=500):
        """Insert many rows fast (instances or dicts). All or nothing. Returns the row count.
        Hooks do not run and new ids are not written back to the objects."""
        db = cls._need_db()
        objs = [i if isinstance(i, cls) else cls(**i) for i in items]
        groups = {}
        for obj in objs:
            pairs = [(f, v) for f, v in obj._values() if not (f.primary_key and v is None)]
            sql = (f"INSERT INTO {quote(cls._table)} ({', '.join(quote(f.column) for f, _ in pairs)}) "
                   f"VALUES ({', '.join(f.ph(v) for f, v in pairs)})")
            groups.setdefault(sql, []).append([v for _, v in pairs])
        with db.atomic():
            for sql, rows in groups.items():
                for start in range(0, len(rows), batch_size):
                    db.execute_many(sql, rows[start:start + batch_size])
        return len(objs)

    # ---- hooks: override in your model ----------------------------------------------
    def before_save(self):
        pass

    def after_save(self):
        pass

    def before_delete(self):
        pass

    def after_delete(self):
        pass

    # ---- instance methods ------------------------------------------------------------
    def _values(self):
        """[(field, value_for_db)] after checking every value."""
        out = []
        for f in self._fields.values():
            value = self.__dict__.get(f.column)
            if value is None:
                if f.primary_key and not isinstance(f, IntegerField):
                    raise ValidationError(f"{type(self).__name__}.{f.name} is the primary key and needs a value")
                if not f.null and not f.primary_key:
                    raise ValidationError(f"{type(self).__name__}.{f.name} cannot be null")
                out.append((f, None))
                continue
            value = f.clean(value)
            if f.choices and value not in f.choices:
                raise ValidationError(f"{type(self).__name__}.{f.name}: {value!r} is not one of {list(f.choices)}")
            self.__dict__[f.column] = value
            out.append((f, f.to_db(value)))
        return out

    def save(self, force_insert=False):
        """INSERT a new row, or UPDATE the row with this primary key."""
        cls = type(self)
        db = cls._need_db()
        self.before_save()
        for f in cls._fields.values():
            if f.auto_now:
                self.__dict__[f.column] = utcnow()
        pairs = self._values()
        pk_column = quote(cls._pk.column)

        if self.pk is not None and not force_insert:
            sets = [(f, v) for f, v in pairs if not f.primary_key]
            changed = 0
            if sets:
                sql = (f"UPDATE {quote(cls._table)} SET "
                       + ", ".join(f"{quote(f.column)} = {f.ph(v)}" for f, v in sets)
                       + f" WHERE {pk_column} = ?")
                params = [v for _, v in sets] + [cls._pk.to_db(self.pk)]
                changed = db.execute(sql, params).rowcount
            if not changed:        # nothing matched (or the driver reports 0): is the row there?
                changed = Query(cls).filter(pk=self.pk).exists()
            if changed:
                self.after_save()
                return self

        pairs = [(f, v) for f, v in pairs if not (f.primary_key and v is None)]
        if pairs:
            sql = (f"INSERT INTO {quote(cls._table)} ({', '.join(quote(f.column) for f, _ in pairs)}) "
                   f"VALUES ({', '.join(f.ph(v) for f, v in pairs)})")
        else:
            sql = f"INSERT INTO {quote(cls._table)} DEFAULT VALUES"
        result = db.execute(sql, [v for _, v in pairs])
        if self.pk is None:
            self.pk = result.lastrowid
        self.after_save()
        return self

    def delete(self):
        """DELETE this row. Returns the number of rows removed."""
        cls = type(self)
        if self.pk is None:
            raise LithoError("This object has not been saved, so there is nothing to delete.")
        self.before_delete()
        count = Query(cls).filter(pk=self.pk).delete()
        self.after_delete()
        return count

    def refresh(self):
        """Reload every field from the database."""
        fresh = type(self).get(self.pk)
        self.__dict__.update(fresh.__dict__)
        for key in [k for k in self.__dict__ if k.startswith("_cache_")]:
            del self.__dict__[key]
        return self

    def to_dict(self):
        return {f.column: self.__dict__.get(f.column) for f in self._fields.values()}

    def __repr__(self):
        return f"<{type(self).__name__} {self._pk.name}={self.pk!r}>"

    def __eq__(self, other):
        if self is other:
            return True
        return isinstance(other, type(self)) and self.pk is not None and self.pk == other.pk

    def __hash__(self):
        return hash((type(self).__name__, self.pk)) if self.pk is not None else id(self)


# ======================================================================================
# Database
# ======================================================================================

class Result:
    """What db.execute() returns."""

    def __init__(self, columns, rows, lastrowid, rowcount):
        self.columns, self.rows = columns, rows
        self.lastrowid, self.rowcount = lastrowid, rowcount

    def dicts(self):
        return [dict(zip(self.columns, row)) for row in self.rows]

    def scalar(self):
        """The first value of the first row (or None)."""
        return self.rows[0][0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        return self.rows[index]


def _import(module, package):
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise LithoError(f"The {module!r} driver is not installed. Run: uv add {package}") from e


class Database:
    """One connection to a sqlite, libSQL or Turso database. Safe to share between threads.

        Database("app.db")                                  # sqlite
        Database("app.db", driver="libsql")                 # libSQL
        Database("app.db", driver="turso")                  # Turso Database
        Database("app.db", driver="turso", mvcc=True)       # + concurrent writers

    Extra keyword arguments go to the driver, for example:
        libsql:  sync_url=..., auth_token=..., encryption_key=...    (embedded replica)
        turso:   remote_url=..., auth_token=...                      (local-first sync)
                 encryption=("aegis256", "<64 hex characters>")
    """

    DRIVERS = ("sqlite", "libsql", "turso")

    def __init__(self, path=":memory:", driver="sqlite", *, mvcc=False, echo=False, **options):
        if driver not in self.DRIVERS:
            raise LithoError(f"driver must be one of {self.DRIVERS}, not {driver!r}")
        if mvcc and driver != "turso":
            raise UnsupportedError("mvcc=True (concurrent writers) is a Turso feature.")
        self.path, self.driver, self.options, self.echo = path, driver, options, echo
        self._lock = threading.RLock()
        self._depth = 0                  # 0 = not inside atomic()
        self._closed = False
        self._remote = False             # True when the connection syncs with a server
        self._conn = self._connect()
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if mvcc:     # the result must be read, or Turso quietly goes back to wal mode
                self._conn.execute("PRAGMA journal_mode = 'mvcc'").fetchall()
        except Exception as e:           # for example: wrong encryption key
            self._conn.close()
            raise self._wrap(e) from e

    def __repr__(self):
        return f"<Database {self.driver} {self.path!r}{' closed' if self._closed else ''}>"

    # ---- connection ----------------------------------------------------------------
    def _connect(self):
        opts = dict(self.options)
        try:
            if self.driver == "sqlite":
                opts.setdefault("check_same_thread", False)
                return sqlite3.connect(self.path, **opts)
            if self.driver == "libsql":
                opts.setdefault("_check_same_thread", False)
                return _import("libsql", "libsql").connect(self.path, **opts)
            turso = _import("turso", "pyturso")
            encryption = opts.pop("encryption", None)
            if encryption:
                opts["encryption"] = turso.EncryptionOpts(cipher=encryption[0], hexkey=encryption[1])
                opts.setdefault("experimental_features", "encryption")
            if "remote_url" in opts:
                self._remote = True
                return _import("turso.sync", "pyturso").connect(self.path, **opts)
            return turso.connect(self.path, **opts)
        except TypeError as e:
            raise UnsupportedError(f"The {self.driver} driver does not accept these options: {e}") from e
        except LithoError:
            raise
        except Exception as e:
            raise self._wrap(e) from e

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _wrap(error):
        """Turn any driver error into a Litho error (libSQL raises plain ValueError)."""
        if isinstance(error, LithoError):
            return error
        message = str(error) or type(error).__name__
        if type(error).__name__ == "IntegrityError" or "constraint failed" in message.lower():
            return IntegrityError(message)
        return DatabaseError(message)

    def _call(self, function, *args):
        """Run function(*args) under the lock; commit if we are not inside atomic()."""
        with self._lock:
            if self._closed:
                raise LithoError("This database is closed.")
            try:
                result = function(*args)
                if self._depth == 0:
                    self._conn.commit()
                return result
            except Exception as e:
                if self._depth == 0:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                wrapped = self._wrap(e)
                if wrapped is e:
                    raise
                raise wrapped from e

    # ---- running SQL ---------------------------------------------------------------
    def execute(self, sql, params=()):
        """Run one statement with ? placeholders. Returns a Result."""
        def run():
            if self.echo:
                print(sql, tuple(params))
            cursor = self._conn.execute(sql, tuple(params))
            if cursor.description:
                columns = [d[0] for d in cursor.description]
                rows = [tuple(r) for r in (cursor.fetchall() or [])]
            else:
                columns, rows = [], []
            return Result(columns, rows, cursor.lastrowid, cursor.rowcount)
        return self._call(run)

    def execute_many(self, sql, rows):
        """Run one statement once per row of parameters. Returns the number of rows."""
        rows = [tuple(r) for r in rows]
        if not rows:
            return 0

        def run():
            if self.echo:
                print(sql, f"x {len(rows)} rows")
            self._conn.executemany(sql, rows)
            return len(rows)
        return self._call(run)

    def query(self, sql, params=()):
        """Run a SELECT and get a list of dicts."""
        return self.execute(sql, params).dicts()

    # ---- transactions --------------------------------------------------------------
    @property
    def in_transaction(self):
        return self._depth > 0

    @contextmanager
    def atomic(self, concurrent=False):
        """All-or-nothing block. Nested blocks become savepoints.
        concurrent=True (Turso with mvcc=True only) lets several connections write at once."""
        if concurrent and self.driver != "turso":
            raise UnsupportedError("atomic(concurrent=True) is a Turso feature.")
        with self._lock:
            outer = self._depth == 0
            if concurrent and not outer:
                raise LithoError("concurrent=True only works on the outermost atomic().")
            name = f"litho_sp{self._depth}"
            begin = ("BEGIN CONCURRENT" if concurrent else "BEGIN") if outer else f"SAVEPOINT {name}"
            self._depth += 1
            try:
                self._call(self._conn.execute, begin)
            except BaseException:
                self._depth -= 1
                raise
            try:
                yield self
            except BaseException:
                self._depth -= 1
                if outer:
                    try:
                        self._conn.rollback()
                    except Exception:
                        pass
                else:
                    self._call(self._conn.execute, f"ROLLBACK TO {name}")
                    self._call(self._conn.execute, f"RELEASE {name}")
                raise
            self._depth -= 1
            if outer:
                self._conn.commit()
            else:
                self._call(self._conn.execute, f"RELEASE {name}")

    # ---- tables --------------------------------------------------------------------
    def bind(self, *models):
        for model in models:
            if not (isinstance(model, type) and issubclass(model, Model)):
                raise LithoError(f"{model!r} is not a Model class")
            model._db = self

    def create_tables(self, *models, safe=True):
        """Create the tables and indexes (safe=True: skip tables that already exist)."""
        self.bind(*models)
        for model in models:
            for field in model._fields.values():
                if isinstance(field, VectorField) and self.driver == "sqlite":
                    raise UnsupportedError("VectorField needs driver='libsql' or driver='turso'.")
        with self.atomic():
            for model in models:
                for sql in self._create_sql(model, safe):
                    self.execute(sql)

    def drop_tables(self, *models):
        """Drop tables. Pass them in the same order as create_tables (dropped in reverse)."""
        with self.atomic():
            for model in reversed(models):
                self.execute(f"DROP TABLE IF EXISTS {quote(model._table)}")

    def tables(self):
        rows = self.execute("SELECT name FROM sqlite_master WHERE type = 'table'").rows
        return sorted(r[0] for r in rows
                      if not r[0].startswith("sqlite_") and not r[0].startswith("__turso"))

    def table_exists(self, table):
        return (table if isinstance(table, str) else table._table) in self.tables()

    def columns(self, table):
        name = table if isinstance(table, str) else table._table
        return [r[1] for r in self.execute(f"PRAGMA table_info({quote(name)})").rows]

    def indexes(self, table):
        name = table if isinstance(table, str) else table._table
        rows = self.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
                            [name]).rows
        return sorted(r[0] for r in rows)

    # ---- schema changes ------------------------------------------------------------
    def add_missing_columns(self, *models):
        """Add columns that exist on the model but not in the table. Returns what was added.
        A new column must be null=True or have a plain default, and cannot be unique."""
        added = []
        self.bind(*models)
        with self.atomic():
            for model in models:
                existing = set(self.columns(model))
                for field in model._fields.values():
                    if field.column in existing:
                        continue
                    default = self._default_sql(field)
                    if field.primary_key or field.unique:
                        raise LithoError(f"Cannot add {model._table}.{field.column}: "
                                         "primary key / unique columns need a new table")
                    if not field.null and default is None:
                        raise LithoError(f"Cannot add {model._table}.{field.column}: give the field "
                                         "null=True or a plain default (number, text, True/False)")
                    self.execute(f"ALTER TABLE {quote(model._table)} ADD COLUMN "
                                 f"{self._column_sql(field)}")
                    if field.index:
                        self.execute(self._index_sql(model, False, [field.column], True))
                    added.append(f"{model._table}.{field.column}")
        return added

    def rename_column(self, table, old, new):
        self.execute(f"ALTER TABLE {quote(table)} RENAME COLUMN {quote(old)} TO {quote(new)}")

    def drop_column(self, table, column):
        self.execute(f"ALTER TABLE {quote(table)} DROP COLUMN {quote(column)}")

    def rename_table(self, old, new):
        self.execute(f"ALTER TABLE {quote(old)} RENAME TO {quote(new)}")

    # ---- SQL generation --------------------------------------------------------------
    @staticmethod
    def _default_sql(field):
        if field.default is None or callable(field.default) or field.primary_key \
                or isinstance(field, ForeignKey):
            return None
        return _literal(field.to_db(field.clean(field.default)))

    def _column_sql(self, field):
        parts = [quote(field.column), field.sql_type]
        if field.primary_key:
            parts.append("PRIMARY KEY AUTOINCREMENT" if isinstance(field, IntegerField)
                         else "PRIMARY KEY NOT NULL")
            return " ".join(parts)
        if not field.null:
            parts.append("NOT NULL")
        if field.unique:
            parts.append("UNIQUE")
        default = self._default_sql(field)
        if default is not None:
            parts.append(f"DEFAULT {default}")
        if isinstance(field, ForeignKey):
            target = field.model
            parts.append(f"REFERENCES {quote(target._table)}({quote(target._pk.column)}) "
                         f"ON DELETE {field.on_delete}")
        return " ".join(parts)

    @staticmethod
    def _index_sql(model, unique, columns, safe):
        name = f"{model._table}_{'_'.join(columns)}_{'uniq' if unique else 'idx'}"
        return (f"CREATE {'UNIQUE ' if unique else ''}INDEX {'IF NOT EXISTS ' if safe else ''}"
                f"{quote(name)} ON {quote(model._table)} ({', '.join(quote(c) for c in columns)})")

    def _create_sql(self, model, safe):
        columns = ",\n  ".join(self._column_sql(f) for f in model._fields.values())
        statements = [f"CREATE TABLE {'IF NOT EXISTS ' if safe else ''}{quote(model._table)} "
                      f"(\n  {columns}\n)"]
        for unique, cols in model._index_specs:
            statements.append(self._index_sql(model, unique, cols, True))
        return statements

    # ---- syncing with a server (needs a real libSQL / Turso Cloud database) ----------
    def sync(self):
        """libSQL embedded replica: pull from the server. Turso: pull, then push."""
        if self.driver == "libsql" and self.options.get("sync_url"):
            self._sync_call(self._conn.sync)
        elif self._remote:
            self._sync_call(self._conn.pull)
            self._sync_call(self._conn.push)
        else:
            raise UnsupportedError("sync() needs libsql(sync_url=...) or turso(remote_url=...).")

    def push(self):
        """Turso only: send local changes to the server."""
        return self._sync_remote("push")

    def pull(self):
        """Turso only: fetch remote changes. Returns True if anything changed."""
        return self._sync_remote("pull")

    def _sync_remote(self, name):
        if not self._remote:
            raise UnsupportedError(f"{name}() needs driver='turso' with remote_url=...")
        return self._sync_call(getattr(self._conn, name))

    def _sync_call(self, function):
        with self._lock:
            if self._depth:
                raise LithoError("Cannot sync inside atomic().")
            try:
                return function()
            except Exception as e:
                raise self._wrap(e) from e
