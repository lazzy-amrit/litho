# Litho documentation

Nov 17, 2026 · @Amrit

Litho is a single-file Python ORM for sqlite, libSQL and Turso; this page shows how it works, from install to transactions, vectors and sync.

## Overview and install

Litho is one file, `main.py`, with no dependencies beyond the database drivers. You describe tables as Python classes, and Litho builds the SQL, binds every value as a parameter and gives you rows back as objects. The same code runs on three drivers:

| Driver | `driver=` | Python package | Notes |
| --- | --- | --- | --- |
| sqlite | `"sqlite"` (default) | stdlib `sqlite3` | Nothing to install. No vectors, no concurrent writers. |
| libSQL | `"libsql"` | `libsql` | Vectors, encryption, embedded replicas with sync. |
| Turso | `"turso"` | `pyturso` (imported as `turso`) | Rust engine: vectors, MVCC concurrent writers, encryption, remote sync. |

Set it up with uv:

```bash
uv init my-app && cd my-app
uv add libsql pyturso          # only the drivers you use; sqlite needs nothing
cp /path/to/litho/main.py litho.py
```

Then `from litho import Database, Model, TextField` in your own code and run it with `uv run app.py`. If you use a driver that is not installed, Litho tells you exactly which `uv add` to run.

Two rules hold everywhere. Table and column names always come from your model definitions, and every value is sent as a bound parameter, so user input can never change the SQL. Queries are lazy: nothing touches the database until you iterate, count, fetch or write.

## Quickstart

This whole example is tested on all three drivers; change only the `driver=` line.

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

db.create_tables(User, Post)                 # creates the tables and binds the models to db

ann = User.create(name="Ann", age=30)
Post.create(author=ann, title="Hello")

User.filter(age__gte=18).order_by("-age").all()      # [<User id=1>]
User.filter(Q(name="Ann") | Q(age__lt=5)).count()    # 1
ann.posts.count()                                    # 1  (reverse side of the foreign key)

with db.atomic():                            # one transaction; nested = savepoint
    User.create(name="Bob")
```

What happened: `create_tables` made `user` and `post` tables (names are the snake\_case of the class name), added an `id` primary key to each, and an index on `post.author_id`. `create` inserted a row and returned an object with its `id` filled in.

## Connecting

`Database(path=":memory:", driver="sqlite", *, mvcc=False, echo=False, **options)` opens one connection. Every model you pass to `db.create_tables(...)` is bound to that database.

```python
db = Database("app.db")                         # sqlite file
db = Database(":memory:")                       # throwaway, great for scripts
db = Database("app.db", driver="libsql")
db = Database("app.db", driver="turso")
db = Database("app.db", echo=True)              # print every SQL statement and its parameters

with Database("app.db") as db:                  # closes the connection on exit
    ...
db.close()                                      # or close it yourself
```

Options by driver:

| Option | Driver | What it does |
| --- | --- | --- |
| `mvcc=True` | turso | Turns on MVCC so `atomic(concurrent=True)` works. |
| `echo=True` | all | Prints each statement and its parameters. |
| `encryption_key="<64 hex chars>"` | libsql | Encrypts the database file. |
| `encryption=("aegis256", "<hex key>")` | turso | Encrypts the database file. |
| `sync_url=`, `auth_token=`, `sync_interval=` | libsql | Embedded replica of a remote libSQL database. |
| `remote_url=`, `auth_token=` | turso | Sync with a remote Turso database. |

A wrong or missing encryption key raises a `DatabaseError` on the first query, not at connect time. Any other option is passed straight to the driver.

**Threads.** A single lock wraps every database call, so one `Database` can be shared by several threads safely (eight threads inserting at once was tested on all three drivers). Calls take turns; they do not run in parallel.

**Autocommit.** Outside `db.atomic()` every statement commits immediately. Inside it, everything commits together or rolls back together (see Transactions).

## Models and fields

A model is a class with field attributes. Litho adds an auto-increment `id` primary key unless you declare your own `primary_key=True` field. The table name is the snake\_case of the class name (`BlogPost` becomes `blog_post`).

```python
class Article(Model):
    title = TextField(max_length=50)
    status = TextField(default="draft", choices=["draft", "live"])
    meta = JSONField(null=True)
    score = FloatField(default=0.0)
    created = DateTimeField(auto_now_add=True)
    updated = DateTimeField(auto_now=True)

    class Meta:
        table = "articles"                          # override the table name
        indexes = [("status", "title")]             # extra indexes
        unique_together = [("title", "status")]     # composite unique constraints
