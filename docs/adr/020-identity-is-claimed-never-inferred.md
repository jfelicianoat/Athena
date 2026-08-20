# ADR-020: Identity is claimed, never inferred

- Status: **Accepted** — implemented 2026-08-20 in `athena.identity`, `athena.channels`, `athena.adapters.channel_gateway`, `athena.adapters.service.server`
- Date: 2026-08-20
- Affects: Athena (identity directory, channel ownership), ChatyGPT (link-code endpoints), Telegram (`/link`, `/unlink`)

## Context

A person using ChatyGPT and Telegram is one person. Athena had no way to know that, so they
were two: two owners, two grants, two sets of runs, two answers to "what am I working on".
Worse, the same human could start a run from each surface and get two agents on one
workspace — which is the duplication this exists to prevent, arriving as a race rather than
as a mistake anyone made.

The tempting fix is the wrong one. Both surfaces carry a name — a Telegram username, a
display name, sometimes an email — and matching them is one line of code. A Telegram
username can be released and re-registered by a stranger within days, so that line says
"whoever holds this string today is whoever held it when we wrote the record down". It is
not identity; it is a name collision with consequences.

## Decision

**Athena owns the linking, and the only evidence is a token Athena issued.** A channel knows
a chat id. A client knows its own session. Neither is in a position to decide that two
accounts are the same human, and both are easy to lie to. The claim is made once, in
`athena.identity`, and nothing else may make it.

**Names never link anything.** Not a display name, not a username, not a matching email.
`UserIdentity.display_name` exists to make a log readable and is consulted by no decision in
the module. `user_id` is an opaque UUID rather than an address, because an identifier that
doubles as a way to reach someone is one that will eventually be matched on.

**The token is a short code with a short life.** Twelve characters over a 30-symbol
alphabet — no I, L, O, U, 0 or 1, because a person retypes this out of a chat window. That
is under 59 bits, which is enough on its own and is not on its own: the code dies in ten
minutes, works once, and guessing is rate limited. The rate limit is not what makes 59 bits
safe; it is what lets the code be short enough that somebody will actually type it.

**Hashed at rest.** SHA-256, not a password hash: there is no dictionary to slow down and
no reuse across services to protect, and the threat being answered is a leaked database
being a working set of codes. The plaintext exists in the mint response and nowhere else,
ever — including the audit trail, which records `token_id` instead.

**One statement, condition in the `WHERE`.** Redemption marks the token used with
`... WHERE token_id = ? AND consumed_at IS NULL` and checks `rowcount`. Reading the token
and then marking it used would leave a window where two redemptions both saw it unused,
which is exactly what "one use" is supposed to deny. A test races two redemptions to prove
it.

**Every failure counts against the account, including stale ones.** Only counting wrong
guesses would let an attacker probe for free by supplying something that fails an earlier
check. The counter is per channel account rather than global, because a global one turns a
brute-force defence into a denial of service — one guesser could lock everybody out.

**Failures say one thing.** Unknown, expired and already-used share a message. Told apart,
they answer "did this code ever exist" for someone who is guessing, which is the single
question a brute-force attempt most wants answered.

**A linked account is never rebound.** Redeeming a valid token onto an account that already
belongs to someone is refused before the token is touched — so the refusal cannot be used to
burn somebody else's code either. Rebinding is the shape an account takeover has; the
deliberate route is `/unlink` first.

**Ownership hangs off the person.** `ChannelGateway` keys runs by `ResolvedIdentity.owner_key`
— the `UserIdentity` when linked, the channel account when not. That is what makes a run
started in ChatyGPT the same run Telegram can see, list and cancel. The unlinked fallback
matters as much: it keeps a stranger working on their own rather than sharing with every
other stranger.

**Linking needs no grant.** It is the one command an unauthorised account may run, because
someone holding a code has not been granted anything yet and refusing them would make the
code unusable.

## Consequences

`/link` and `/unlink` join the closed command set, and `ChannelCommand` gains `link_code`,
which is never logged and never echoed.

Event routing became two lookups instead of one: the run names its owner, the owner names
where they were last reached. That indirection is what lets a run started before a link keep
reporting, and a linked person hear about it wherever they are now.

`ChannelAccessPolicy.for_identity` became `for_owner`. A grant now belongs to a person, so
granting a workspace once covers every surface they use.

The mint endpoint believes its caller when it says which user it is acting for. The service
is loopback-only behind a per-start bearer token, and pretending that is a second
authentication step would be worse than saying it plainly. The belief is bounded rather than
trusted: a client that asks for the wrong user has minted a ten-minute single-use mistake,
not a standing grant. A real per-user story for ChatyGPT is a separate decision.

A directory is optional. Without one every channel account is its own person, which is
correct — just not shared.

## Alternatives rejected

**Matching on username or email.** The reason this ADR exists.

**A long random token instead of a typable code.** Nobody retypes 43 characters out of a
chat window, so it would be pasted — and a pasted secret travels through whatever the person
pastes it into.

**Letting a valid token rebind a linked account.** Convenient, and indistinguishable from a
takeover.

**Reassigning a stranger's existing runs to the person on link.** Linking says who someone
is from now on. Rewriting what an account did before the claim would be asserting a fact
nobody made.
