# Litho documentation

Nov 18, 2026 · @Amrit

Litho is a fast, lightweight Python ORM for SQLite, libSQL and Turso with vector search, transactions, and local-first sync.

## Overview and Install

Install Litho directly from PyPI:

```bash
# Core package (uses stdlib sqlite3 with zero external dependencies)
pip install litho-orm
# or with uv:
uv add litho-orm
```

Install with optional database driver extras:

```bash
# Turso Rust engine (pyturso)
pip install "litho-orm[turso]"
# or: uv add litho-orm --extra turso

# libSQL engine (libsql)
pip install "litho-orm[libsql]"
# or: uv add litho-orm --extra libsql

# Both drivers
pip install "litho-orm[turso,libsql]"
```

Litho can also be used as a standalone single file (`litho.py` or `main.py`) dropped directly into any project without installation.

| Driver | `driver=` | Python package | Features |
| --- | --- | --- | --- |
| SQLite | `"sqlite"` (default) | stdlib `sqlite3` | Zero dependencies, instant setup. |
| libSQL | `"libsql"` | `libsql` | Float32 vectors, database encryption, direct remote connection & embedded replicas with sync. |
| Turso | `"turso"` | `pyturso` (as `turso`) | High performance Rust engine: float32 vectors, MVCC concurrent writers, encryption, local-first push/pull sync. |

Then import what you need in your code:

```python
from litho import Database, Model, TextField, IntegerField, ForeignKey, Q
```

Two rules hold everywhere:
1. Table and column names always come from your model definitions.
2. Every value is sent as a bound parameter, so user input can never alter the SQL. Queries are lazy: nothing touches the database until you iterate, count, fetch or write.

## Quickstart

This example runs seamlessly on all three drivers (SQLite, libSQL, and Turso); change only the `driver=` argument:

```python
from litho import Database, Model, TextField, IntegerField, ForeignKey, Q

db = Database("app.db")                      # driver="sqlite" by default
# db = Database("app.db", driver="libsql")
# db = Database("app.db", driver="turso")

class User(Model):
    name = TextField(unique=True)
    age = IntegerField(null=True)

class Post(Model):
    author = ForeignKey(User, backref="posts")
    title = TextField()

db.create_tables(User, Post)                 # creates tables & indexes and binds models to db

ann = User.create(name="Ann", age=30)
Post.create(author=ann, title="Hello")

User.filter(age__gte=18).order_by("-age").all()      # [<User id=1>]
User.filter(Q(name="Ann") | Q(age__lt=5)).count()    # 1
ann.posts.count()                                    # 1 (reverse side of foreign key)

with db.atomic():                            # transaction (nested = savepoint)
    User.create(name="Bob")
```

What happened: `create_tables` made `user` and `post` tables (names default to snake\_case of the class name), added an auto-increment `id` primary key to each, and created an index on `post.author_id`. `create` inserted a row and returned an object with its `id` populated.

## Connecting

`Database(path=":memory:", driver="sqlite", *, mvcc=False, echo=False, **options)` opens one connection. Models passed to `db.create_tables(...)` or `db.bind(...)` are bound to that database.

```python
db = Database("app.db")                         # local SQLite file
db = Database(":memory:")                       # in-memory database
db = Database("app.db", driver="libsql")        # local libSQL database
db = Database("app.db", driver="turso")         # local Turso database
db = Database("app.db", echo=True)              # log SQL statements and parameters

# Direct remote libSQL database (e.g., Turso Cloud)
db = Database("libsql://your-db.turso.io", driver="libsql", auth_token="...")

with Database("app.db") as db:                  # auto-closes connection on exit
    ...
db.close()                                      # manual close
```

Options by driver:

| Option | Driver | What it does |
| --- | --- | --- |
| `mvcc=True` | turso | Enables MVCC mode so `atomic(concurrent=True)` works across writer connections. |
| `echo=True` | all | Prints each SQL statement and its parameters. |
| `encryption_key="<64 hex chars>"` | libsql | Encrypts the database file. |
| `encryption=("aegis256", "<hex key>")` | turso | Encrypts the database file with Aegis256. |
| `sync_url=`, `auth_token=`, `sync_interval=` | libsql | Embedded replica mirroring a remote libSQL / Turso database. |
| `remote_url=`, `auth_token=` | turso | Local-first file database that pushes to and pulls from a remote Turso database. Accepts `libsql://` or `https://` URLs. |