```

### Field types

| Field | Python value | Stored as |
| --- | --- | --- |
| `IntegerField` | `int` | INTEGER |
| `FloatField` | `float` | REAL |
| `TextField(max_length=None)` | `str` | TEXT |
| `BooleanField` | `bool` | INTEGER 0/1 |
| `DateTimeField(auto_now, auto_now_add)` | timezone-aware `datetime` (UTC) | TEXT, ISO format |
| `DateField` | `date` | TEXT, ISO format |
| `JSONField` | any JSON value (dict, list, ...) | TEXT |
| `BlobField` | `bytes` | BLOB |
| `VectorField(dim)` | list of floats | BLOB, 32-bit float vector (libsql and turso only) |
| `ForeignKey(Model, ...)` | a model instance | INTEGER column `<name>_id` |

### Options every field accepts

| Option | Meaning |
| --- | --- |
| `null=True` | Column may be NULL. Without it, `None` raises `ValidationError`. |
| `default=` | A value, or a function called each time (`default=list`). |
| `unique=True` | UNIQUE constraint. |
| `index=True` | Create an index on the column. |
| `primary_key=True` | Use this field as the primary key instead of `id`. |
| `choices=[...]` | Only these values are accepted. |

`auto_now_add=True` stamps a datetime on insert; `auto_now=True` re-stamps it on every save.

Values are checked before they reach the database. A bad value raises `ValidationError` (which is also a `ValueError`) with a readable message, for example `Article.status: 'weird' is not one of ['draft', 'live']` or `title: longer than 50 characters`.

## Querying

A query is a lazy object. `User.filter(...)` runs nothing; it returns a `Query` you can keep refining, and the SQL runs when you iterate it, call `.all()`, `.count()` and so on. Every method that refines returns a new query, so you can reuse a base query safely.

```python
adults = User.filter(age__gte=18)
adults.order_by("-age").all()          # list of User objects
for user in adults.order_by("name"):  # iterating also runs it
    print(user.name)
```

### Lookups

Write a lookup as `field__operator=value`. With no operator it means equals.

| Lookup | Meaning | Example |
| --- | --- | --- |
| (none) / `eq` | equals | `name="Ann"` |
| `ne` | not equal | `age__ne=30` |
| `gt`, `gte`, `lt`, `lte` | comparisons | `age__gte=18` |
| `in`, `not_in` | in a list | `name__in=["Ann", "Bob"]` |
| `between` | inclusive range of two values | `age__between=(10, 20)` |
| `like` | SQL LIKE pattern | `name__like="A%"` |
| `contains` | substring (case-insensitive for ASCII) | `name__contains="o"` |
| `startswith`, `endswith` | prefix or suffix | `name__startswith="A"` |
| `isnull` | NULL check | `age__isnull=True` |

`contains`, `startswith` and `endswith` escape `%` and `_` in your value, so they match exactly what you typed.

### Combining conditions

Several keyword arguments, or several `.filter()` calls, are ANDed. For OR and NOT use `Q`:

```python
User.filter(Q(name="Ann") | Q(age__lt=18))         # OR
User.filter(~Q(name="Ann"))                        # NOT
User.filter(Q(age__gte=18) & ~Q(active=False))     # AND with NOT
User.exclude(name="Ann")                           # same as filter(~Q(name="Ann"))
```

### Ordering, paging and distinct

```python
User.order_by("name")                  # ascending
User.order_by("-age", "name")          # age descending, then name
User.order_by("name").limit(10).offset(20)
User.order_by("name").paginate(1, per_page=2)    # page 1, two rows per page (per_page defaults to 20)
Post.query().distinct().column("author_id")      # [1, 2]
```

### Getting results

| Call | Returns |
| --- | --- |
| `.all()` or iterate | list of model objects |
| `.first()` | first object or `None` |
| `.get(**lookups)` | exactly one object. Raises `User.DoesNotExist` or `MultipleObjectsReturned`. |
| `Model.get(1)` | look up by primary key |
| `Model.get_or_none(...)` | one object or `None` |
| `.count()` | number of rows |
| `.exists()` | `True` or `False` |
| `.values("name", "age")` | list of dicts: `[{'name': 'Ann', 'age': 30}, ...]` |
| `.column("name")` | flat list: `['Ann', 'Bob']` |
| `.sum("age")`, `.avg`, `.min`, `.max` | one number (`None` on an empty set) |

```python
User.query().sum("age")            # 47
User.query().avg("age")            # 23.5
User.get(name="Ann").age           # 30
User.get_or_none(name="nobody")    # None
```

Output above is from a real run with Ann (30), Bob (17) and Cy (no age).

## Writing data

### Create, save, refresh, delete

```python
ann = User.create(name="Ann", age=30)   # insert and return the object (id is filled in)

