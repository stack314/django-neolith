# 0026. Set-up keeps a recorded identity it does not know, so the next set-up reports it again

- **Status:** Accepted
- **Accepted:** 2026-11-18
- **Date:** 2026-11-18
- Corrected before release: Decision 1 (#94), 2026-11-18

## Context

[0021](0021-an-app-declares-itself-on-its-neolithappconfig.md) Decision 10 lets `connect()` run before the host's table exists: it records the identity, and set-up checks every recorded identity against the table. It says what happens to the record afterwards in one sentence: "The recorded set is host-owned state of the kind K6 permits: the host documents it and empties it at set-up."

A process can run set-up more than once: once a set-up has passed, `override_settings(INSTALLED_APPS=...)` runs it again, as a test suite does. (A first set-up that a refusal stopped is not tried again in the same process, since Django's `populate()` is not reentrant.) When such a later set-up is refused for an unknown identity and the same override is entered again, the module whose `connect()` asked for the identity has already been imported, so its `connect()` line does not run again. If the refused set-up had emptied the record, the retry would have nothing left to check, and it would pass with the misspelt identity still in the code. The reading that empties the set therefore lets a retry miss what the first attempt reported, which Decision 10 itself is there to prevent ("a misspelt identity in any module imported by the end of set-up stops the host").

The implementation merged in #64 already follows a narrower reading, and this record writes it down. That reading still dropped a record when a set-up that was then refused knew the identity (#94), so Decision 1 was corrected before release to the rule below.

## Options

1. **Empty the whole set at set-up, as the sentence reads.** Simple, and the set never grows past one set-up. A retry in the same process passes with an unknown identity in place.
2. **Drop only the identities the table knows; keep an unknown one.** A retry reports the unknown identity again. After a set-up, the set keeps only the identities that set-up did not know, and the next set-up rereads them.
3. **Keep every record for the life of the process.** Every later set-up rechecks every identity. That reports the case #94 tracks, but it also refuses the first apps when they are restored after an override that added an app whose module asked for a contract only that override's apps know: the module stays imported after its app is no longer hosted ([0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 5).
4. **Keep a record until a set-up passes, and check only the records of the apps a set-up installs, found from the module that called `connect()`.** This closes #94, but the module that calls is not always the one whose app caused the call: a helper in one app called from another app's models, or a module of one app that another imports lazily, is tied to the wrong app. Django's `Apps.get_containing_app_config` also gives a nested app's modules to the outer app when the nested one is not installed.
5. **Keep a record until a set-up passes, and tie each call site to the installed apps of the set-up whose loading asked for it.** A record is made only while a set-up loads, or before the first one, so the first set-up to see a call site is the one whose loading asked for it. A later set-up checks the call site if it installs every one of those apps. This closes #94, and the first apps restored after an override that added apps are not refused for the added apps' records.

## Decision

1. **A record is kept until a set-up passes, and belongs to the set-up whose loading asked for it.** Option 5. This narrows two sentences of 0021 Decision 10: "Set-up checks every recorded identity against the table, after Validate has imported the implementation modules, and an unknown one stops the host (Decision 8)." and "The recorded set is host-owned state of the kind K6 permits: the host documents it and empties it at set-up."

   In their place: the recorded set is host-owned state of the kind K6 permits, and the host documents it. For each call site of `connect()` it keeps the names of the apps installed at the first set-up that sees that call site; a call site asked for again starts afresh. A set-up checks the call sites tied to apps it all installs, after Validate has imported the implementation modules, and an unknown one stops the host (Decision 8). A set-up that passes drops the call sites it checked, all of which its table knows; a refused set-up drops none. An unknown identity therefore stays, and the next set-up that installs the same apps reports it again. The rest of Decision 10 is unchanged.

## Consequences

- A later set-up retried with the same apps reports again an unknown identity that the earlier attempt reported, whatever a refused set-up with other apps knew in between. `tests/test_host.py` holds this for a plain retry and for a refused set-up between, refused as a changed set of hosted apps or by Resolve (spec 05, APP-14).
- The first set-up checks every record, since every call site is first seen there. A call site reached through a helper or a lazy import is checked by the set-ups that install the apps whose loading asked for it.
- Restoring the first apps after an override that added apps, a nested app among them, is not refused for the added apps' records. Such a record stays until the process ends or a set-up installs those apps again; the set holds at most one entry per call site.
- 0021 gains a `Narrowed by` line under its status, and nothing else in it changes (CONTRIBUTING, D2).
