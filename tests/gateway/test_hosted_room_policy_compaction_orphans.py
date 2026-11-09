"""A compacted discussion anchor must not strand its settled receipt in the bounded snapshot.

Transcript compaction keeps the newest ``MAX_THREAD_TRANSCRIPT_EVENTS`` thread messages. A
``turn.settled`` receipt published around the cutoff survives while its ``message.user`` anchor
is deleted, and the merged snapshot then fails strict watermark validation forever: every
``prepare`` raises, the stall tracker parks the room, and no member can ever be woken again.
"""

from gateway import hosted_room_discussion as discussion
from gateway import hosted_room_policy_checkpoint as checkpoint_module
from gateway import hosted_rooms as rooms
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint

PROFILES = ("writer", "reviewer")
ROOM = "compact-room"
THREAD = "thread"


def append(db, event_id, kind, payload, actor_kind="user"):
    return rooms.append_event(db, room_id=ROOM, event_id=event_id, kind=kind,
        actor={"kind": actor_kind, "id": "home"}, authority_gateway_id="home", authority_epoch=1, payload=payload)


def log(db):
    return rooms.read_events(db, room_id=ROOM)["events"]


def test_compacted_anchor_does_not_strand_its_settled_receipt(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    # A tiny window so three newer messages push the cutoff past the anchor while the
    # committed member message and its receipt stay inside it.
    monkeypatch.setattr(checkpoint_module, "MAX_THREAD_TRANSCRIPT_EVENTS", 4)
    room = rooms.create_room(db, room_id=ROOM, name="Compact", authority_gateway_id="home",
        members=[{"member_id": profile, "profile": profile, "handle": profile} for profile in PROFILES])
    append(db, "anchor", "message.user", {"text": "@writer Prepare the plan.", "thread_id": THREAD})
    task = discussion.plan_next_task(room, log(db), local_profiles=PROFILES, freeze_input_context=True).task
    assert task is not None
    settled = discussion.plan_publication(room, log(db), task, status="settled",
        result={"text": "The plan is ready."}, local_profiles=PROFILES)
    for event in settled.events:
        rooms.append_event(db, **event.append_kwargs(ROOM))
    for index in range(3):
        append(db, f"filler-{index}", "message.user", {"text": f"filler {index}", "thread_id": THREAD})

    snapshot = HostedRoomPolicyCheckpoint(db).snapshot(room_id=ROOM, latest_seq=log(db)[-1]["seq"])
    # The anchor is gone from the window but its receipt survived it: preparing the room
    # must not raise, and the newest message still gets its member turn.
    decision = discussion.plan_next_task(room, list(snapshot.events), local_profiles=PROFILES,
        initial_watermarks=snapshot.watermarks, freeze_input_context=True)
    assert decision.status == "task"
    assert decision.discussion_event_id == "filler-2"