ann.age = 31
ann.save()                               # UPDATE (INSERT if it has no id yet)
ann.refresh()                            # reload the row from the database
ann.to_dict()                            # {'id': 1, 'name': 'Ann', 'age': 31, ...}
ann.delete()                             # delete this row

User(id=999, name="X").save(force_insert=True)   # always INSERT, even with an id
```

Values are checked (`null`, `choices`, `max_length`, vector size) before anything is sent; a bad value raises `ValidationError`. A duplicate in a `unique` column raises `IntegrityError`.

### Many rows at once

```python
User.bulk_create([{"name": "a"}, {"name": "b"}, User(name="c")])   # returns 3
```

`bulk_create` accepts dicts or model objects, inserts in batches (`batch_size=500`) and is all-or-nothing: if any row fails, none are saved. It does not run hooks and does not fill in ids on the objects you passed.

### Get, create or update

```python
user, created = User.get_or_create(name="Dee", defaults={"age": 5})   # (obj, True) the first time
user, created = User.get_or_create(name="Dee")                        # (obj, False) after that

User.upsert("name", name="Ann", age=31)   # insert, or update the row whose unique `name` matches
```

`get_or_create` looks up by the keyword arguments and only uses `defaults` when it has to create. `upsert` takes the name of a unique field first, uses `INSERT ... ON CONFLICT DO UPDATE`, and returns the saved object. An `auto_now_add` field keeps its original value on update.

### Updating and deleting by query

```python
User.filter(age__lt=18).update(active=False)     # returns the number of rows changed
Post.filter(title="Hello").increment("views")    # views = views + 1, done in SQL
Post.filter(title="Hello").increment("views", by=5)
User.filter(name__startswith="m").delete()       # returns the number of rows deleted
```

`update`, `increment` and `delete` refuse a query that has `limit` or `offset`, because SQLite cannot safely apply those to a write. `increment` is safe under concurrency since the addition happens in the database. These bulk calls do not run hooks.

### Hooks

Define any of these methods on a model; Litho calls them around `save()` and `delete()` on an instance:

```python
class Article(Model):
    title = TextField()

    def before_save(self):  self.title = self.title.strip()
    def after_save(self):   print("saved", self.id)
    def before_delete(self): ...
    def after_delete(self):  ...
```

An exception in a `before_` hook stops the operation.

### Timestamps

`DateTimeField(auto_now_add=True)` is set when the row is created; `DateTimeField(auto_now=True)` is refreshed on every `save()`. Both are timezone-aware UTC datetimes.

## Relationships

A `ForeignKey` stores the other row's id in a column named `<field>_id` and gives you the related object through the field name.

```python
class Team(Model):
    name = TextField()

class Player(Model):
    team = ForeignKey(Team, null=True, on_delete="SET NULL", backref="players")
    name = TextField()

red = Team.create(name="Red")
p = Player.create(team=red, name="P1")

