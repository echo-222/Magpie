"""Human adjustment of a Material Pack (spec §6.3-G): remove / move / add / note / rename.

Every operation is logged in `pack.human_edits` and persisted. Removed materials go to
`removed_material_ids`, which the alternatives step honours, so regeneration never
silently undoes a human decision.
"""

from __future__ import annotations

from .db import Database
from .models import HumanEdit, MaterialPack, PackGroup, PackMember


class PackError(ValueError):
    pass


def _ensure_group(pack: MaterialPack, name: str) -> PackGroup:
    name = (name or "").strip()
    if not name:
        raise PackError("group name required")
    g = pack.find_group(name)
    if g is None:
        g = PackGroup(name=name, purpose="added by human")
        pack.groups.append(g)
    return g


def remove_member(db: Database, pack: MaterialPack, material_id: str) -> MaterialPack:
    found = pack.find_member(material_id)
    if not found:
        raise PackError(f"{material_id} is not in this pack")
    group, member = found
    group.members.remove(member)
    if material_id not in pack.removed_material_ids:
        pack.removed_material_ids.append(material_id)
    pack.human_edits.append(HumanEdit(op="remove", material_id=material_id, from_group=group.name))
    return db.save_pack(pack)


def move_member(db: Database, pack: MaterialPack, material_id: str, to_group: str, position: int | None = None) -> MaterialPack:
    found = pack.find_member(material_id)
    if not found:
        raise PackError(f"{material_id} is not in this pack")
    src, member = found
    src.members.remove(member)
    dst = _ensure_group(pack, to_group)
    if position is None or position >= len(dst.members):
        dst.members.append(member)
    else:
        dst.members.insert(max(0, position), member)
    pack.human_edits.append(HumanEdit(op="move", material_id=material_id, from_group=src.name, to_group=dst.name))
    return db.save_pack(pack)


def add_member(db: Database, pack: MaterialPack, material_id: str, group: str, reason: str | None = None, role: str | None = None) -> MaterialPack:
    if db.get_material(material_id) is None:
        raise PackError(f"unknown material {material_id}")
    if pack.find_member(material_id):
        raise PackError(f"{material_id} already in pack")
    dst = _ensure_group(pack, group)
    if material_id in pack.removed_material_ids:  # explicit human re-add wins over earlier removal
        pack.removed_material_ids.remove(material_id)
    dst.members.append(PackMember(material_id=material_id, role=role, reason=reason, added_by="human"))
    pack.human_edits.append(HumanEdit(op="add", material_id=material_id, to_group=dst.name, detail=reason))
    return db.save_pack(pack)


def set_note(db: Database, pack: MaterialPack, material_id: str, note: str | None) -> MaterialPack:
    found = pack.find_member(material_id)
    if not found:
        raise PackError(f"{material_id} is not in this pack")
    found[1].note = (note or "").strip() or None
    pack.human_edits.append(HumanEdit(op="note", material_id=material_id, detail=found[1].note))
    return db.save_pack(pack)


def rename_group(db: Database, pack: MaterialPack, old: str, new: str) -> MaterialPack:
    g = pack.find_group(old)
    if g is None:
        raise PackError(f"no group {old!r}")
    new = (new or "").strip()
    if not new:
        raise PackError("new group name required")
    if pack.find_group(new):
        raise PackError(f"group {new!r} already exists")
    g.name = new
    pack.human_edits.append(HumanEdit(op="rename_group", from_group=old, to_group=new))
    return db.save_pack(pack)
