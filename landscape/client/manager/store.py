try:
    import sqlite3
except ImportError:
    from pysqlite2 import dbapi2 as sqlite3

# from landscape.lib.apt.package.store import with_cursor

"""Functions used by all sqlite-backed stores."""
from functools import wraps


def with_cursor(method):
    """Decorator that encloses the method in a database transaction.

    Even though SQLite is supposed to be useful in autocommit mode, we've
    found cases where the database continued to be locked for writing
    until the cursor was closed.  With this in mind, instead of using
    the autocommit mode, we explicitly terminate transactions and enforce
    cursor closing with this decorator.
    """

    @wraps(method)
    def inner(self, *args, **kwargs):
        if not self._db:
            # Create the database connection only when we start to actually
            # use it. This is essentially just a workaroud of a sqlite bug
            # happening when 2 concurrent processes try to create the tables
            # around the same time, the one which fails having an incorrect
            # cache and not seeing the tables
            self._db = sqlite3.connect(self._filename)
            self._ensure_schema()
        try:
            cursor = self._db.cursor()
            try:
                result = method(self, cursor, *args, **kwargs)
            finally:
                cursor.close()
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise
        return result

    return inner



class ManagerStore:
    def __init__(self, filename):
        self._db = sqlite3.connect(filename)
        ensure_schema(self._db)

    @with_cursor
    def get_graph(self, cursor, graph_id):
        cursor.execute(
            "SELECT graph_id, filename, user FROM graph WHERE graph_id=?",
            (graph_id,),
        )
        return cursor.fetchone()

    @with_cursor
    def get_graphs(self, cursor):
        cursor.execute("SELECT graph_id, filename, user FROM graph")
        return cursor.fetchall()

    @with_cursor
    def add_graph(self, cursor, graph_id, filename, user):
        cursor.execute(
            "SELECT graph_id FROM graph WHERE graph_id=?",
            (graph_id,),
        )
        if cursor.fetchone():
            cursor.execute(
                "UPDATE graph SET filename=?, user=? WHERE graph_id=?",
                (filename, user, graph_id),
            )
        else:
            cursor.execute(
                "INSERT INTO graph (graph_id, filename, user) "
                "VALUES (?, ?, ?)",
                (graph_id, filename, user),
            )

    @with_cursor
    def remove_graph(self, cursor, graph_id):
        cursor.execute("DELETE FROM graph WHERE graph_id=?", (graph_id,))

    @with_cursor
    def set_graph_accumulate(self, cursor, graph_id, timestamp, value):
        cursor.execute(
            "SELECT graph_id, graph_timestamp, graph_value FROM "
            "graph_accumulate WHERE graph_id=?",
            (graph_id,),
        )
        graph_accumulate = cursor.fetchone()
        if graph_accumulate:
            cursor.execute(
                "UPDATE graph_accumulate SET graph_timestamp = ?, "
                "graph_value = ? WHERE graph_id=?",
                (timestamp, value, graph_id),
            )
        else:
            cursor.execute(
                "INSERT INTO graph_accumulate (graph_id, graph_timestamp, "
                "graph_value) VALUES (?, ?, ?)",
                (graph_id, timestamp, value),
            )

    @with_cursor
    def get_graph_accumulate(self, cursor, graph_id):
        cursor.execute(
            "SELECT graph_id, graph_timestamp, graph_value FROM "
            "graph_accumulate WHERE graph_id=?",
            (graph_id,),
        )
        return cursor.fetchone()


def ensure_schema(db):
    cursor = db.cursor()
    try:
        cursor.execute(
            "CREATE TABLE graph"
            " (graph_id INTEGER PRIMARY KEY,"
            " filename TEXT NOT NULL, user TEXT)",
        )
        cursor.execute(
            "CREATE TABLE graph_accumulate"
            " (graph_id INTEGER PRIMARY KEY,"
            " graph_timestamp INTEGER, graph_value FLOAT)",
        )
    except sqlite3.OperationalError:
        cursor.close()
        db.rollback()
    else:
        cursor.close()
        db.commit()