p.team_id          # 1       the raw column
p.team.name        # "Red"   loads the Team row the first time you ask
red.players.count()                     # reverse side, because of backref="players"
red.players.filter(name="P1").all()     # a normal query, already limited to this team
```

`ForeignKey(model, on_delete="CASCADE", backref=None, **field_options)`:

| `on_delete` | When the parent row is deleted |
| --- | --- |
| `"CASCADE"` (default) | Child rows are deleted too. |
| `"SET NULL"` | The child's column becomes NULL. Needs `null=True`. |
| `"RESTRICT"` | The delete fails with `IntegrityError` while children exist. |
| `"NO ACTION"` | Left to the database default. |

Foreign keys are enforced on all three drivers, and Litho indexes the `_id` column for you. `backref` is optional; without it, query the children from their own model: `Player.filter(team=red)`.

### Filtering across a relationship

Use a double underscore to reach into the related model. Any lookup works on the far side.

```python
Player.filter(team=red)                       # by object
Player.filter(team__in=[red, blue])           # by several objects
Player.filter(team__name="Red")               # by a field of the parent
Player.filter(team__name__startswith="R")     # with a lookup
```

These are written as subqueries, so they never duplicate rows the way a join can.

## Transactions, raw SQL and schema changes

### Transactions

`db.atomic()` is a context manager. Everything inside commits together when the block ends, and rolls back if an exception leaves the block.

```python
with db.atomic():
    User.create(name="T1")
    try:
        with db.atomic():              # nested block = a savepoint
            User.create(name="T2")
            raise RuntimeError("undo T2 only")
    except RuntimeError:
        pass
# T1 is saved, T2 was rolled back
```

`db.in_transaction` tells you whether you are inside a block. `create_tables` and `drop_tables` run in their own transaction, so a failure leaves no half-built schema. Turso with `mvcc=True` can also open the outer block as `db.atomic(concurrent=True)` (see the Turso section).

### Raw SQL

When the ORM is not enough, drop to SQL. Always use `?` placeholders for values.

```python
db.query("SELECT name FROM user WHERE age > ? ORDER BY name", [20])
# [{'name': 'Ann'}]                         a list of dicts

result = db.execute("SELECT COUNT(*) FROM user")
result.scalar()                              # 8         first column of the first row
result.rows, result.columns, result.dicts()  # raw rows, column names, list of dicts
result.lastrowid, result.rowcount            # after INSERT / UPDATE / DELETE

db.execute("UPDATE user SET age = ? WHERE name = ?", [40, "Ann"])
db.execute_many("INSERT INTO user (name) VALUES (?)", [["m1"], ["m2"]])   # returns the row count
```

`db.execute` returns a `Result`, which is also iterable and has `len()`. Features Litho has no helper for, such as full-text search, window functions, CTEs or `PRAGMA`, are all reachable this way.

### Inspecting the schema

```python
db.tables()                  # ['post', 'user']   (internal tables are hidden)
db.table_exists(User)        # True
db.columns("post")           # ['id', 'author_id', 'title']
db.indexes("post")           # ['post_author_id_idx']
```

### Changing a schema

Litho has no migration files. It gives you small, safe tools for the changes you actually make:

```python
db.add_missing_columns(V2)               # adds columns the model has but the table lacks
                                         # returns ['thing.color']
db.rename_column("old", "a", "alpha")
db.drop_column("old", "b")
db.rename_table("old", "older")
db.drop_tables(User, Post)               # same order as create_tables; dropped in reverse
```

`add_missing_columns` only adds. A new column must be `null=True` or have a plain `default` (number, text or True/False), and it cannot be unique or a primary key. To change a column's type or constraints, create a new table, copy the data with SQL, drop the old one and rename.

## libSQL and Turso features

These work on `driver="libsql"` and/or `driver="turso"`. On plain sqlite, Litho raises `UnsupportedError` with a clear message instead of failing inside SQL.

### Vector search (libsql and turso)

Declare a `VectorField(dim)`, store lists of floats, and ask for the nearest rows with `nearest(field, vector, k=10, metric="cos")`. Each returned row gets a `.distance`, and rows come back closest first.

```python
class Doc(Model):
    title = TextField()
    emb = VectorField(3, null=True)

db.create_tables(Doc)
Doc.create(title="x", emb=[1, 0, 0])
Doc.create(title="y", emb=[0, 1, 0])
Doc.create(title="z", emb=[0.9, 0.1, 0])

for row in Doc.query().nearest("emb", [1, 0, 0], k=2):
    print(row.title, round(row.distance, 3))
# x 0.0
# z 0.006

Doc.query().nearest("emb", [0, 1, 0], k=1, metric="l2")    # Euclidean distance instead of cosine
```

The vector must have exactly `dim` numbers or you get a `ValidationError`. `metric` is `"cos"` or `"l2"`. You can combine `nearest` with `filter()` to restrict candidates.

### Concurrent writers (turso)

Turso's MVCC mode lets several connections write at the same time. Turn it on when you connect, then open the outer transaction as concurrent:

```python
db = Database("app.db", driver="turso", mvcc=True)
with db.atomic(concurrent=True):
    C.create(n=1)
