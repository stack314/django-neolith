# 0026. Set-up keeps a recorded identity it does not know, so the next set-up reports it again

- **Status:** Accepted
- **Accepted:** 2026-11-18
- **Date:** 2026-11-18

## Context

[0021](0021-an-app-declares-itself-on-its-neolithappconfig.md) Decision 10 lets `connect()` run before the host's table exists: it records the identity, and set-up checks every recorded identity against the table. It says what happens to the record afterwards in one sentence: "The recorded set is host-owned state of the kind K6 permits: the host documents it and empties it at set-up."

A process can run set-up more than once: once a set-up has passed, `override_settings(INSTALLED_APPS=...)` runs it again, as a test suite does. (A first set-up that a refusal stopped is not tried again in the same process, since Django's `populate()` is not reentrant.) When such a later set-up is refused for an unknown identity and the same override is entered again, the module whose `connect()` asked for the identity has already been imported, so its `connect()` line does not run again. If the refused set-up had emptied the record, the retry would have nothing left to check, and it would pass with the misspelt identity still in the code. The reading that empties the set therefore lets a retry miss what the first attempt reported, which Decision 10 itself is there to prevent ("a misspelt identity in any module imported by the end of set-up stops the host").

The implementation merged in #64 already follows a narrower reading, and this record writes it down.

## Options

1. **Empty the whole set at set-up, as the sentence reads.** Simple, and the set never grows past one set-up. A retry in the same process passes with an unknown identity in place.
2. **Drop only the identities the table knows; keep an unknown one.** A retry reports the unknown identity again. After a set-up, the set keeps only the identities that set-up did not know, and the next set-up rereads them.
3. **Keep every record for the life of the process.** Every later set-up rechecks every identity. That reports the case #94 tracks, but it also refuses the first apps when they are restored after an override that added an app whose module asked for a contract only that override's apps know: the module stays imported after its app is no longer hosted ([0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 5).

## Decision

1. **Set-up drops the identities its table knows and keeps an unknown one.** Option 2. This narrows one sentence of 0021 Decision 10: "The recorded set is host-owned state of the kind K6 permits: the host documents it and empties it at set-up."

   In its place: the recorded set is host-owned state of the kind K6 permits. The host documents it and checks it at set-up, dropping the identities its table knows, whether that set-up passes or is refused; an unknown one stays, so the next set-up reports it again. The rest of Decision 10 is unchanged.

## Consequences

- A later set-up retried with the same apps reports again an unknown identity that the earlier attempt reported. `tests/test_host.py` holds this for a retry under `override_settings` (spec 05, APP-14).
- Before any set-up, the set holds every identity asked for. After one, it holds only those that set-up did not know, with their call sites.
- **A known gap.** An unknown identity stays until a set-up's table knows it, and that set-up may itself be refused: a later set-up with other hosted apps is refused (0022 Decision 5), but its table can know the identity, and set-up drops it then. Entering the earlier override again can then pass with the identity still unknown. #94 tracks it.
- No behaviour changes: this records what #64 built. 0021 gains a `Narrowed by` line under its status, and nothing else in it changes (CONTRIBUTING, D2).
