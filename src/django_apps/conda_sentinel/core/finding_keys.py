"""`CPM-AD-22`'s finding key: what a piece of work is filed under, across re-observation.

The failure this exists to prevent is stated in the decision and is worth restating,
because it is not obvious and it is silent: **an accepted finding resurrecting as new
work tomorrow.** Evidence is append-only (`CPM-AD-2`), so tonight's collector run
inserts a *new row* for the same advisory it saw yesterday. A workflow item keyed on
an evidence row id would therefore find no item for that new row, open a second one,
and put a finding somebody accepted last week back at the top of a queue -- with no
error anywhere, and with the original item still sitting there resolved.

So an item is keyed on a **natural key**: the facts that make this finding *this*
finding, which do not change when the same fact is observed again. `CPM-AD-22` gives
the vulnerability case directly -- `package + advisory_id + affected_range` -- and the
shape generalises: every table that can produce work declares its own.

**The key is a bounded digest with a readable prefix, and both halves are load-bearing.**
A natural key spelled out in full is unbounded -- an affected-version range can be a
long expression -- and a column has a width. A pure hash is bounded and tells a
reviewer reading a database nothing at all. So the stored key is
`<table>:<package>:<digest>`: the prefix answers "what kind of work, about which
package" at a glance, and the digest makes the rest exact without a length limit.
`finding_facts` carries the readable form beside it, which is what a person actually
reads when two items look alike.

**The digest is over a length-prefixed encoding, not over a joined string.** Joining
with a separator makes `("ab", "c")` and `("a", "bc")` the same key for any separator
that can appear in a value, and advisory identifiers and version ranges can contain
very nearly anything. Length-prefixing removes the question rather than answering it
with a refusal a caller would have to handle.

**Declared alongside the evidence table, never centrally.** A registry of keys in one
module would let a table be added without one, which is the failure mode this is
about -- the missing declaration is invisible until a duplicate item appears in a
queue weeks later. A table that can produce work inherits `FindingKeyed` and names
its own fields, and `tests/unit/django_apps/test_finding_keys.py` sweeps for a table
that produces work and declares none.

**On the `AD-` prefix.** A bare `AD-n` in this repository is an *inherited* platform
decision; a decision from this product's own architecture spine always carries the
`CPM-` prefix.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Final

from django.db import models

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DIGEST_LENGTH",
    "FINDING_FACTS_LENGTH",
    "FINDING_KEY_LENGTH",
    "FindingKeyError",
    "FindingKeyed",
    "finding_key_of",
]

#: How many hex characters of the digest the key carries.
#:
#: Sixteen, which is 64 bits. At `CPM-NFR-1`'s ten thousand packages and a few
#: findings each, a birthday collision is on the order of one in a hundred billion --
#: far below the rate at which a person mistypes an advisory identifier, which is the
#: failure this number is competing with. The full digest would make the key
#: unreadable in a queue listing for no gain a reviewer could ever observe.
DIGEST_LENGTH: Final[int] = 16

#: The column widths. The key is bounded by construction -- a table name, a package
#: id and a fixed digest -- and the facts are not, so they are stored in a text
#: column and this is the point at which a very long one is truncated for display.
FINDING_KEY_LENGTH: Final[int] = 128
FINDING_FACTS_LENGTH: Final[int] = 1024

#: What separates the readable half's parts. Only ever in the prefix, which is built
#: from a table name and an integer, so no value a source supplied can contain it.
_PREFIX_SEPARATOR: Final[str] = ":"


class FindingKeyError(Exception):
    """A finding key could not be built from a row that was asked for one.

    Named rather than left as `AttributeError` or `ValueError`, because the useful
    half of the report is *which* declaration is wrong: a table that named a field it
    does not have, or one that declared no fields at all and was asked anyway. Both
    are programming errors in a declaration, and both are silent until a queue fills
    with duplicates.
    """


def finding_key_of(table: str, package_id: int, facts: Sequence[tuple[str, str]]) -> tuple[str, str]:
    """Return the stored key and the readable facts for one finding.

    Args:
        table: The evidence table the finding came from, by its schema name.
        package_id: The package the finding is about. Always part of the key: two
            packages can be affected by one advisory, and they are two pieces of
            work.
        facts: The declared key fields as `(name, value)` pairs, in declaration
            order. Order is part of the key, deliberately -- a table that reorders
            its declaration has changed what it considers the same finding, and
            should get different keys rather than silently merge two.

    Returns:
        The bounded key, and the readable form to store beside it.

    Raises:
        FindingKeyError: When no fact is given. A key over nothing but the package
            would file every finding of that kind under one item, which is the
            opposite failure to the one this module prevents and just as quiet.

    """
    if not facts:
        message = (
            f"{table} was asked for a finding key with no key fields. A key over the package alone would file "
            f"every finding of this kind under one item; CPM-AD-22 wants the facts that make this finding this "
            f"finding, declared on the table itself."
        )
        raise FindingKeyError(message)

    # Length-prefixed, so no separator can be confused with a value. See the module
    # docstring: joining is what makes ("ab", "c") and ("a", "bc") the same key.
    encoded = "".join(f"{len(name)}:{name}={len(value)}:{value}" for name, value in facts)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:DIGEST_LENGTH]
    readable = " ".join(f"{name}={value}" for name, value in facts)
    key = _PREFIX_SEPARATOR.join((table, str(package_id), digest))
    return key, readable[:FINDING_FACTS_LENGTH]


class FindingKeyed(models.Model):
    """Evidence that can become work declares the key that work is filed under.

    A mixin rather than a registry, for the reason the module docstring gives: a
    central table of keys lets an evidence table be added without one, and the
    missing declaration is invisible until a duplicate item shows up in a queue.

    A table that inherits this and leaves `FINDING_KEY_FIELDS` empty is refused when
    it is asked for a key -- loudly, at the point of asking, rather than by filing
    everything under one item.
    """

    #: The fields whose values make this finding *this* finding, in the order they
    #: contribute to the key.
    #:
    #: **`observed_at` and the primary key are never among them.** Those are what
    #: change when the same fact is observed again, which is the whole thing
    #: `CPM-AD-22` is about; `tests/unit/django_apps/test_finding_keys.py` refuses a
    #: declaration naming either.
    FINDING_KEY_FIELDS: ClassVar[tuple[str, ...]] = ()

    class Meta:
        """Abstract, so this declaration creates no table and no migration."""

        abstract = True

    def finding_key(self, *, epoch: Sequence[tuple[str, str]] = ()) -> tuple[str, str]:
        """Return this row's finding key and its readable facts.

        Args:
            epoch: Facts appended after the declared key fields, for a finding whose
                work is filed under a *period* as well as under the facts -- the
                instant a package's current inventory listing began
                (`CPM-OPERATE-S11`), so the item the product closed when the package
                left stays closed and fresh work opens beside it. Empty for the
                ordinary key, and the ordinary key is byte-identical to what it was
                before this argument existed.

        Returns:
            The bounded key and the readable form, as `finding_key_of` builds them.

        Raises:
            FindingKeyError: When this table declares no key fields, or declares one
                it does not have. Both are errors in the declaration and both are
                silent in production until a queue fills with duplicates, so they are
                raised where the mistake is rather than absorbed.

        """
        table = self._meta.db_table
        facts: list[tuple[str, str]] = []
        for name in self.FINDING_KEY_FIELDS:
            try:
                value = getattr(self, name)
            except AttributeError as missing:
                message = (
                    f"{table} declares {name!r} as a finding key field and has no such attribute. A key field "
                    f"is a column of the table it is declared on."
                )
                raise FindingKeyError(message) from missing
            facts.append((name, "" if value is None else str(value)))
        facts.extend(epoch)
        return finding_key_of(table, self.package_id, facts)  # type: ignore[attr-defined]
