"""Durable state: what we've replied to, per-account cooldowns, daily counters.

SQLite so restarts never double-reply and daily caps survive a crash.
"""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state.db")
# Reads booked by measurement tasks carry this author prefix (their own lane).
MEASUREMENT_AUTHOR_PREFIX = "measure:"


class State:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS actions(
            id INTEGER PRIMARY KEY,
            kind TEXT,               -- 'reply' | 'post'
            tweet_id TEXT,           -- target tweet for replies
            author TEXT,             -- target handle for replies
            text TEXT,
            at REAL,
            day TEXT                 -- UTC yyyy-mm-dd for daily counting
        )""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_tweet ON actions(tweet_id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_author ON actions(author, at)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_day ON actions(day)")
        self.db.execute("""CREATE TABLE IF NOT EXISTS provider_deliveries(
            id INTEGER PRIMARY KEY,
            tweet_id TEXT,
            rule_id TEXT,
            author TEXT,
            classification TEXT,
            credits INTEGER NOT NULL,
            at REAL NOT NULL,
            day TEXT NOT NULL,
            month TEXT NOT NULL
        )""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_tweet ON provider_deliveries(tweet_id)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_day ON provider_deliveries(day)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_month ON provider_deliveries(month)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS provider_balance_observations(
            id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL,
            debit INTEGER NOT NULL,
            recharge INTEGER NOT NULL,
            at REAL NOT NULL,
            day TEXT NOT NULL,
            month TEXT NOT NULL
        )""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_balance_day "
            "ON provider_balance_observations(day)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_balance_month "
            "ON provider_balance_observations(month)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS provider_paid_starts(
            id INTEGER PRIMARY KEY,
            at REAL NOT NULL,
            day TEXT NOT NULL
        )""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_paid_start_day "
            "ON provider_paid_starts(day)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS x_stream_reads(
            id INTEGER PRIMARY KEY,
            post_id TEXT NOT NULL,
            author TEXT NOT NULL,
            cost_microusd INTEGER NOT NULL,
            at REAL NOT NULL,
            day TEXT NOT NULL,
            month TEXT NOT NULL,
            UNIQUE(post_id, day)
        )""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_x_stream_reads_day "
            "ON x_stream_reads(day)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_x_stream_reads_month "
            "ON x_stream_reads(month)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS llm_usage(
            id INTEGER PRIMARY KEY,
            model TEXT NOT NULL,
            lane TEXT NOT NULL DEFAULT 'legacy',
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cache_write_tokens INTEGER NOT NULL,
            cache_read_tokens INTEGER NOT NULL,
            cost_microusd INTEGER NOT NULL,
            at REAL NOT NULL,
            day TEXT NOT NULL,
            month TEXT NOT NULL
        )""")
        llm_usage_columns = {
            str(row[1]) for row in self.db.execute("PRAGMA table_info(llm_usage)")
        }
        if "lane" not in llm_usage_columns:
            try:
                self.db.execute(
                    "ALTER TABLE llm_usage "
                    "ADD COLUMN lane TEXT NOT NULL DEFAULT 'legacy'"
                )
            except sqlite3.OperationalError as exc:
                # The replies and posts daemons can initialize the same database
                # concurrently after a deployment. Both may observe the legacy
                # schema before one wins the ALTER TABLE race.
                if "duplicate column name: lane" not in str(exc).lower():
                    raise
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_llm_day ON llm_usage(day)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_llm_month ON llm_usage(month)")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_lane_day ON llm_usage(lane, day)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_lane_month ON llm_usage(lane, month)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS publisher_usage(
            id INTEGER PRIMARY KEY,
            endpoint TEXT NOT NULL,
            remote_post_id TEXT NOT NULL,
            cost_microusd INTEGER NOT NULL,
            at REAL NOT NULL,
            day TEXT NOT NULL,
            month TEXT NOT NULL
        )""")
        self.db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_publisher_remote_post "
            "ON publisher_usage(remote_post_id)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_publisher_day ON publisher_usage(day)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_publisher_month ON publisher_usage(month)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS publisher_intents(
            intent_key TEXT PRIMARY KEY,
            endpoint TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            remote_post_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS reply_drafts(
            id INTEGER PRIMARY KEY,
            tweet_id TEXT NOT NULL UNIQUE,
            author TEXT NOT NULL,
            source_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            source_created_at REAL NOT NULL,
            intent_url TEXT NOT NULL,
            status TEXT NOT NULL,
            telegram_chat_id TEXT,
            telegram_message_id INTEGER,
            error TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            day TEXT NOT NULL
        )""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_reply_drafts_day "
            "ON reply_drafts(day)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_reply_drafts_author "
            "ON reply_drafts(author, created_at)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_reply_drafts_status "
            "ON reply_drafts(status, expires_at)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS dynamic_author_audits(
            handle TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            median_views REAL NOT NULL,
            sample_posts INTEGER NOT NULL,
            topical_posts INTEGER NOT NULL,
            forbidden_posts INTEGER NOT NULL,
            followers INTEGER NOT NULL,
            bio TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            estimated_provider_credits INTEGER NOT NULL,
            checked_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dynamic_author_audits_status "
            "ON dynamic_author_audits(status, expires_at)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_dynamic_author_audits_checked "
            "ON dynamic_author_audits(checked_at)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS runtime_settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS generation_attempts(
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            bucket TEXT NOT NULL,
            at REAL NOT NULL
        )""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_attempt_bucket "
            "ON generation_attempts(kind, bucket)"
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS generation_outcomes(
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            bucket TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_id TEXT NOT NULL,
            at REAL NOT NULL,
            day TEXT NOT NULL
        )""")
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_generation_outcome_bucket "
            "ON generation_outcomes(kind, bucket, at)"
        )
        # Early numbers of a drafted source post (R3, Oct 19 2026): read again
        # for free on the day the stream delivered it. kind names the step.
        self.db.execute("""CREATE TABLE IF NOT EXISTS source_snapshots(
            id INTEGER PRIMARY KEY,
            post_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            age_seconds REAL NOT NULL,
            impressions INTEGER,
            likes INTEGER,
            replies INTEGER,
            reposts INTEGER,
            quotes INTEGER,
            bookmarks INTEGER,
            at REAL NOT NULL,
            UNIQUE(post_id, kind)
        )""")
        # Original posts v3 (Oct 20 2026): the real data a post is built from,
        # sent by the owner in Telegram (/artifact, /howto).
        # News posts (Oct 24 2026): every official Google Search item seen once,
        # so nothing is posted twice and nothing older than the first read is posted.
        self.db.execute("""CREATE TABLE IF NOT EXISTS news_items(
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            published_at REAL NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            seen_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS post_artifacts(
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            photo_file_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            source TEXT NOT NULL DEFAULT 'telegram'
        )""")
        # 'telegram' artifacts are drafted at once; 'feed' artifacts (the Pentra
        # window's daily push) wait for the next post slot.
        if "source" not in {row[1] for row in self.db.execute("PRAGMA table_info(post_artifacts)")}:
            self.db.execute("ALTER TABLE post_artifacts ADD COLUMN source TEXT NOT NULL DEFAULT 'telegram'")
        self.db.execute("""CREATE TABLE IF NOT EXISTS artifact_feed_seen(
            id TEXT PRIMARY KEY,
            at REAL NOT NULL
        )""")
        # Warm leads from Grok Bot (Oct 20 2026), one row per lead sent to
        # Telegram, so a lead is never drafted twice.
        self.db.execute("""CREATE TABLE IF NOT EXISTS leads(
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL
        )""")
        # Grok Bot's daily directory digests (Oct 21 2026). The ntfy cache keeps
        # 12 hours; these rows keep the history for the morning summary.
        self.db.execute("""CREATE TABLE IF NOT EXISTS grok_digests(
            id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            received_at REAL NOT NULL
        )""")
        self.db.commit()

    def add_grok_digest(self, message_id: str, payload: str, now: float = None) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO grok_digests(id,payload,received_at) VALUES(?,?,?)",
            (str(message_id), str(payload), now or time.time()),
        )
        self.db.commit()
        return cur.rowcount == 1

    def latest_grok_digest(self) -> dict | None:
        """The newest whole digest (a "part" 2 message only carries sources)."""
        for (payload,) in self.db.execute("SELECT payload FROM grok_digests ORDER BY received_at DESC"):
            try:
                digest = json.loads(payload)
            except ValueError:
                continue
            if isinstance(digest, dict) and int(digest.get("part") or 1) == 1:
                return digest
        return None

    @staticmethod
    def _day(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")

    def already_replied(self, tweet_id: str) -> bool:
        cur = self.db.execute(
            "SELECT 1 FROM actions WHERE tweet_id=? AND kind='reply' LIMIT 1", (tweet_id,))
        return cur.fetchone() is not None

    def last_action_at(self) -> float:
        cur = self.db.execute("SELECT MAX(at) FROM actions")
        row = cur.fetchone()
        return row[0] or 0.0

    def last_kind_at(self, kind: str) -> float:
        row = self.db.execute(
            "SELECT MAX(at) FROM actions WHERE kind=?",
            (kind,),
        ).fetchone()
        return row[0] or 0.0

    def last_reply_to_author_at(self, author: str) -> float:
        cur = self.db.execute(
            "SELECT MAX(at) FROM actions WHERE author=? AND kind='reply'", (author.lower(),))
        row = cur.fetchone()
        return row[0] or 0.0

    def count_today(self, kind: str = None, now: float = None) -> int:
        now = now or time.time()
        day = self._day(now)
        if kind:
            cur = self.db.execute(
                "SELECT COUNT(*) FROM actions WHERE day=? AND kind=?", (day, kind))
        else:
            cur = self.db.execute("SELECT COUNT(*) FROM actions WHERE day=?", (day,))
        return cur.fetchone()[0]

    def count_since(self, kind: str, since: float) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM actions WHERE kind=? AND at>=?",
            (kind, float(since)),
        ).fetchone()
        return int(row[0])

    def set_runtime_setting(
        self,
        key: str,
        value: str | int | float,
        now: float = None,
    ) -> None:
        now = now or time.time()
        self.db.execute(
            """INSERT INTO runtime_settings(key,value,updated_at)
               VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value,
                   updated_at=excluded.updated_at""",
            (str(key), str(value), now),
        )
        self.db.commit()

    def reserve_generation_attempt(
        self,
        *,
        kind: str,
        bucket: str,
        limit: int,
        now: float = None,
    ) -> bool:
        """Atomically reserve one bounded generation attempt for a time slot."""
        now = now or time.time()
        limit = max(0, int(limit))
        if limit == 0:
            return False
        try:
            self.db.execute("BEGIN IMMEDIATE")
            count = int(self.db.execute(
                "SELECT COUNT(*) FROM generation_attempts WHERE kind=? AND bucket=?",
                (str(kind), str(bucket)),
            ).fetchone()[0])
            if count >= limit:
                self.db.rollback()
                return False
            self.db.execute(
                "INSERT INTO generation_attempts(kind,bucket,at) VALUES(?,?,?)",
                (str(kind), str(bucket), now),
            )
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def generation_attempt_count(self, *, kind: str, bucket: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM generation_attempts WHERE kind=? AND bucket=?",
            (str(kind), str(bucket)),
        ).fetchone()
        return int(row[0])

    def record_generation_outcome(
        self,
        *,
        kind: str,
        bucket: str,
        outcome: str,
        reason: str = "",
        evidence_id: str = "",
        now: float = None,
    ) -> None:
        """Persist accepted, rejected, and failed generation outcomes."""
        now = now or time.time()
        self.db.execute(
            """INSERT INTO generation_outcomes(
                   kind,bucket,outcome,reason,evidence_id,at,day
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                str(kind),
                str(bucket),
                str(outcome),
                str(reason)[:1000],
                str(evidence_id),
                now,
                self._day(now),
            ),
        )
        self.db.commit()

    def recent_generation_outcomes(
        self,
        *,
        kind: str,
        limit: int = 20,
    ) -> list[dict]:
        rows = self.db.execute(
            """SELECT bucket,outcome,reason,evidence_id,at
               FROM generation_outcomes
               WHERE kind=?
               ORDER BY at DESC LIMIT ?""",
            (str(kind), max(1, int(limit))),
        ).fetchall()
        return [
            {
                "bucket": row[0],
                "outcome": row[1],
                "reason": row[2],
                "evidence_id": row[3],
                "at": row[4],
            }
            for row in rows
        ]

    def reply_source_outcome_count_today(self, now: float = None) -> int:
        """Count reply/thread source posts consumed by the reply daemon."""
        now = now or time.time()
        row = self.db.execute(
            """SELECT COUNT(*) FROM generation_outcomes
               WHERE kind='reply' AND day=? AND evidence_id LIKE '%|reply'""",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def runtime_setting(self, key: str, default: str = "") -> str:
        row = self.db.execute(
            "SELECT value FROM runtime_settings WHERE key=?",
            (str(key),),
        ).fetchone()
        return default if row is None else str(row[0])

    def dynamic_author_audit(
        self,
        handle: str,
        *,
        now: float = None,
    ) -> dict | None:
        """Return only a still-fresh reach/relevance verdict."""
        now = now or time.time()
        row = self.db.execute(
            """SELECT status,median_views,sample_posts,topical_posts,
                      forbidden_posts,followers,bio,reason,evidence_json,
                      estimated_provider_credits,checked_at,expires_at
               FROM dynamic_author_audits
               WHERE handle=? AND expires_at>?""",
            (handle.lstrip("@").lower(), now),
        ).fetchone()
        if row is None:
            return None
        try:
            evidence = json.loads(row[8])
        except (TypeError, ValueError):
            evidence = []
        return {
            "handle": handle.lstrip("@").lower(),
            "status": row[0],
            "median_views": float(row[1]),
            "sample_posts": int(row[2]),
            "topical_posts": int(row[3]),
            "forbidden_posts": int(row[4]),
            "followers": int(row[5]),
            "bio": str(row[6]),
            "reason": str(row[7]),
            "evidence": evidence,
            "estimated_provider_credits": int(row[9]),
            "checked_at": float(row[10]),
            "expires_at": float(row[11]),
        }

    def upsert_dynamic_author_audit(
        self,
        *,
        handle: str,
        status: str,
        median_views: float,
        sample_posts: int,
        topical_posts: int,
        forbidden_posts: int,
        followers: int,
        bio: str,
        reason: str,
        evidence: list[dict],
        estimated_provider_credits: int,
        checked_at: float,
        expires_at: float,
    ) -> None:
        self.db.execute(
            """INSERT INTO dynamic_author_audits(
                   handle,status,median_views,sample_posts,topical_posts,
                   forbidden_posts,followers,bio,reason,evidence_json,
                   estimated_provider_credits,checked_at,expires_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(handle) DO UPDATE SET
                   status=excluded.status,
                   median_views=excluded.median_views,
                   sample_posts=excluded.sample_posts,
                   topical_posts=excluded.topical_posts,
                   forbidden_posts=excluded.forbidden_posts,
                   followers=excluded.followers,
                   bio=excluded.bio,
                   reason=excluded.reason,
                   evidence_json=excluded.evidence_json,
                   estimated_provider_credits=excluded.estimated_provider_credits,
                   checked_at=excluded.checked_at,
                   expires_at=excluded.expires_at""",
            (
                handle.lstrip("@").lower(),
                str(status),
                float(median_views),
                int(sample_posts),
                int(topical_posts),
                int(forbidden_posts),
                int(followers),
                str(bio)[:2000],
                str(reason)[:1000],
                json.dumps(evidence, separators=(",", ":")),
                int(estimated_provider_credits),
                float(checked_at),
                float(expires_at),
            ),
        )
        self.db.commit()

    def dynamic_author_audit_count_today(self, now: float = None) -> int:
        now = now or time.time()
        start = datetime.fromtimestamp(now, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        row = self.db.execute(
            "SELECT COUNT(*) FROM dynamic_author_audits WHERE checked_at>=?",
            (start,),
        ).fetchone()
        return int(row[0])

    def dynamic_qualified_author_count(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            """SELECT COUNT(*) FROM dynamic_author_audits
               WHERE status='qualified' AND expires_at>?""",
            (now,),
        ).fetchone()
        return int(row[0])

    def dynamic_qualified_author_handles(self, now: float = None) -> list[str]:
        now = now or time.time()
        rows = self.db.execute(
            """SELECT handle FROM dynamic_author_audits
               WHERE status='qualified' AND expires_at>?
               ORDER BY handle""",
            (now,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def set_operator_active_until(
        self,
        active_until: float,
        now: float = None,
    ) -> None:
        self.set_runtime_setting(
            "reply_operator_active_until",
            float(active_until),
            now=now,
        )

    def set_operator_continuous(self, enabled: bool, now: float = None) -> None:
        """Persist the desired reply-monitoring mode across restarts.

        The paid providers remain protected by their independent daily and
        monthly circuit breakers.  This flag only removes the fragile timed
        operator window; it does not authorize public posting.
        """
        self.set_runtime_setting(
            "reply_operator_continuous",
            "1" if enabled else "0",
            now=now,
        )

    def operator_is_continuous(self) -> bool:
        return self.runtime_setting("reply_operator_continuous", "0") == "1"

    def operator_active_until(self) -> float:
        try:
            return float(
                self.runtime_setting("reply_operator_active_until", "0")
            )
        except (TypeError, ValueError):
            return 0.0

    # Set by the reply runner from human_approval.shift_hours_utc: continuous
    # (/always) monitoring only counts as active inside the daily window. A
    # timed /on shift ignores it.
    shift_window = None

    def operator_is_active(self, now: float = None) -> bool:
        now = now or time.time()
        if self.operator_active_until() > now:
            return True
        return self.operator_is_continuous() and (
            self.shift_window is None or bool(self.shift_window(now))
        )

    def telegram_update_offset(self) -> int:
        try:
            return int(self.runtime_setting("telegram_update_offset", "0"))
        except (TypeError, ValueError):
            return 0

    def set_telegram_update_offset(self, offset: int) -> None:
        self.set_runtime_setting("telegram_update_offset", int(offset))

    def recent_texts(self, kind: str | tuple[str, ...], limit: int = 12) -> list[str]:
        """Return recent published copy oldest-first for repetition control."""
        kinds = (kind,) if isinstance(kind, str) else tuple(kind)
        marks = ",".join("?" for _ in kinds)
        cur = self.db.execute(
            f"SELECT text FROM actions WHERE kind IN ({marks}) ORDER BY at DESC LIMIT ?",
            (*kinds, max(1, int(limit))),
        )
        return [row[0] for row in reversed(cur.fetchall()) if row[0]]

    def recent_reply_drafts(self, limit: int = 12) -> list[str]:
        """Return recent operator-facing reply drafts oldest-first."""
        cur = self.db.execute(
            """SELECT reply_text FROM reply_drafts
               WHERE status IN ('reserved','notified')
               ORDER BY created_at DESC LIMIT ?""",
            (max(1, int(limit)),),
        )
        return [row[0] for row in reversed(cur.fetchall()) if row[0]]

    def record(self, kind: str, text: str, tweet_id: str = None, author: str = None,
               now: float = None):
        now = now or time.time()
        self.db.execute(
            "INSERT INTO actions(kind,tweet_id,author,text,at,day) VALUES(?,?,?,?,?,?)",
            (kind, tweet_id, (author or "").lower(), text, now, self._day(now)))
        self.db.commit()

    def reserve_reply_draft(
        self,
        *,
        tweet_id: str,
        author: str,
        source_text: str,
        reply_text: str,
        source_created_at: float,
        intent_url: str,
        expires_at: float,
        now: float = None,
    ) -> bool:
        """Reserve one notification before Telegram network I/O.

        The unique tweet ID makes reconnect replay and worker retries incapable
        of generating duplicate approval requests.
        """
        now = now or time.time()
        try:
            self.db.execute(
                """INSERT INTO reply_drafts(
                       tweet_id,author,source_text,reply_text,source_created_at,
                       intent_url,status,created_at,expires_at,day
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(tweet_id),
                    author.lstrip("@").lower(),
                    source_text,
                    reply_text,
                    float(source_created_at),
                    intent_url,
                    "reserved",
                    now,
                    float(expires_at),
                    self._day(now),
                ),
            )
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            self.db.rollback()
            return False

    def mark_reply_draft_observed(self, *, tweet_id: str) -> None:
        self.db.execute(
            "UPDATE reply_drafts SET status='observed', error=NULL WHERE tweet_id=?",
            (str(tweet_id),),
        )
        self.db.commit()

    def reply_latency_stats_today(self, now: float = None) -> dict:
        """Seconds from the source post to the draft being ready, today."""
        now = now or time.time()
        rows = [
            float(r[0]) for r in self.db.execute(
                "SELECT created_at-source_created_at FROM reply_drafts WHERE day=?",
                (self._day(now),),
            )
            if r[0] is not None
        ]
        if not rows:
            return {"count": 0, "median": None, "max": None}
        rows.sort()
        return {
            "count": len(rows),
            "median": round(rows[len(rows) // 2], 1),
            "max": round(rows[-1], 1),
        }

    def reply_outcome_counts_today(self, now: float = None) -> dict:
        now = now or time.time()
        return {
            str(outcome): int(count)
            for outcome, count in self.db.execute(
                """SELECT outcome, COUNT(*) FROM generation_outcomes
                   WHERE kind='reply' AND day=? GROUP BY outcome
                   ORDER BY COUNT(*) DESC""",
                (self._day(now),),
            )
        }

    def complete_reply_draft_notification(
        self,
        *,
        tweet_id: str,
        telegram_chat_id: str,
        telegram_message_id: int,
    ) -> None:
        self.db.execute(
            """UPDATE reply_drafts
               SET status='notified', telegram_chat_id=?, telegram_message_id=?,
                   error=NULL
               WHERE tweet_id=? AND status='reserved'""",
            (str(telegram_chat_id), int(telegram_message_id), str(tweet_id)),
        )
        self.db.commit()

    def fail_reply_draft_notification(self, *, tweet_id: str, error: str) -> None:
        self.db.execute(
            """UPDATE reply_drafts
               SET status='failed', error=?
               WHERE tweet_id=? AND status='reserved'""",
            (str(error)[:500], str(tweet_id)),
        )
        self.db.commit()

    def reply_draft_exists(self, tweet_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM reply_drafts WHERE tweet_id=? LIMIT 1",
            (str(tweet_id),),
        ).fetchone()
        return row is not None

    def reply_draft_status(self, tweet_id: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM reply_drafts WHERE tweet_id=? LIMIT 1",
            (str(tweet_id),),
        ).fetchone()
        return None if row is None else str(row[0])

    def reply_draft_count_today(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            """SELECT COUNT(*) FROM reply_drafts
               WHERE day=? AND status IN ('reserved','notified')""",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def active_reply_draft_count(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            """SELECT COUNT(*) FROM reply_drafts
               WHERE status IN ('reserved','notified') AND expires_at>?""",
            (now,),
        ).fetchone()
        return int(row[0])

    def drafts_due_rise_check(
        self,
        *,
        kind: str,
        min_age: float,
        max_age: float,
        now: float = None,
    ) -> list[dict]:
        """Sent drafts whose source post is min_age..max_age seconds old, has
        no `kind` snapshot yet, and was read by the stream today: X does not
        bill a post again on the same UTC day, so the re-read is free."""
        now = now or time.time()
        rows = self.db.execute(
            f"""SELECT d.tweet_id, d.author, d.source_text, d.reply_text, d.source_created_at,
                       d.created_at, d.expires_at, d.telegram_message_id
                FROM reply_drafts d
                WHERE d.status='notified' AND d.telegram_message_id IS NOT NULL
                  AND d.source_created_at <= ? AND d.source_created_at >= ?
                  AND EXISTS (SELECT 1 FROM x_stream_reads r WHERE r.post_id=d.tweet_id AND r.day=?
                              AND r.author NOT LIKE '{MEASUREMENT_AUTHOR_PREFIX}%')
                  AND NOT EXISTS (SELECT 1 FROM source_snapshots s WHERE s.post_id=d.tweet_id AND s.kind=?)
                ORDER BY d.source_created_at""",
            (now - float(min_age), now - float(max_age), self._day(now), str(kind)),
        ).fetchall()
        keys = ("tweet_id", "author", "source_text", "reply_text", "source_created_at",
                "created_at", "expires_at", "telegram_message_id")
        return [dict(zip(keys, row)) for row in rows]

    def record_source_snapshot(
        self,
        *,
        post_id: str,
        kind: str,
        age_seconds: float,
        metrics: dict,
        now: float = None,
    ) -> None:
        now = now or time.time()
        self.db.execute(
            """INSERT OR IGNORE INTO source_snapshots(
                   post_id,kind,age_seconds,impressions,likes,replies,reposts,quotes,bookmarks,at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (str(post_id), str(kind), float(age_seconds),
             metrics.get("impression_count"), metrics.get("like_count"), metrics.get("reply_count"),
             metrics.get("retweet_count"), metrics.get("quote_count"), metrics.get("bookmark_count"), now),
        )
        self.db.commit()

    def add_post_artifact(self, *, kind: str, text: str, photo_file_id: str = "", source: str = "telegram",
                          now: float = None) -> int:
        now = now or time.time()
        cursor = self.db.execute(
            """INSERT INTO post_artifacts(kind,text,photo_file_id,status,created_at,updated_at,source)
               VALUES(?,?,?,'new',?,?,?)""",
            (str(kind), str(text), str(photo_file_id or ""), now, now, str(source)),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def next_new_post_artifact(self, source: str | None = None) -> dict | None:
        row = self.db.execute(
            """SELECT id,kind,text,photo_file_id,created_at FROM post_artifacts
               WHERE status='new' AND (? IS NULL OR source=?) ORDER BY id LIMIT 1""",
            (source, source),
        ).fetchone()
        if row is None:
            return None
        return dict(zip(("id", "kind", "text", "photo_file_id", "created_at"), row))

    def finish_post_artifact(self, artifact_id: int, *, status: str, note: str = "", now: float = None) -> None:
        now = now or time.time()
        self.db.execute(
            "UPDATE post_artifacts SET status=?, note=?, updated_at=? WHERE id=?",
            (str(status), str(note)[:500], now, int(artifact_id)),
        )
        self.db.commit()

    def feed_artifact_seen(self, feed_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM artifact_feed_seen WHERE id=?", (str(feed_id),)).fetchone() is not None

    def mark_feed_artifact_seen(self, feed_id: str, now: float = None) -> None:
        self.db.execute("INSERT OR IGNORE INTO artifact_feed_seen(id,at) VALUES(?,?)", (str(feed_id), now or time.time()))
        self.db.commit()

    def last_post_artifact_drafted_at(self) -> float:
        row = self.db.execute(
            "SELECT MAX(updated_at) FROM post_artifacts WHERE status IN ('drafted', 'published')"
        ).fetchone()
        return float(row[0] or 0.0)

    def plan_posts_done(self) -> int:
        """Posts v4: plan entries tried (published, drafted or failed)."""
        row = self.db.execute(
            "SELECT COUNT(*) FROM post_artifacts WHERE kind='plan' AND status!='new'"
        ).fetchone()
        return int(row[0])

    # Posts v4 (Oct 23 2026): a held entry used to be passed over until the plan
    # wrapped, so with half the attempts held the 32-entry plan repeated after
    # about 8 days of posts.
    PLAN_REPEAT_SECONDS = 14 * 86_400
    PLAN_RETRY_SECONDS = 20 * 3_600

    def next_plan_index(self, plan_len: int, now: float | None = None) -> int:
        """The plan entry after the last one tried, skipping entries published
        in the last 14 days and entries held in the last 20 hours: a held entry
        gets another try on a later day, and nothing published repeats within
        14 days. If every entry is excluded, the oldest publish comes back."""
        now = time.time() if now is None else now
        last, published, held = -1, {}, set()
        for text, status, updated_at in self.db.execute(
            "SELECT text, status, updated_at FROM post_artifacts WHERE kind='plan' AND status!='new' ORDER BY id"
        ):
            try:
                index = int(json.loads(text or "{}").get("index", -1)) % plan_len
            except (ValueError, TypeError, AttributeError):
                continue
            last = index
            if status in ("published", "drafted") and updated_at >= now - self.PLAN_REPEAT_SECONDS:
                published[index] = max(updated_at, published.get(index, 0.0))
            elif status == "failed" and updated_at >= now - self.PLAN_RETRY_SECONDS:
                held.add(index)
        for step in range(1, plan_len + 1):
            index = (last + step) % plan_len
            if index not in published and index not in held:
                return index
        return min(published, key=published.get) if published else (last + 1) % plan_len

    def add_news_items(self, items: list[dict], *, seeding: bool, now: float | None = None) -> int:
        """Store items not seen before: 'seeded' on the first read (never
        posted), 'new' afterwards, or 'skipped' for an event-type title."""
        now = time.time() if now is None else now
        added = 0
        for item in items:
            status = "seeded" if seeding else ("skipped" if item.get("skip") else "new")
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO news_items(id,title,url,published_at,payload,status,seen_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (str(item["id"]), str(item["title"]), str(item["url"]), float(item["published"]),
                 json.dumps(item), status, now, now),
            )
            added += cursor.rowcount
        self.db.commit()
        return added

    def news_seen_any(self) -> bool:
        return self.db.execute("SELECT 1 FROM news_items LIMIT 1").fetchone() is not None

    def next_news_item(self, *, fresh_seconds: float, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT payload FROM news_items WHERE status='new' AND published_at>=? ORDER BY published_at LIMIT 1",
            (now - fresh_seconds,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def finish_news_item(self, item_id: str, *, status: str, note: str = "", now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.db.execute("UPDATE news_items SET status=?, note=?, updated_at=? WHERE id=?",
                        (str(status), str(note)[:500], now, str(item_id)))
        self.db.commit()

    def news_published_since(self, since: float) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM news_items WHERE status='published' AND updated_at>=?", (since,)
        ).fetchone()[0])

    def news_attempts_since(self, since: float) -> int:
        """News items tried (published or failed) since a time: one a day."""
        return int(self.db.execute(
            "SELECT COUNT(*) FROM news_items WHERE status IN ('published','failed') AND updated_at>=?", (since,)
        ).fetchone()[0])

    def howto_artifacts_done(self) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) FROM post_artifacts WHERE kind='howto' AND status!='new'"
        ).fetchone()
        return int(row[0])

    def lead_exists(self, lead_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM leads WHERE id=?", (str(lead_id),)).fetchone() is not None

    def recent_lead_replies(self, limit: int = 10) -> list[str]:
        """The reply drafts of the last sent leads, oldest first."""
        replies = []
        for (payload,) in self.db.execute("SELECT payload FROM leads ORDER BY created_at DESC LIMIT ?", (int(limit),)):
            try:
                reply = str(json.loads(payload).get("draft_reply") or "")
            except ValueError:
                continue
            if reply:
                replies.append(reply)
        return list(reversed(replies))

    def add_lead(self, lead_id: str, payload: str, *, status: str, note: str = "", now: float = None) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO leads(id,payload,status,note,created_at) VALUES(?,?,?,?,?)",
            (str(lead_id), str(payload), str(status), str(note)[:500], now or time.time()),
        )
        self.db.commit()

    def last_reply_draft_at(self) -> float:
        row = self.db.execute(
            """SELECT MAX(created_at) FROM reply_drafts
               WHERE status IN ('reserved','notified')"""
        ).fetchone()
        return row[0] or 0.0

    def last_reply_draft_to_author_at(self, author: str) -> float:
        row = self.db.execute(
            """SELECT MAX(created_at) FROM reply_drafts
               WHERE author=? AND status IN ('reserved','notified')""",
            (author.lstrip("@").lower(),),
        ).fetchone()
        return row[0] or 0.0

    def record_provider_delivery(
        self,
        *,
        tweet_id: str,
        rule_id: str,
        author: str,
        classification: str,
        credits: int,
        now: float = None,
    ):
        now = now or time.time()
        day = self._day(now)
        self.db.execute(
            """INSERT INTO provider_deliveries(
                   tweet_id,rule_id,author,classification,credits,at,day,month
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                tweet_id,
                rule_id,
                author.lower(),
                classification,
                int(credits),
                now,
                day,
                day[:7],
            ),
        )
        self.db.commit()

    def provider_delivery_count(self, tweet_id: str) -> int:
        if not tweet_id:
            return 0
        row = self.db.execute(
            "SELECT COUNT(*) FROM provider_deliveries WHERE tweet_id=?", (tweet_id,)
        ).fetchone()
        return int(row[0])

    def provider_credits_today(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COALESCE(SUM(credits),0) FROM provider_deliveries WHERE day=?",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def provider_credits_month(self, now: float = None) -> int:
        now = now or time.time()
        month = self._day(now)[:7]
        row = self.db.execute(
            "SELECT COALESCE(SUM(credits),0) FROM provider_deliveries WHERE month=?",
            (month,),
        ).fetchone()
        return int(row[0])

    def record_provider_balance(self, balance: int, now: float = None) -> dict:
        """Record authoritative provider debits without losing spend on a top-up."""
        now = now or time.time()
        current = int(balance)
        previous = self.db.execute(
            "SELECT balance FROM provider_balance_observations ORDER BY id DESC LIMIT 1"
        ).fetchone()
        last = current if previous is None else int(previous[0])
        debit = max(0, last - current)
        recharge = max(0, current - last)
        day = self._day(now)
        self.db.execute(
            """INSERT INTO provider_balance_observations(
                   balance,debit,recharge,at,day,month
               ) VALUES(?,?,?,?,?,?)""",
            (current, debit, recharge, now, day, day[:7]),
        )
        self.db.commit()
        return {
            "balance": current,
            "debit": debit,
            "recharge": recharge,
            "debits_today": self.provider_balance_debits_today(now),
            "debits_month": self.provider_balance_debits_month(now),
        }

    def provider_balance_debits_today(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COALESCE(SUM(debit),0) FROM provider_balance_observations WHERE day=?",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def provider_balance_debits_month(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COALESCE(SUM(debit),0) FROM provider_balance_observations WHERE month=?",
            (self._day(now)[:7],),
        ).fetchone()
        return int(row[0])

    def provider_paid_starts_today(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COUNT(*) FROM provider_paid_starts WHERE day=?",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def reserve_provider_paid_start(self, max_starts: int, now: float = None) -> bool:
        """Atomically reserve a paid rule lifecycle before any provider mutation."""
        now = now or time.time()
        day = self._day(now)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            count = int(
                self.db.execute(
                    "SELECT COUNT(*) FROM provider_paid_starts WHERE day=?", (day,)
                ).fetchone()[0]
            )
            if count >= int(max_starts):
                self.db.rollback()
                return False
            self.db.execute(
                "INSERT INTO provider_paid_starts(at,day) VALUES(?,?)", (now, day)
            )
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def record_llm_usage(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_write_tokens: int,
        cache_read_tokens: int,
        cost_microusd: int,
        lane: str = "legacy",
        now: float = None,
    ):
        now = now or time.time()
        day = self._day(now)
        self.db.execute(
            """INSERT INTO llm_usage(
                   model,lane,input_tokens,output_tokens,cache_write_tokens,
                   cache_read_tokens,cost_microusd,at,day,month
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                model,
                str(lane),
                int(input_tokens),
                int(output_tokens),
                int(cache_write_tokens),
                int(cache_read_tokens),
                int(cost_microusd),
                now,
                day,
                day[:7],
            ),
        )
        self.db.commit()

    def record_x_stream_read(
        self,
        *,
        post_id: str,
        author: str,
        cost_microusd: int,
        now: float = None,
    ) -> bool:
        """Book one official X post read, deduplicated within the UTC day."""
        now = now or time.time()
        day = self._day(now)
        try:
            cursor = self.db.execute(
                """INSERT OR IGNORE INTO x_stream_reads(
                       post_id,author,cost_microusd,at,day,month
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    str(post_id),
                    author.lstrip("@").lower(),
                    int(cost_microusd),
                    now,
                    day,
                    day[:7],
                ),
            )
            self.db.commit()
            return cursor.rowcount == 1
        except Exception:
            self.db.rollback()
            raise

    def x_stream_cost_microusd_today(self, now: float = None, lane: str | None = None) -> int:
        """Today's read spend: every read, or one lane ("stream", or
        "measurement" for reads booked as measure:<handle>)."""
        now = now or time.time()
        clause = {
            None: "",
            "stream": f" AND author NOT LIKE '{MEASUREMENT_AUTHOR_PREFIX}%'",
            "measurement": f" AND author LIKE '{MEASUREMENT_AUTHOR_PREFIX}%'",
        }[lane]
        row = self.db.execute(
            f"SELECT COALESCE(SUM(cost_microusd),0) FROM x_stream_reads WHERE day=?{clause}",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def x_stream_read_count_today(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COUNT(*) FROM x_stream_reads WHERE day=?",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def x_stream_cost_microusd_month(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_microusd),0) FROM x_stream_reads WHERE month=?",
            (self._day(now)[:7],),
        ).fetchone()
        return int(row[0])

    def llm_cost_microusd_today(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_microusd),0) FROM llm_usage WHERE day=?",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def llm_cost_microusd_today_for_model(
        self, model: str, now: float = None
    ) -> int:
        now = now or time.time()
        row = self.db.execute(
            """SELECT COALESCE(SUM(cost_microusd),0)
               FROM llm_usage WHERE day=? AND model=?""",
            (self._day(now), str(model)),
        ).fetchone()
        return int(row[0])

    def llm_cost_microusd_month(self, now: float = None) -> int:
        now = now or time.time()
        month = self._day(now)[:7]
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_microusd),0) FROM llm_usage WHERE month=?",
            (month,),
        ).fetchone()
        return int(row[0])

    def llm_cost_microusd_month_for_model(
        self, model: str, now: float = None
    ) -> int:
        now = now or time.time()
        month = self._day(now)[:7]
        row = self.db.execute(
            """SELECT COALESCE(SUM(cost_microusd),0)
               FROM llm_usage WHERE month=? AND model=?""",
            (month, str(model)),
        ).fetchone()
        return int(row[0])

    def llm_cost_microusd_today_for_lane(
        self, lane: str, now: float = None
    ) -> int:
        now = now or time.time()
        row = self.db.execute(
            """SELECT COALESCE(SUM(cost_microusd),0)
               FROM llm_usage WHERE day=? AND lane=?""",
            (self._day(now), str(lane)),
        ).fetchone()
        return int(row[0])

    def llm_cost_microusd_month_for_lane(
        self, lane: str, now: float = None
    ) -> int:
        now = now or time.time()
        month = self._day(now)[:7]
        row = self.db.execute(
            """SELECT COALESCE(SUM(cost_microusd),0)
               FROM llm_usage WHERE month=? AND lane=?""",
            (month, str(lane)),
        ).fetchone()
        return int(row[0])

    def record_publisher_usage(
        self,
        *,
        endpoint: str,
        remote_post_id: str,
        cost_microusd: int,
        now: float = None,
    ):
        now = now or time.time()
        day = self._day(now)
        self.db.execute(
            """INSERT INTO publisher_usage(
                   endpoint,remote_post_id,cost_microusd,at,day,month
               ) VALUES(?,?,?,?,?,?)""",
            (
                endpoint,
                remote_post_id,
                int(cost_microusd),
                now,
                day,
                day[:7],
            ),
        )
        self.db.commit()

    def reserve_publisher_intent(
        self,
        *,
        intent_key: str,
        endpoint: str,
        text_hash: str,
        now: float = None,
    ) -> bool:
        """Reserve one public action before sending it across the network."""
        now = now or time.time()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            exists = self.db.execute(
                "SELECT 1 FROM publisher_intents WHERE intent_key=?",
                (intent_key,),
            ).fetchone()
            if exists:
                self.db.rollback()
                return False
            self.db.execute(
                """INSERT INTO publisher_intents(
                       intent_key,endpoint,text_hash,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)""",
                (intent_key, endpoint, text_hash, "pending", now, now),
            )
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def mark_publisher_intent_failed(
        self,
        *,
        intent_key: str,
        now: float = None,
    ) -> None:
        now = now or time.time()
        self.db.execute(
            """UPDATE publisher_intents
               SET status='failed', updated_at=?
               WHERE intent_key=? AND status='pending'""",
            (now, intent_key),
        )
        self.db.commit()

    def complete_publisher_intent(
        self,
        *,
        intent_key: str,
        endpoint: str,
        remote_post_id: str,
        cost_microusd: int,
        now: float = None,
    ) -> None:
        """Atomically complete the intent and book the successful API write."""
        now = now or time.time()
        day = self._day(now)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT status FROM publisher_intents WHERE intent_key=?",
                (intent_key,),
            ).fetchone()
            if row is None or row[0] != "pending":
                raise RuntimeError(
                    f"publisher intent is not pending: {intent_key}"
                )
            self.db.execute(
                """INSERT INTO publisher_usage(
                       endpoint,remote_post_id,cost_microusd,at,day,month
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    endpoint,
                    remote_post_id,
                    int(cost_microusd),
                    now,
                    day,
                    day[:7],
                ),
            )
            self.db.execute(
                """UPDATE publisher_intents
                   SET status='succeeded', remote_post_id=?, updated_at=?
                   WHERE intent_key=?""",
                (remote_post_id, now, intent_key),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def publisher_intent_status(self, intent_key: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM publisher_intents WHERE intent_key=?",
            (intent_key,),
        ).fetchone()
        return None if row is None else str(row[0])

    def publisher_cost_microusd_today(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_microusd),0) FROM publisher_usage WHERE day=?",
            (self._day(now),),
        ).fetchone()
        return int(row[0])

    def publisher_cost_microusd_month(self, now: float = None) -> int:
        now = now or time.time()
        row = self.db.execute(
            "SELECT COALESCE(SUM(cost_microusd),0) FROM publisher_usage WHERE month=?",
            (self._day(now)[:7],),
        ).fetchone()
        return int(row[0])

    def close(self):
        self.db.close()
