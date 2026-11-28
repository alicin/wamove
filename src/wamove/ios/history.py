"""Translate Android mentions and reactions into iOS ChatStorage metadata."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from .chatstorage import COCOA_EPOCH, Writer

LID = re.compile(r"([0-9]+)@lid")
PHONE = re.compile(r"\+?([0-9]+)(?:@(?:s\.whatsapp\.net|c\.us))?")
MENTION = re.compile(r"(?<![\w@])@([0-9]+)(?![\w])")


@dataclass
class Report:
    mentions: int = 0
    reactions: int = 0


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 64, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("Invalid protobuf varint")


def _fields(data: bytes):
    if not isinstance(data, bytes):
        raise ValueError("Expected binary protobuf metadata")
    offset = 0
    while offset < len(data):
        start = offset
        tag, offset = _read_varint(data, offset)
        number, wire = divmod(tag, 8)
        if not number:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, offset = _read_varint(data, offset)
            else:
                size = 8 if wire == 1 else 4
            end = offset + size
            if end > len(data):
                raise ValueError("Truncated protobuf field")
            value = data[offset:end]
            offset = end
        else:
            raise ValueError("Unsupported protobuf wire type")
        yield number, wire, value, data[start:offset]


def _bytes_field(number: int, value: str | bytes) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return _varint(number * 8 + 2) + _varint(len(value)) + value


def _int_field(number: int, value: int) -> bytes:
    return _varint(number * 8) + _varint(value)


def _reaction(key: str, emoji: str, stamp: int, actor: str | None) -> bytes:
    # iOS uses different fields for an incoming reaction and one from this account.
    if actor is None:
        return _bytes_field(2, _bytes_field(1, key) + _bytes_field(2, emoji) +
                            _int_field(3, stamp) + _int_field(4, 1) + _int_field(7, 0))
    return _bytes_field(1, _bytes_field(1, key) + _bytes_field(2, actor) +
                        _bytes_field(3, emoji) + _int_field(4, stamp) + _int_field(5, 1) + _int_field(7, 0))


def _merge_receipt(
    blob: bytes, events: dict[str | None, tuple[str, str, int]], identities: Identities
) -> tuple[bytes, int]:
    fields = []
    reactions = []
    actors = set()
    position = None
    try:
        for number, wire, data, raw in _fields(blob):
            if (number, wire) != (7, 2):
                fields.append(raw)
                continue
            if position is None:
                position = len(fields)
            for number, wire, value, raw in _fields(data):
                reactions.append(raw)
                if (number, wire) == (2, 2):
                    actors.add(None)
                elif (number, wire) == (1, 2):
                    actor = next((jid for n, w, jid, _ in _fields(value) if (n, w) == (2, 2)), None)
                    if not actor:
                        return blob, 0
                    jid = actor.decode("utf-8")
                    actors.add(identities.lid(jid) or jid)
    except ValueError:
        return blob, 0
    pending = [_reaction(*event, actor) for actor, event in events.items() if actor not in actors]
    if not pending:
        return blob, 0
    fields.insert(position if position is not None else len(fields), _bytes_field(7, b"".join(reactions + pending)))
    return b"".join(fields), len(pending)


def _columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')}


class Identities:
    def __init__(self, conn: sqlite3.Connection, ios_lids: Path):
        self.phones: dict[str, set[str]] = defaultdict(set)
        self.lids: dict[str, set[str]] = defaultdict(set)
        if {"lid_row_id", "jid_row_id"} <= _columns(conn, "jid_map", "android"):
            for phone, lid in conn.execute("""
                SELECT p.raw_string, l.raw_string FROM android.jid_map AS pair
                JOIN android.jid AS l ON l._id = pair.lid_row_id
                JOIN android.jid AS p ON p._id = pair.jid_row_id
            """):
                self._add(phone, lid)
        if ios_lids.is_file() and ios_lids.stat().st_size:
            with closing(sqlite3.connect(ios_lids.resolve().as_uri() + "?mode=ro", uri=True)) as ios:
                for table, lid_column in (("ZWAPHONENUMBERLIDPAIR", "ZLID"), ("ZWAZACCOUNT", "ZIDENTIFIER")):
                    if {"ZPHONENUMBER", lid_column} <= _columns(ios, table):
                        for phone, lid in ios.execute(f"SELECT ZPHONENUMBER, {lid_column} FROM {table}"):
                            self._add(phone, lid)

    def _add(self, phone: str | None, lid: str | None) -> None:
        number = PHONE.fullmatch(phone or "")
        if lid and lid.isascii() and lid.isdecimal():
            lid += "@lid"
        if number and lid and LID.fullmatch(lid):
            self.phones[number[1]].add(lid)
            self.lids[lid].add(number[1])

    def lid(self, jid: str | None) -> str | None:
        account = LID.fullmatch(jid or "")
        if account:
            return account[0]
        phone = PHONE.fullmatch(jid or "")
        matches = self.phones.get(phone[1], ()) if phone else ()
        if len(matches) == 1:
            lid = next(iter(matches))
            if len(self.lids[lid]) == 1:
                return lid
        return None

    def aliases(self, jid: str | None) -> set[str]:
        if not jid:
            return set()
        aliases = {jid}
        phone = PHONE.fullmatch(jid)
        if phone:
            aliases.add(phone[1] + "@s.whatsapp.net")
        lid = self.lid(jid)
        if lid:
            aliases.add(lid)
        if LID.fullmatch(jid):
            numbers = self.lids.get(jid, ())
            if len(numbers) == 1:
                number = next(iter(numbers))
                if len(self.phones[number]) == 1:
                    aliases.add(number + "@s.whatsapp.net")
        return aliases


_MENTION_PARENTS = "SELECT message_row_id FROM android.message_mentions"
_REACTION_PARENTS = """
    SELECT a.parent_message_row_id FROM android.message_add_on AS a
    JOIN android.message_add_on_reaction AS r ON r.message_add_on_row_id = a._id
