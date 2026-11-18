# 0026. Set-up keeps a recorded identity it does not know, so the next set-up reports it again

- **Status:** Proposed
- **Date:** 2026-11-18

New record, Proposed; the pull request that accepts it adds the Narrowed by line to 0021.

## Context

[0021](0021-an-app-declares-itself-on-its-neolithappconfig.md) Decision 10 lets `connect()` run before the host's table exists: it records the identity, and set-up checks every recorded identity against the table. It says what happens to the record afterwards in one sentence: "The recorded set is host-owned state of the kind K6 permits: the host documents it and empties it at set-up."

A process can run set-up more than once. A test suite does so with `override_settings(INSTALLED_APPS=...)`, and a set-up that a refusal stopped can be tried again in the same process. The module whose `connect()` asked for an unknown identity has already been imported by then, so its `connect()` line does not run again. If the first set-up emptied the record, the retry has nothing left to check, and it passes with the misspelt identity still in the code. The reading that empties the set therefore lets a retry miss what the first attempt reported, which Decision 10 itself is there to prevent ("a misspelt identity in any module imported by the end of set-up stops the host").

The implementation merged in #64 already follows a narrower reading, and this record proposes to write it down.

## Options

1. **Empty the whole set at set-up, as the sentence reads.** Simple, and the set never grows past one set-up. A retry in the same process passes with an unknown identity in place.
2. **Drop only the identities the table knows; keep an unknown one.** A retry reports the unknown identity again. The set keeps entries only while they are wrong, and each set-up rereads them.
3. **Keep every record for the life of the process.** Every later set-up rechecks every identity. Once a set-up has passed, a later one is admitted only for the same hosted apps ([0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 5), so an identity known once stays known, and rechecking it adds work without finding anything.

## Decision

1. **Set-up drops the identities its table knows and keeps an unknown one.** Option 2. This narrows one sentence of 0021 Decision 10: "The recorded set is host-owned state of the kind K6 permits: the host documents it and empties it at set-up."

   In its place: the recorded set is host-owned state of the kind K6 permits. The host documents it and checks it at set-up, dropping the identities its table knows, whether that set-up passes or is refused; an unknown one stays, so the next set-up reports it again. The rest of Decision 10 is unchanged.

## Consequences

- A set-up retried in the same process cannot pass while an identity asked for before an earlier set-up is still unknown. `tests/test_host.py` holds this for a retry under `override_settings` (spec 05, APP-14).
- An unknown identity stays in the set until a set-up's table knows it. Once a set-up has passed, that would take other hosted apps, which 0022 Decision 5 refuses, so in practice it stays until the process ends. The set holds only identities that were found wrong, with their call sites.
- No behaviour changes: this records what #64 built. When this record is accepted, 0021 gains `Narrowed by 0026 (Decision 10)` under its status, and nothing else in it changes (CONTRIBUTING, D2).