```

`concurrent=True` only works on the outermost `atomic()` and only on Turso; elsewhere it raises `UnsupportedError`.

### Encryption at rest

```python
key = "2d7a30108d3eb3e45c90a732041fe54778bdcf707c76749fab7da335d1b39c1d"   # 64 hex characters

Database("secure.db", driver="libsql", encryption_key=key)
Database("secure.db", driver="turso", encryption=("aegis256", key))
```

Both were tested by creating an encrypted file and reading it back. Keep the key safe: without it the file cannot be opened, and opening it with the wrong key raises `DatabaseError` on the first query.

### Syncing with a server

These need a real libSQL or Turso Cloud database and URL.

```python
# libSQL embedded replica: a local file that mirrors a remote database
db = Database("replica.db", driver="libsql",
              sync_url="libsql://your-db.turso.io", auth_token="...")
db.sync()

# Turso Database: local file that pushes and pulls
db = Database("local.db", driver="turso",
              remote_url="https://your-db.turso.io", auth_token="...")
db.pull()      # fetch remote changes (True if anything changed)
db.push()      # send local changes
db.sync()      # pull, then push
```

`sync`, `push` and `pull` cannot be called inside `atomic()`. Because they need a server, these calls are covered only by error-path checks here, not by a live test, so try them against a throwaway database first.

## Errors, gotchas and what is not included

### Errors

Every error Litho raises inherits from `LithoError`, and the three drivers' own exceptions are wrapped into the same classes, so one `except` works everywhere.

| Error | Raised when |
| --- | --- |
| `LithoError` | Base class. Also: driver not installed, misuse such as `sync()` inside `atomic()`. |
| `DatabaseError` | The database refused a statement (bad SQL, wrong encryption key, locked file). |
| `IntegrityError` | A UNIQUE, NOT NULL, foreign-key or RESTRICT rule was broken. Subclass of `DatabaseError`. |
| `ValidationError` | A value failed a field check before being sent. Also a `ValueError`. |
| `FieldError` | Unknown field name or lookup, or a mistake in a model definition. |
| `UnsupportedError` | The driver cannot do it (vectors on sqlite, `concurrent=True` off Turso, `sync()` without a remote). |
| `DoesNotExist` | `.get()` found no row. Use `User.DoesNotExist` to catch it for one model. |
| `MultipleObjectsReturned` | `.get()` found more than one row. |

```python
try:
    User.create(name="Ann")          # name is unique and Ann exists
except IntegrityError:
    ...
try:
    User.get(name="nobody")
except User.DoesNotExist:
    ...
```

### Gotchas

- **NULL follows SQL rules.** `age__ne=30` does not return rows where `age` is NULL, because NULL is not equal or unequal to anything. Add `Q(age__isnull=True)` with `|` if you want them. `exclude(name="Ann")` behaves the same way.
- **`field=None` means IS NULL.** `User.filter(age=None)` finds the rows with no age.
- **Bulk calls skip hooks.** `bulk_create`, `update`, `increment` and query `delete` do not call `before_save`, `after_save` or the delete hooks; only `save()` and instance `delete()` do. `bulk_create` also leaves `id` as `None` on the objects you passed.
- **Models belong to one database.** `create_tables` binds a model to that `Database`. Using a model before it is bound raises a clear error.
- **One connection, calls take turns.** Sharing it between threads is safe, but it is not a connection pool.
- **Big integers are exact.** Integers above 2^53 are bound as text so libSQL does not round them; you get the same number back on every driver.
- **Vectors are libsql and turso only.** Plan on a different column type if you also run on plain sqlite.
- **Remote sync is untested here.** It needs a live server, so only its error paths were checked.

### What is not included

Litho keeps to the features most apps need. These are left out on purpose; use `db.execute` / `db.query` for them:

- async API (everything is synchronous)
- many-to-many fields (use a model for the link table with two `ForeignKey`s)
- full-text search, JSON path filters, window functions and CTEs
- migration files and versioning (use `add_missing_columns` and the rename/drop helpers)
- Turso Cloud management API, branching, change data capture