A wrong or missing encryption key raises a `DatabaseError` on the first query. Any additional option is passed straight to the driver.

**Threads.** A reentrant lock (`threading.RLock`) protects every database call, making a single `Database` instance safe to share across multiple threads.

**Autocommit.** Outside `db.atomic()`, every statement commits immediately. Inside `db.atomic()`, statements commit together or roll back together.

## Models and Fields

A model is a subclass of `Model` with field attributes. Litho adds an auto-increment `id` primary key unless you declare your own field with `primary_key=True`. Table names default to the snake\_case of the class name (`BlogPost` becomes `blog_post`).

```python
class Article(Model):
    title = TextField(max_length=50)
    status = TextField(default="draft", choices=["draft", "live"])
    meta = JSONField(null=True)
    score = FloatField(default=0.0)
    created = DateTimeField(auto_now_add=True)
    updated = DateTimeField(auto_now=True)

    class Meta:
        table = "articles"                          # override table name
        indexes = [("status", "title")]             # composite index
        unique_together = [("title", "status")]     # composite unique constraint
```

### Field Types

| Field | Python Type | Stored As |
| --- | --- | --- |
| `IntegerField` | `int` | INTEGER (values > $2^{53}$ bound as text to preserve 64-bit precision in libSQL) |
| `FloatField` | `float` | REAL |
| `TextField(max_length=None)` | `str` | TEXT |
| `BooleanField` | `bool` | INTEGER (0 or 1) |
| `DateTimeField(auto_now, auto_now_add)` | timezone-aware `datetime` | TEXT (ISO-8601 format) |
| `DateField` | `date` | TEXT (ISO-8601 format) |
| `JSONField` | `dict`, `list`, `str`, `int`, `float`, `bool` | TEXT (serialized JSON) |
| `BlobField` | `bytes` | BLOB |
| `VectorField(dim)` | list/tuple of floats | BLOB (float32 vector; libSQL & Turso) |
| `ForeignKey(Model, ...)` | related Model instance | INTEGER column `<name>_id` |

### Field Options

| Option | Meaning |
| --- | --- |
| `null=True` | Column may be NULL. Without it, `None` raises `ValidationError`. |
| `default=` | Default value, or a callable invoked each time (`default=list`, `default=utcnow`). |
| `unique=True` | Creates a UNIQUE constraint on the column. |
| `index=True` | Creates an index on the column. |
| `primary_key=True` | Uses this field as primary key instead of generating `id`. |
| `choices=[...]` | Only listed values are accepted; others raise `ValidationError`. |

`auto_now_add=True` stamps UTC datetime on initial creation; `auto_now=True` updates on every save.

## Querying

Queries are lazy. `User.filter(...)` constructs a `Query` object without contacting the database. SQL runs when iterating, calling `.all()`, `.first()`, `.count()`, etc.

```python
adults = User.filter(age__gte=18)
adults.order_by("-age").all()          # list of User instances
for user in adults.order_by("name"):   # iterating executes query
    print(user.name)
```

### Lookups

Write lookups using double underscores: `field__operator=value`. Without an operator, it defaults to exact equality.

| Lookup | Meaning | Example |
| --- | --- | --- |
| (none) / `eq` | equals | `name="Ann"` |
| `ne` | not equals | `age__ne=30` |
| `gt`, `gte`, `lt`, `lte` | comparisons | `age__gte=18` |
| `in`, `not_in` | membership in list | `name__in=["Ann", "Bob"]` |
| `between` | inclusive range | `age__between=(10, 20)` |
| `like` | SQL LIKE pattern | `name__like="A%"` |
| `contains` | substring match (auto-escaped) | `name__contains="o"` |
| `startswith`, `endswith` | prefix or suffix (auto-escaped) | `name__startswith="A"` |
| `isnull` | NULL check | `age__isnull=True` |

