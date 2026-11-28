import sqlite3
from contextlib import closing

import pytest

from fixtures import BOB_LID, BOB_PN, PASSWORD, UDID, make_backup, make_chatstorage, make_msgstore
from wamove.android.msgstore import parse
from wamove.cli import main
from wamove.ios import chatstorage, history
from wamove.ios.backup import CHATSTORAGE, Backup

BODY = "Ask @" + BOB_PN.split("@")[0] + " about that"
LINKED = "Ask @" + BOB_LID.split("@")[0] + " about that"
NATIVE_RECEIPT = bytes.fromhex("0801 3a11 120f 0a036f776e 1202c2a9 1801 2001 3800")


@pytest.mark.parametrize("repair_only", [False, True])
def test_build_mentions_and_reactions(tmp_path, monkeypatch, repair_only):
    work = tmp_path / "work"
    (work / "android").mkdir(parents=True)
    android = work / "android" / "msgstore.db"
    make_msgstore(android)
    with closing(sqlite3.connect(android)) as conn, conn:
        conn.executescript("""
            CREATE TABLE message_mentions (message_row_id, jid_row_id);
            CREATE TABLE message_add_on (_id INTEGER PRIMARY KEY, parent_message_row_id,
                from_me, sender_jid_row_id, key_id);
            CREATE TABLE message_add_on_reaction (message_add_on_row_id, reaction, sender_timestamp);
            INSERT INTO message_mentions VALUES (110, 4);
            INSERT INTO message_add_on VALUES (1, 110, 0, 4, 'incoming'),
                (2, 110, 1, NULL, 'old-own'), (3, 114, 0, NULL, 'direct');
            INSERT INTO message_add_on_reaction VALUES (1, '👍', 1700000000001),
                (2, '❤️', 1700000000002), (3, '😂', 1700000000003);
        """)
        conn.execute("UPDATE message SET text_data=? WHERE _id=110", (BODY,))
    database = tmp_path / CHATSTORAGE
    make_chatstorage(database)
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.executescript("""
            ALTER TABLE ZWAMEDIAITEM ADD COLUMN ZMETADATA BLOB;
            ALTER TABLE ZWAMESSAGE ADD COLUMN ZMESSAGEINFO INTEGER;
            CREATE TABLE ZWAMESSAGEINFO (
                Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER, ZMESSAGE INTEGER, ZRECEIPTINFO BLOB);
            INSERT INTO Z_PRIMARYKEY VALUES (7, 'WAMessageInfo', 0, 0);
        """)
    if repair_only:
        archive = parse(android)
        with closing(chatstorage.Writer(database, archive.lids)) as writer:
            writer.import_archive(archive, None)
        with closing(sqlite3.connect(database)) as conn, conn:
            # The same stanza ID and timestamp in another chat must never receive these changes.
            conn.execute("""
                INSERT INTO ZWAMESSAGE
                    (Z_PK,Z_ENT,Z_OPT,ZCHATSESSION,ZISFROMME,ZSTANZAID,ZMESSAGEDATE,ZMESSAGETYPE,ZTEXT)
                SELECT 999,6,1,1,ZISFROMME,ZSTANZAID,ZMESSAGEDATE,0,ZTEXT FROM ZWAMESSAGE WHERE ZSTANZAID='K110'
            """)
            conn.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=999 WHERE Z_ENT=6")
            pk, = conn.execute("SELECT Z_PK FROM ZWAMESSAGE WHERE ZSTANZAID='K110' AND Z_PK<>999").fetchone()
            conn.execute("INSERT INTO ZWAMESSAGEINFO VALUES (1,7,1,?,?)", (pk, NATIVE_RECEIPT))
            conn.execute("UPDATE ZWAMESSAGE SET ZMESSAGEINFO=1 WHERE Z_PK=?", (pk,))
            conn.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=1 WHERE Z_ENT=7")
    pristine = work / "ios" / "pristine" / UDID
    make_backup(pristine, database, PASSWORD)
    original_manifest = (pristine / "Manifest.db").read_bytes()
    monkeypatch.setenv("WAMOVE_BACKUP_PASSWORD", PASSWORD)
    command = ["--work", str(work), "build", "--repair-only" if repair_only else "--no-media"]
    assert main(command) == 0
    assert (pristine / "Manifest.db").read_bytes() == original_manifest

    result = tmp_path / "result.sqlite"
    backup_path = work / "ios" / "patched" / UDID
    with closing(Backup(backup_path, PASSWORD)) as saved:
        assert saved.read(CHATSTORAGE, result)
    assert chatstorage.verify(result) == []
    with closing(sqlite3.connect(result)) as conn:
        text, mentions, receipt = conn.execute("""
            SELECT ZTEXT,ZMETADATA,ZRECEIPTINFO FROM ZWAMESSAGE m
            JOIN ZWAMEDIAITEM i ON i.Z_PK=m.ZMEDIAITEM
            JOIN ZWAMESSAGEINFO r ON r.Z_PK=m.ZMESSAGEINFO
            WHERE m.ZSTANZAID='K110'
        """).fetchone()
        assert text == LINKED
        assert mentions == b"\x42" + bytes([len(BOB_LID)]) + BOB_LID.encode()
        assert "👍".encode() in receipt and BOB_LID.encode() in receipt
        assert ("❤️".encode() in receipt) is not repair_only
        direct, = conn.execute("""
            SELECT ZRECEIPTINFO FROM ZWAMESSAGE m JOIN ZWAMESSAGEINFO r ON r.Z_PK=m.ZMESSAGEINFO
            WHERE m.ZSTANZAID='K114'
        """).fetchone()
        assert "😂".encode() in direct and BOB_LID.encode() in direct
        if repair_only:
            assert receipt.startswith(NATIVE_RECEIPT[:2])
            assert NATIVE_RECEIPT[4:] in receipt
            assert conn.execute("SELECT ZTEXT,ZMEDIAITEM,ZMESSAGEINFO FROM ZWAMESSAGE WHERE Z_PK=999").fetchone() == (
                BODY, None, None)
    with closing(chatstorage.Writer(result)) as writer:
        assert history.repair(writer, android, tmp_path / "absent-lids.sqlite") == history.Report()
