"""Disposable, disk-backed management snapshots; never mutate a catalog on read."""

from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import stat
import tempfile

from .history import archive_key
from .qt_library import _ReadOnlyHistory
from .thumbcache import cloud_only


PAGE_SIZE = 100
_MISSING = """
WITH states AS (
 SELECT m.tweet_id,m.num,m.media_type,
        MAX(o.state='present') present,MAX(o.state='unavailable') uncertain
 FROM media m JOIN observed_rows o ON o.id=m.rowid
 GROUP BY m.tweet_id,m.num,m.media_type
), ranked AS (
 SELECT m.*,ROW_NUMBER() OVER(PARTITION BY tweet_id,num,media_type
 ORDER BY (record_source='saved') DESC,updated_at DESC,folder_mode) rank
 FROM media m
)
SELECT m.*,CASE WHEN COALESCE(c.ignored,0)=1 OR COALESCE(p.keep_deleted,0)=1 THEN 1 ELSE 0 END ignored,
       COALESCE(p.pending,0) deletion_pending,
       CASE WHEN s.uncertain OR COALESCE(p.pending,0)>0 THEN 'unavailable' ELSE 'missing' END availability
FROM ranked m JOIN states s USING(tweet_id,num,media_type)
LEFT JOIN missing_choices c USING(tweet_id,num,media_type)
LEFT JOIN (SELECT tweet_id,num,media_type,COUNT(*) pending,MAX(keep_deleted) keep_deleted
           FROM deletion_items WHERE status='pending' GROUP BY tweet_id,num,media_type) p
USING(tweet_id,num,media_type)
WHERE m.rank=1 AND s.present=0
"""
_PRESENT = """
WITH candidates AS (
 SELECT m.*,o.path_key,o.bytes,
 ROW_NUMBER() OVER(PARTITION BY o.path_key
   ORDER BY (m.record_source IN ('saved','existing')) DESC,m.updated_at DESC,
            m.folder_mode DESC,m.tweet_id DESC,m.num DESC,m.rowid ASC) primary_rank,
 ROW_NUMBER() OVER(PARTITION BY o.path_key
   ORDER BY (m.author!='') DESC,(m.record_source IN ('saved','existing')) DESC,m.updated_at DESC,
            m.folder_mode DESC,m.tweet_id DESC,m.num DESC,m.rowid ASC) author_rank,
 ROW_NUMBER() OVER(PARTITION BY o.path_key
   ORDER BY (m.posted_source='metadata' AND COALESCE(m.posted_at,'')!='') DESC,
            (COALESCE(m.posted_at,'')!='') DESC,(m.record_source IN ('saved','existing')) DESC,
            m.updated_at DESC,m.folder_mode DESC,m.tweet_id DESC,m.num DESC,m.rowid ASC) posted_rank,
 MIN(NULLIF(m.saved_at,'')) OVER(PARTITION BY o.path_key) first_saved
 FROM media m JOIN observed_rows o ON o.id=m.rowid WHERE o.state='present' AND m.path!=''
)
SELECT p.path_key,p.bytes,p.tweet_id,a.author,d.posted_at,p.first_saved saved_at
FROM candidates p JOIN candidates a ON a.path_key=p.path_key AND a.author_rank=1
JOIN candidates d ON d.path_key=p.path_key AND d.posted_rank=1
WHERE p.primary_rank=1
"""