`contains`, `startswith` and `endswith` automatically escape `%` and `_` so user input matches literally.

### Combining Conditions: `Q` Objects

Multiple keyword arguments in `.filter()` are ANDed. For OR and NOT logic, use `Q`:

```python
User.filter(Q(name="Ann") | Q(age__lt=18))         # OR
User.filter(~Q(name="Ann"))                        # NOT
User.filter(Q(age__gte=18) & ~Q(active=False))     # AND with NOT
User.exclude(name="Ann")                           # shorthand for filter(~Q(name="Ann"))
```

### Ordering, Pagination & Aggregation

```python
User.order_by("name")                              # ascending
User.order_by("-age", "name")                      # age descending, then name ascending
User.order_by("name").limit(10).offset(20)
User.order_by("name").paginate(1, per_page=20)     # page 1 with 20 items per page
Post.query().distinct().column("author_id")        # unique list of author IDs

# Aggregate functions
User.query().sum("age")                            # sum of column
User.query().avg("age")                            # average
User.query().min("age")                            # minimum
User.query().max("age")                            # maximum
User.query().count()                               # row count
User.filter(name="Ann").exists()                   # boolean True/False
```

### Retrieving Results

| Method | Returns |
| --- | --- |
| `.all()` or `iter(q)` | List of model instances |
| `.first()` | First model instance, or `None` |
| `.get(**lookups)` | Exactly one instance; raises `DoesNotExist` or `MultipleObjectsReturned` |
| `Model.get(pk)` | Look up directly by primary key value |
| `Model.get_or_none(...)` | Exactly one instance or `None` |
| `.values(*fields)` | List of dicts, e.g. `[{'name': 'Ann', 'age': 30}]` |
| `.column(name)` | Flat list of single column values: `['Ann', 'Bob']` |

## Writing Data

### Create, Save, Refresh & Delete

```python
ann = User.create(name="Ann", age=30)              # insert and return instance with id

ann.age = 31
ann.save()                                         # UPDATE existing row (or INSERT if no id)
ann.refresh()                                      # reload all fields from database
ann.to_dict()                                      # {'id': 1, 'name': 'Ann', 'age': 31}
ann.delete()                                       # delete this row

User(id=999, name="Special").save(force_insert=True) # force INSERT even with id
```

### Batch Operations

```python
User.bulk_create([{"name": "a"}, {"name": "b"}, User(name="c")], batch_size=500)
```

`bulk_create` inserts in configurable batches (`batch_size=500`) wrapped in a transaction.

### Get, Create or Upsert

```python
user, created = User.get_or_create(name="Dee", defaults={"age": 5})

# Upsert: specify unique conflict field, followed by values
User.upsert("name", name="Ann", age=31)
```

`upsert` uses SQLite `INSERT ... ON CONFLICT DO UPDATE` and preserves `auto_now_add` values.

### Query Updates and Increments

```python
User.filter(age__lt=18).update(is_active=False)     # returns number of rows updated
Post.filter(title="Hello").increment("views", by=1) # views = views + 1 (in SQL)
User.filter(name__startswith="test").delete()       # returns number of rows deleted
```

### Hooks

Define any of these lifecycle methods on your model:

```python
class Article(Model):
    title = TextField()

    def before_save(self):
        self.title = self.title.strip()

    def after_save(self):
        print("Saved row:", self.id)

    def before_delete(self):
        pass

    def after_delete(self):
        pass
```

## Relationships

A `ForeignKey` stores the parent ID in `<name>_id` and provides lazy access to the related instance.

```python
class Team(Model):
    name = TextField()

class Player(Model):
    team = ForeignKey(Team, null=True, on_delete="CASCADE", backref="players")
    name = TextField()

red = Team.create(name="Red")
p = Player.create(team=red, name="P1")

p.team_id                                           # 1 (raw foreign key integer)
p.team.name                                         # "Red" (loads related row lazily)
red.players.count()                                 # 1 (reverse relation via backref)
red.players.filter(name="P1").all()                 # filtered query on children

# Filter across relationships (using subqueries)
Player.filter(team__name="Red").all()
Player.filter(team__name__startswith="R").all()
```

