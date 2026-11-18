# 0029. A record belongs to the set-up whose loading asked for it, and only a set-up that passes drops it

- **Status:** Accepted
- **Accepted:** 2026-11-18
- **Date:** 2026-11-18

## Context

[0021](0021-an-app-declares-itself-on-its-neolithappconfig.md) Decision 10 lets `connect()` run before the host's table exists: it records the identity, and "Set-up checks every recorded identity against the table, after Validate has imported the implementation modules, and an unknown one stops the host (Decision 8)." [0026](0026-set-up-keeps-an-unknown-recorded-identity.md) Decision 1 says what happens to the record afterwards: "The host documents it and checks it at set-up, dropping the identities its table knows, whether that set-up passes or is refused; an unknown one stays, so the next set-up reports it again."

0026 names a gap this leaves (#94). Once a set-up has passed, `override_settings(INSTALLED_APPS=...)` runs set-up again. Say an override adds an app whose module asks for a contract no hosted app provides: that set-up is refused, and the record stays. A second override adds the same app and one that provides the contract. It is refused too, either because the hosted apps changed ([0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 5) or by Resolve. But its table knows the identity, so it drops the record. Entering the first override again then passes with the identity still unknown, because the module that asked is not imported again.

0026 also explains why keeping every record, and checking all of them at every set-up, does not work either. Restoring the first apps after an override that added an app would be refused for that app's records, because the app's modules stay imported after the app is no longer installed. Both failures have one cause: a record is kept by identity alone, without the set-up whose loading asked for it.

## Options

1. **Keep 0026 as it is.** The gap stays: a retry after a refused set-up with other apps can pass with an unknown identity.
2. **Drop a record only when a set-up passes, and check every record at every set-up.** This closes the gap, but it refuses the first apps when they are restored, as 0026 Option 3 says.
3. **Drop a record only when a set-up passes, and tie each record to the app of the module that called `connect()`.** This closes the gap, but the module that calls is not always the one whose app caused the call. A helper in one app, called from another app's models, or a module of one app that another app imports lazily, would be tied to the wrong app. Django's `Apps.get_containing_app_config` also gives a nested app's modules to the outer app when the nested one is not installed.
4. **Drop a record only when a set-up passes, and tie each call site to the installed apps of each set-up whose loading asked from it.** A record is made only by the thread that loads a set-up, or before the first set-up, so the first set-up to see a request is the one whose loading made it. A later set-up checks the call site if it installs every app of one of those set-ups. This closes the gap, and the first apps restored after an override that added apps are not refused for the added apps' records. A `connect()` from another thread while a later set-up loads is not caused by that loading; it is checked at once against the table that thread's calls run on, as it would be a moment before or after.

## Decision

1. **A record belongs to the set-up whose loading asked for it, and only a set-up that passes drops it.** Option 4. This narrows one sentence of 0021 Decision 10, "Set-up checks every recorded identity against the table, after Validate has imported the implementation modules, and an unknown one stops the host (Decision 8)", and one sentence of 0026 Decision 1, "The host documents it and checks it at set-up, dropping the identities its table knows, whether that set-up passes or is refused; an unknown one stays, so the next set-up reports it again."

   In their place: the host documents the recorded set. For each call site of `connect()` it keeps, for each set-up whose loading asked from it, the names of the apps that set-up installs. A `connect()` from a thread other than the one loading a later set-up is not recorded: it is checked at once against the table Django restores when that set-up ends. A set-up checks the call sites tied to apps it all installs, after Validate has imported the implementation modules, and an unknown one stops the host (0021 Decision 8). A set-up that passes drops, at each call site it checked, every set of apps it installs all of; its table knows each of those identities. A refused set-up drops none. An unknown identity therefore stays, and the next set-up that installs the same apps reports it again. The rest of 0021 Decision 10 and of 0026 Decision 1 is unchanged.

## Consequences

- A set-up retried with the same apps reports an unknown identity again, whatever a refused set-up with other apps knew in between. `tests/test_host.py` holds this for the two ways that set-up can be refused, a changed set of hosted apps and Resolve (spec 05, APP-14).
- The first set-up checks every record, as before, since every call site is first seen there. A call site reached through a helper or a lazy import is checked by the set-ups that install the apps whose loading asked from it, and a helper asked from by two set-ups belongs to both.
- The host notes which thread created its app config, which is the thread Django loads the registry in, to tell a set-up's own `connect()` calls from another thread's.
- Restoring the first apps after an override that added apps, a nested app among them, is not refused for the added apps' records. Such a record stays in the set until the process ends or a set-up installs those apps again. The set holds at most one entry per call site and set of installed apps.
- 0021 gains `Narrowed by 0029 (Decision 1)` under its status, and so does 0026; nothing else in either changes (CONTRIBUTING, D2).