class ManagementSnapshot:
    """One bounded-memory scan; subsequent pages perform SQL reads only."""

    def __init__(self, paths, cancelled=lambda: False):
        self.paths = paths
        self._temporary = tempfile.TemporaryDirectory(prefix="luvbox-management-", ignore_cleanup_errors=True)
        self._db = sqlite3.connect(Path(self._temporary.name) / "snapshot.sqlite3")
        self._db.row_factory = sqlite3.Row
        self.checked_at = ""
        self._statistics_cache = None
        try:
            with closing(_ReadOnlyHistory(paths)) as source:
                def progress(*_):
                    if cancelled():
                        raise InterruptedError("management refresh cancelled")
                source._db.backup(self._db, pages=128, progress=progress)
                scope = object.__new__(_ReadOnlyHistory)
                scope.__dict__.update(source.__dict__)
                scope._db = self._db
                scope._folder_cache = {}
            self._db.executescript("""
                CREATE TABLE observed_paths(path_key TEXT PRIMARY KEY,state TEXT,bytes INTEGER);
                CREATE TABLE observed_rows(id INTEGER PRIMARY KEY,path_key TEXT,state TEXT,bytes INTEGER);
                CREATE INDEX observed_key ON observed_rows(path_key);
            """)
            with self._db:
                for row in self._db.execute("SELECT rowid,path,last_path FROM media"):
                    if cancelled():
                        raise InterruptedError("management refresh cancelled")
                    value = row["path"] or row["last_path"]
                    key = os.path.normcase(os.path.abspath(value)) if value else ""
                    known = self._db.execute("SELECT state,bytes FROM observed_paths WHERE path_key=?", (key,)).fetchone()
                    if known is None:
                        state, size = self._availability(scope, value)
                        self._db.execute("INSERT INTO observed_paths VALUES(?,?,?)", (key, state, size))
                    else:
                        state, size = known
                    self._db.execute("INSERT INTO observed_rows VALUES(?,?,?,?)", (row["rowid"], key, state, size))
            self.checked_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _availability(scope, value):
        if not value:
            return "missing", 0
        try:
            path = Path(value)
            if not path.is_absolute() or (path.anchor and not Path(path.anchor).exists()):
                return "unavailable", 0
            candidate = scope._media_path(path)
            if candidate is None or cloud_only(candidate):
                return "unavailable", 0
            details = candidate.stat()
            if not stat.S_ISREG(details.st_mode):
                return "unavailable", 0
            return "present", details.st_size
        except FileNotFoundError:
            return "missing", 0
        except (OSError, ValueError, RuntimeError):
            return "unavailable", 0

    def close(self):
        if self._db is not None:
            self._db.close()
            self._db = None
        self._temporary.cleanup()

    def _page(self, query, page, parameters=()):
        total = self._db.execute("SELECT COUNT(*) FROM (" + query + ")", parameters).fetchone()[0]
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        page = min(max(0, int(page)), max(0, pages - 1))
        rows = [dict(row) for row in self._db.execute(query + " LIMIT ? OFFSET ?", (*parameters, PAGE_SIZE, page * PAGE_SIZE))]
        return dict(rows=rows, total=total, page=page, pages=pages, page_size=PAGE_SIZE)

    def missing_page(self, page=0, mode="all"):
        condition = {"all": "", "missing": " AND m.ignored=0 AND m.availability='missing'",
                     "ignored": " AND m.ignored=1", "unavailable": " AND m.availability='unavailable'"}
        if mode not in condition:
            raise ValueError("목록 종류를 확인해 주세요.")
        query = "SELECT * FROM (" + _MISSING + ") m WHERE 1=1" + condition[mode]
        result = self._page(query + " ORDER BY COALESCE(saved_at,posted_at,'') DESC,tweet_id DESC,num", page)
        for row in result["rows"]:
            row["archive_key"] = archive_key(row["tweet_id"], row["num"], row["media_type"])
            row["_identity"] = (row["tweet_id"], row["num"], row["media_type"])
        return result

    def deletion_page(self, page=0):
        return self._page("SELECT * FROM deletion_items ORDER BY created_at DESC,id DESC", page)

    def statistics(self, page=0):
        if self._statistics_cache is None:
            self._db.execute("CREATE TEMP TABLE current_files AS " + _PRESENT)
            self._db.execute("CREATE INDEX current_files_author ON current_files(author)")
            summary = dict(self._db.execute("SELECT COUNT(*) files,COUNT(DISTINCT tweet_id) posts,"
                                           "COALESCE(SUM(bytes),0) bytes FROM current_files").fetchone())
            summary["unavailable"] = self._db.execute("SELECT COUNT(*) FROM observed_paths WHERE state='unavailable'").fetchone()[0]
            months = []
            for kind, field in (("게시월", "posted_at"), ("저장월", "saved_at")):
                months.extend(dict(row, kind=kind) for row in self._db.execute(
                    "SELECT SUBSTR(" + field + ",1,7) month,COUNT(*) files FROM current_files WHERE "
                    + field + " IS NOT NULL AND SUBSTR(" + field + ",1,7) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
                    "GROUP BY month ORDER BY month DESC"))
            months.extend(dict(row, kind="확인된 좋아요 관측월") for row in self._db.execute(
                "SELECT SUBSTR(liked_before,1,7) month,COUNT(*) files FROM likes "
                "WHERE liked_after IS NOT NULL AND SUBSTR(liked_before,1,7) GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
                "GROUP BY month ORDER BY month DESC"))
            self._statistics_cache = (summary, months)
        authors = self._page("SELECT MAX(author) author,COUNT(*) files,COUNT(DISTINCT tweet_id) posts "
                             "FROM current_files WHERE author!='' GROUP BY LOWER(author) ORDER BY files DESC,author", page)
        summary, months = self._statistics_cache
        authors["statistics"] = dict(summary=summary, authors=authors["rows"], months=months)
        return authors

    def run_page(self, page=0, all_accounts=False):
        from .activity import outcome_page
        recent = outcome_page(0, 1000, account="" if all_accounts else self.paths.account)["rows"]
        self._db.execute("DROP TABLE IF EXISTS recent_activity")
        self._db.execute("CREATE TEMP TABLE recent_activity(run_id TEXT,origin TEXT,account TEXT,started_at TEXT,"
                         "finished_at TEXT,status TEXT,saved INTEGER,skipped INTEGER,message TEXT,log TEXT,history_id INTEGER)")
        for row in recent:
            output = row.get("output_dir", "")
            matches = (isinstance(output, str) and Path(output).is_absolute()
                       and row.get("account", "").casefold() == self.paths.account.casefold()
                       and os.path.normcase(os.path.normpath(output)) ==
                           os.path.normcase(os.path.normpath(str(self.paths.output_dir))))
            self._db.execute("INSERT INTO recent_activity VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                str(row.get("run_id", "")), row.get("origin", "scheduled"), row.get("account", ""),
                row.get("started_at", ""), row.get("finished_at", ""), row.get("status", "error"),
                row.get("saved", 0), row.get("skipped", 0), row.get("message", ""), row.get("log", ""),
                row.get("history_run_id") if matches else None))
        query = ("SELECT * FROM (SELECT run_id,origin,account,started_at,finished_at,status,saved,skipped,message,log "
                 "FROM recent_activity UNION ALL SELECT CAST(id AS TEXT),'legacy',?,started_at,finished_at,status,"
                 "saved,skipped,message,'' FROM runs WHERE NOT EXISTS(SELECT 1 FROM recent_activity a WHERE a.history_id=runs.id)) "
                 "ORDER BY COALESCE(NULLIF(started_at,''),finished_at,'') DESC,run_id DESC")
        return self._page(query, page, (self.paths.account,))
