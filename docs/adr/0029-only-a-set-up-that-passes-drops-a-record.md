# 0029. Only a set-up that passes drops a record, and a set-up checks the records of the apps it installs

- **Status:** Accepted
- **Accepted:** 2026-11-18
- **Date:** 2026-11-18

## Context

[0026](0026-set-up-keeps-an-unknown-recorded-identity.md) Decision 1 says what set-up does with the identities `connect()` recorded before a table existed: "The host documents it and checks it at set-up, dropping the identities its table knows, whether that set-up passes or is refused; an unknown one stays, so the next set-up reports it again."

0026 names a gap this leaves (#94). Once a set-up has passed, `override_settings(INSTALLED_APPS=...)` runs set-up again. Say an override adds an app whose module asks for a contract no hosted app provides: that set-up is refused, and the record stays. A second override adds the same app and one that provides the contract. It is refused too, either because the hosted apps changed ([0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 5) or by Resolve. But its table knows the identity, so it drops the record. Entering the first override again then passes with the identity still unknown, because the module that asked is not imported again.

0026 also explains why keeping every record and checking all of them at every set-up does not work. Restoring the first apps after an override that added an app would be refused for that app's records: the app's module stays imported after the app is no longer installed. Both failures have one cause. A record is kept by identity alone, without the module that asked for it.

## Options

1. **Keep 0026 as it is.** The gap stays: a retry after a refused set-up with other apps can pass with an unknown identity.
2. **Drop records only when a set-up passes, and check every record at every set-up.** This closes the gap, but it refuses the first apps when they are restored, as 0026 Option 3 says.
3. **Drop records only when a set-up passes, and have a set-up check only the records whose module belongs to an app it installs, or to no app ever installed in the process.** A record from an app that an earlier set-up installed and this one does not is left for a set-up that installs that app again. This closes the gap and keeps the first apps admitted on restore. The record holds one more fact per call site, the name of the module that called `connect()`, which only this choice reads.

## Decision

1. **Only a set-up that passes drops a record, and a set-up checks the records of the apps it installs.** Option 3. This narrows one sentence of 0026 Decision 1: "The host documents it and checks it at set-up, dropping the identities its table knows, whether that set-up passes or is refused; an unknown one stays, so the next set-up reports it again."

   In its place: the host documents the recorded set and keeps, for each call site, the module that called `connect()`. A set-up checks the call sites whose module lies in an app it installs, as Django's `Apps.get_containing_app_config` finds it, or in no app installed at any set-up of the process. A set-up that passes drops the call sites it checked, all of which its table knows. A refused set-up drops none. An unknown identity therefore stays, and the next set-up that installs its app reports it again. The rest of 0026 Decision 1 is unchanged.

## Consequences

- A set-up retried with the same apps reports an unknown identity again, whatever a refused set-up with other apps knew in between. `tests/test_host.py` holds this for the two ways that set-up is refused, a changed set of hosted apps and Resolve (spec 05, APP-14).
- Restoring the first apps after an override that added an app is not refused for that app's records. The record stays in the set until the process ends or a set-up installs that app again.
- A call site whose module name is not known, or lies outside every installed app, is checked by every set-up, as before.
- 0026 gains `Narrowed by 0029 (Decision 1)` under its status, and nothing else in it changes (CONTRIBUTING, D2).