Supported `on_delete` actions: `"CASCADE"`, `"SET NULL"`, `"RESTRICT"`, `"NO ACTION"`.

## Transactions, Raw SQL & Schema Alterations

### Transactions

`db.atomic()` is a context manager. Nested blocks automatically become SQLite `SAVEPOINT`s:

```python
with db.atomic():
    User.create(name="T1")
    try:
        with db.atomic():                          # nested savepoint
            User.create(name="T2")
            raise RuntimeError("undo T2 only")
    except RuntimeError:
        pass
# T1 is committed; T2 was rolled back
```

### Raw SQL

For advanced queries or unsupported dialect features:

```python
# Returns list of dicts
users = db.query("SELECT name FROM user WHERE age > ?", [20])

# Returns a Result object
result = db.execute("SELECT id, name FROM user")
result.scalar()                                    # first column of first row
result[0]                                          # row indexing
result.rows, result.columns                        # raw tuples and column headers
result.dicts()                                     # list of dicts
result.lastrowid, result.rowcount                  # mutation metadata
len(result)                                        # row count

db.execute_many("INSERT INTO user (name) VALUES (?)", [["u1"], ["u2"]])
```

### Schema Alterations

Litho provides lightweight schema migration helpers:

```python
db.add_missing_columns(UserV2)                     # adds newly defined fields to table
db.rename_column("article", "old_col", "new_col")
db.drop_column("article", "old_col")
db.rename_table("old_table", "new_table")
db.drop_tables(User, Post)                         # drops in reverse dependency order
```

## libSQL and Turso Features

### Vector Search

Store float vectors and query nearest neighbors by cosine or Euclidean distance:

```python
class Document(Model):
    title = TextField()
    emb = VectorField(dim=3)

db.create_tables(Document)
Document.create(title="Doc 1", emb=[1.0, 0.0, 0.0])
Document.create(title="Doc 2", emb=[0.0, 1.0, 0.0])

for doc in Document.query().nearest("emb", [1.0, 0.0, 0.0], k=2, metric="cos"):
    print(doc.title, doc.distance)

# Euclidean distance:
Document.query().nearest("emb", [0.0, 1.0, 0.0], k=1, metric="l2")
```

### Turso MVCC Concurrent Writers

```python
db = Database("app.db", driver="turso", mvcc=True)

with db.atomic(concurrent=True):
    Counter.create(value=1)
```

### Encryption at Rest

```python
key = "2d7a30108d3eb3e45c90a732041fe54778bdcf707c76749fab7da335d1b39c1d"

Database("secure.db", driver="libsql", encryption_key=key)
Database("secure.db", driver="turso", encryption=("aegis256", key))
```

### Server Sync & Embedded Replicas

```python
# 1. libSQL embedded replica (local file mirroring remote database)
db = Database("replica.db", driver="libsql",
              sync_url="libsql://your-db.turso.io", auth_token="...")
db.sync()

# 2. Turso local-first database (push and pull)
db = Database("local.db", driver="turso",
              remote_url="libsql://your-db.turso.io", auth_token="...")
db.pull()      # pull remote changes into local db (returns True if changes applied)
db.push()      # push local writes up to remote server
db.sync()      # pull, then push
```

## Errors

All Litho errors inherit from `LithoError`. Native driver errors from `sqlite3`, `libsql`, and `pyturso` are automatically caught and translated:

* `LithoError`: Base exception class.
* `DatabaseError`: Database statement failure (syntax, locked, etc.).
* `IntegrityError`: UNIQUE, NOT NULL, CHECK, or FOREIGN KEY violation.
* `ValidationError`: Value failed field validation before reaching database.
* `FieldError`: Unknown field name or invalid lookup operator.
* `UnsupportedError`: Feature not supported by chosen driver.
* `DoesNotExist` / `Model.DoesNotExist`: Row not found on `.get()`.
* `MultipleObjectsReturned`: Multiple rows matched `.get()`.