"""


def _parents(writer: Writer, identities: Identities, sources: list[str]) -> dict[int, sqlite3.Row]:
    # A stanza ID can be reused in another chat. Match chat identity, direction and timestamp as well.
    conn = writer.conn
    candidates = defaultdict(list)
    for row in conn.execute(f"""
        SELECT a._id, p.Z_PK, p.ZTEXT, p.ZMESSAGETYPE, s.ZSESSIONTYPE,
               s.ZCONTACTJID, s.ZCONTACTIDENTIFIER, peer.raw_string AS android_peer
        FROM android.message AS a
        JOIN android.chat AS c ON c._id = a.chat_row_id
        JOIN android.jid AS peer ON peer._id = c.jid_row_id
        JOIN ZWAMESSAGE AS p ON p.ZSTANZAID = a.key_id AND p.ZISFROMME = a.from_me
          AND abs(p.ZMESSAGEDATE - (a.timestamp / 1000.0 - ?)) < 0.01
        JOIN ZWACHATSESSION AS s ON s.Z_PK = p.ZCHATSESSION
        WHERE a._id IN ({" UNION ".join(sources)})
    """, (COCOA_EPOCH,)):
        ios_aliases = identities.aliases(row["ZCONTACTJID"]) | identities.aliases(row["ZCONTACTIDENTIFIER"])
        if ios_aliases & identities.aliases(row["android_peer"]):
            candidates[row["_id"]].append(row)
    return {android_id: rows[0] for android_id, rows in candidates.items() if len(rows) == 1}


def _add_mentions(writer: Writer, parents: dict[int, sqlite3.Row], identities: Identities) -> int:
    conn = writer.conn
    mentions = defaultdict(set)
    for android_id, original, jid in conn.execute("""
        SELECT a.message_row_id, m.text_data, j.raw_string FROM android.message_mentions AS a
        JOIN android.jid AS j ON j._id = a.jid_row_id JOIN android.message AS m ON m._id = a.message_row_id
        WHERE m.message_type = 0
    """):
        parent = parents.get(android_id)
        if parent is None or parent["ZMESSAGETYPE"] != 0 or not original or parent["ZTEXT"] != original:
            continue
        actor = identities.lid(jid)
        if actor:
            # Android may put either the phone number or the LID in the visible token.
            for alias in identities.aliases(jid):
                mentions[parent["Z_PK"]].add((alias.split("@", 1)[0].lstrip("+"), actor))

    added = 0
    for pk, aliases in mentions.items():
        targets = defaultdict(set)
        for token, actor in aliases:
            targets[token].add(actor)
        resolved = {token: next(iter(actors)) for token, actors in targets.items() if len(actors) == 1}
        body, item = conn.execute("SELECT ZTEXT, ZMEDIAITEM FROM ZWAMESSAGE WHERE Z_PK=?", (pk,)).fetchone()
        row = conn.execute("SELECT ZMETADATA FROM ZWAMEDIAITEM WHERE Z_PK=? AND ZMESSAGE=?", (item, pk)).fetchone()
        if item is not None and row is None:
            continue
        blob = row[0] if row and row[0] is not None else b""
        try:
            if any((n, w) == (8, 2) for n, w, _, _ in _fields(blob)):
                continue
        except ValueError:
            continue
        jids: dict[str, None] = {}

        def replace(match: re.Match[str]) -> str:
            actor = resolved.get(match[1])
            if not actor:
                return match[0]
            jids[actor] = None
            return "@" + actor.split("@", 1)[0]

        text = MENTION.sub(replace, body)
        if not jids:
            continue
        metadata = b"".join(_bytes_field(8, jid) for jid in jids) + blob
        if row:
            writer.update("ZWAMEDIAITEM", item, {"ZMETADATA": metadata})
        else:
            item = writer.insert("ZWAMEDIAITEM", "WAMediaItem", {
                "ZMESSAGE": pk, "ZMETADATA": metadata, "ZFILESIZE": 0, "ZMEDIAORIGIN": 0,
                "ZMOVIEDURATION": 0, "ZASPECTRATIO": 0, "ZHACCURACY": 0, "ZLATITUDE": 0, "ZLONGITUDE": 0,
            })
        writer.update("ZWAMESSAGE", pk, {"ZTEXT": text, "ZMEDIAITEM": item})
        added += len(jids)
    return added


def _add_reactions(writer: Writer, parents: dict[int, sqlite3.Row], identities: Identities) -> int:
    conn = writer.conn
    reactions = defaultdict(dict)
    for android_id, own, sender, key, emoji, stamp in conn.execute("""
        SELECT a.parent_message_row_id, a.from_me, j.raw_string, a.key_id, r.reaction, r.sender_timestamp
        FROM android.message_add_on_reaction AS r JOIN android.message_add_on AS a ON a._id = r.message_add_on_row_id
        LEFT JOIN android.jid AS j ON j._id = a.sender_jid_row_id
        ORDER BY r.sender_timestamp, a._id
    """):
        parent = parents.get(android_id)
        if parent is None or not key or not isinstance(stamp, int) or stamp <= 0:
            continue
        peer = parent["android_peer"] if parent["ZSESSIONTYPE"] == 0 else None
        actor = None if own else identities.lid(sender or peer)
        if not own and not actor:
            continue
        reactions[parent["Z_PK"]][actor] = (key, emoji, stamp)

    added = 0
    for pk, events in reactions.items():
        events = {actor: event for actor, event in events.items() if event[1]}
        if not events:
            continue
        info, = conn.execute("SELECT ZMESSAGEINFO FROM ZWAMESSAGE WHERE Z_PK=?", (pk,)).fetchone()
        row = conn.execute("SELECT ZRECEIPTINFO FROM ZWAMESSAGEINFO WHERE Z_PK=? AND ZMESSAGE=?", (info, pk)).fetchone()
        if info is not None and row is None:
            continue
        metadata, count = _merge_receipt(row[0] if row and row[0] is not None else b"", events, identities)
        if not count:
            continue
        if row:
            writer.update("ZWAMESSAGEINFO", info, {"ZRECEIPTINFO": metadata})
        else:
            info = writer.insert("ZWAMESSAGEINFO", "WAMessageInfo", {"ZMESSAGE": pk, "ZRECEIPTINFO": metadata})
            writer.update("ZWAMESSAGE", pk, {"ZMESSAGEINFO": info})
        added += count
    return added


def repair(writer: Writer, android: Path, ios_lids: Path) -> Report:
    conn = writer.conn
    report = Report()
    conn.execute("ATTACH DATABASE ? AS android", (android.resolve().as_uri() + "?mode=ro",))
    try:
        mentions = ("ZMETADATA" in writer.columns.get("ZWAMEDIAITEM", ()) and
                    "WAMediaItem" in writer.entities and
                    {"message_row_id", "jid_row_id"} <= _columns(conn, "message_mentions", "android"))
        reactions = ("ZMESSAGEINFO" in writer.columns["ZWAMESSAGE"] and
                     {"ZMESSAGE", "ZRECEIPTINFO"} <= set(writer.columns.get("ZWAMESSAGEINFO", ())) and
                     "WAMessageInfo" in writer.entities and
                     {"_id", "parent_message_row_id", "from_me", "key_id", "sender_jid_row_id"}
                     <= _columns(conn, "message_add_on", "android") and
                     {"message_add_on_row_id", "reaction", "sender_timestamp"}
                     <= _columns(conn, "message_add_on_reaction", "android"))
        if not mentions and not reactions:
            return report
        identities = Identities(conn, ios_lids)
        sources = [query for enabled, query in ((mentions, _MENTION_PARENTS), (reactions, _REACTION_PARENTS)) if enabled]
        parents = _parents(writer, identities, sources)
        with conn:
            for enabled, table, entity in (
                (mentions, "ZWAMEDIAITEM", "WAMediaItem"),
                (reactions, "ZWAMESSAGEINFO", "WAMessageInfo"),
            ):
                if enabled:
                    highest, = conn.execute(f"SELECT coalesce(max(Z_PK), 0) FROM {table}").fetchone()
                    writer.entities[entity]["max"] = max(writer.entities[entity]["max"], highest)
            if mentions:
                report.mentions = _add_mentions(writer, parents, identities)
            if reactions:
                report.reactions = _add_reactions(writer, parents, identities)
            for count, name in ((report.mentions, "WAMediaItem"), (report.reactions, "WAMessageInfo")):
                if count:
                    entity = writer.entities[name]
                    conn.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=? WHERE Z_ENT=?", (entity["max"], entity["ent"]))
        return report
    finally:
        conn.execute("DETACH DATABASE android")
