# The queues: how work opens, moves and closes

Three queues, one table, and a state machine declared as data.

---

## What a queue is here

A queue is **not** a list somebody maintains. It is a filtered view of one table,
`workflow_items`, and its rows are opened by the policy run rather than by a person.

| Queue | Owned by | Opened when |
|---|---|---|
| `remediation` | `packaging_engineer` | The vulnerability pass matched an advisory |
| `compliance_review` | `security_reviewer` | The licence pass could not clear a licence |
| `identity_review` | `leadership` | The resolver could not identify a package |

Ranked worst-first by the priority pass's own bucket, then by score. The rank is not a
second opinion invented by the queue — the priority pass already decided, and the queue
reproduces its order.

---

## How an item opens

After a policy run finishes, a registered step reads what the passes concluded and
opens an item wherever a verdict says there is work. It **concludes nothing itself**:
it does not decide whether something is a problem, and it does not rank anything.

Three sources, three queues — and the identity one is different in kind. A matched
advisory and an uncleared licence are each backed by a derived row; an unidentified
package's "finding" is the *absence* of an identity, so its key is built from the
package rather than from a row.

### Keyed, so nightly runs do not pile up

Every item is opened by a **finding key**. A run that has already opened an item for a
finding finds it rather than making a second one. That is what makes the step safe to
run every night: after the first run, most nights open nothing at all.

### The confidence gate decides what is even askable

An `unmapped` package has no verdicts worth acting on — every status on it is
`unknown`. So it opens **exactly one** item, in the identity queue, and none of the
others.

Opening a remediation item for a package nobody has identified would put work in a
queue that cannot be done until different work in a different queue is finished first.

---

## The state machine

Five states. `open` is reachable **only** by the policy run — no transition produces
it, so nothing a person does can put an item back into the state that means "nobody
has looked at this".

```
open ──► triaged ──┬──► routed ──► in_progress ──► resolved
                   │                   │
                   ├──► in_progress ◄──┘  (release the claim)
                   │
                   └──► accepted        (security_reviewer only, reason required)
```

The whole table, which is the code's own and not a paraphrase:

| From | To | Who | Reason? | Means |
|---|---|---|---|---|
| `open` | `triaged` | any product role | no | acknowledged the finding |
| `triaged` | `routed` | any product role | no | sent it to the queue that can act on it |
| `triaged` | `in_progress` | any product role | no | claimed it |
| `routed` | `in_progress` | any product role | no | claimed it |
| `in_progress` | `resolved` | any product role | no | resolved it |
| `in_progress` | `triaged` | any product role | no | released the claim |
| `triaged` | `accepted` | **`security_reviewer`** | **yes** | accepted the risk |

`resolved` and `accepted` are terminal. A queue shows open work only, because a queue
whose length grows monotonically stops being read.

### Why a table and not methods

A state machine written as `def triage()`, `def route()`, `def resolve()` spreads its
rules across as many methods as there are transitions, and the rule that matters —
*which moves exist* — is readable only by reading all of them. Worse, the role check
gets copied into each one, which is "every view inventing its own check" moved one
layer down.

As a table it is seven lines and a reviewer sees the whole machine at once. The service
function can do nothing the table does not permit.

### `blocked` is deliberately not a state

The mockups mark it explicitly as *a display state, not a state change*. An item whose
remediation readiness is `blocked` stays open at its bucket, and the policy re-derives
that readiness every run. A sixth state here would let a person freeze an item in a
condition the policy engine is supposed to keep re-deciding — and the item would then
be wrong the moment a fix appeared.

### Accepting a risk is the one transition that demands a reason

It is also the only one restricted to a single role. The justification is required by
the **declaration**, so the service refuses without one rather than each caller
remembering to ask.

---

## Moving an item

!!! warning "There is no button for this yet"

    The queue screens are **read-only**. Every transition is an API call:

    ```
    POST /conda-sentinel/api/v1/workflow-items/<item_id>/transition/
    ```

    The screens show you the work and let you find it; moving it is an integration's
    job today. If you were expecting to click something, you were not wrong to —
    it just is not built.

    The one button a reviewer *does* have is on the package page an item links to:
    **Collect now** re-runs every applicable collector on that package without
    waiting for the sweep, which is what you want after an override or once a fix
    has landed and before you move the item. It is on the
    [operations page](operations.md#collect-now-a-manual-recollection-from-the-page).

The endpoint resolves its permission from the queue the item is **currently in**, not
from a fixed role. An item routed to another queue is that queue's to move.

Four things are checked, separately, and each has its own refusal:

1. The caller believed the item was in the state it is actually in — a stale client
   moving an item somebody else already moved is refused, not silently applied.
2. The move is one the table declares.
3. The caller holds the role that transition requires.
4. A justification is present where the transition demands one.

---

## Maintaining the queues

### Finding one item

Every queue and every report is searchable by package name:

```
/conda-sentinel/queues/remediation/?q=aiohttp
```

The fragment narrows what your role may already see and never widens it — the queue is
selected first. No spelling of `?q=` reaches another queue's items.

### Reading a queue's length as a signal

A queue that grows without bound is not necessarily a backlog of work. Check, in this
order:

| Ask | Because |
|---|---|
| Is the policy run happening at all? | If it is not, no new items open and the old ones are stale |
| Are items being *closed*? | Nothing in the product closes an item; a person must |
| Did a collector start failing? | An `error` verdict is still a verdict, and it opens work |
| Did a rule set change? | A new policy version can open work the old one did not |

### Items are not deleted

Resolving or accepting moves an item to a terminal state; it stays in the table and
drops off the queue view. There is no delete path, and neither prune touches them:
`prune_expired_state` prunes expired sessions and mapper epoch records, and
`prune_evidence` — the nightly purge of evidence and run-ledger rows older than the
retention — names every `workflow` table as one it never touches
([operations](operations.md#ninety-days-of-evidence-purged-nightly)).

### A resolved finding that recurs opens a new item

Because the item is keyed by the finding. If the same advisory matches the same package
again after the item was resolved, that is a new finding key only if the underlying
finding row is new. A finding that never went away keeps its original item — which is
why "most nights open nothing at all" is true.

---

## What a queue item carries

`workflow_items` holds the item's **current** state and nothing about how it got there:

| Field | What it is |
|---|---|
| `finding_key` | What makes it the same item across runs. Unique. |
| `finding_facts` | The readable form of that key, so a reviewer never reverses a digest |
| `package` | A real relation — deleting a package with open work is refused by the database |
| `queue` | Which of the three |
| `state` | One of the five |
| `claimed_by` | Who has it, when `in_progress` |
| `opened_at`, `changed_at` | When the run opened it, and when it last moved |

The history is a **separate, append-only table**, `workflow_transitions`, one row per
move:

| Field | What it is |
|---|---|
| `item`, `from_state`, `to_state` | The move |
| `queue` | The queue it was in *at the time* — an item can be routed |
| `actor` | Who did it |
| `justification` | The reason, required on `accepted` |
| `occurred_at` | When |

That split is the point. Asking "what state is this in" is a read of one row; asking
"who accepted this risk and why" is a read of a log nothing may edit. A `justification`
column on the item would hold only the most recent reason, and an item that had been
accepted, reopened and accepted again would quietly lose the first one.

The rank columns on the queue screen — bucket and score — are stored on **neither**.
They are read from the rollup by correlated subquery at display time, so a queue always
ranks by what the latest policy run thinks rather than by what was true when the item
opened.
