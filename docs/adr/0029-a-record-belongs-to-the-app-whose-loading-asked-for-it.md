# 0029. A record belongs to the app whose loading step asked for it, and only a set-up that passes drops it

- **Status:** Accepted
- **Accepted:** 2026-11-18
- **Date:** 2026-11-18

## Context

[0021](0021-an-app-declares-itself-on-its-neolithappconfig.md) Decision 10 lets `connect()` run before the host's table exists: it records the identity, and "Set-up checks every recorded identity against the table, after Validate has imported the implementation modules, and an unknown one stops the host (Decision 8)." [0026](0026-set-up-keeps-an-unknown-recorded-identity.md) Decision 1 says what happens to the record afterwards: "The host documents it and checks it at set-up, dropping the identities its table knows, whether that set-up passes or is refused; an unknown one stays, so the next set-up reports it again."

0026 names a gap this leaves (#94). Once a set-up has passed, `override_settings(INSTALLED_APPS=...)` runs set-up again. Say an override adds an app whose module asks for a contract no hosted app provides: that set-up is refused, and the record stays. A second override adds the same app and one that provides the contract. It is refused too, either because the hosted apps changed ([0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) Decision 5) or by Resolve. But its table knows the identity, so it drops the record. Entering the first override again then passes with the identity still unknown, because the module that asked is not imported again.

0026 also explains why keeping every record, and checking all of them at every set-up, does not work either. Restoring the first apps after an override that added an app would be refused for that app's records, because the app's modules stay imported after the app is no longer installed. Both failures have one cause: a record is kept by identity alone, without the app whose loading asked for it.

## Options

1. **Keep 0026 as it is.** The gap stays: a retry after a refused set-up with other apps can pass with an unknown identity.
2. **Drop a record only when a set-up passes, and check every record at every set-up.** This closes the gap, but it refuses the first apps when they are restored, as 0026 Option 3 says.
3. **Drop a record only when a set-up passes, and tie each call site to the app of the module that called `connect()`.** The module that calls is not always the one whose app caused the call: a helper in one app called from another app's models, or a module of one app that another imports lazily, is tied to the wrong app. Django's `Apps.get_containing_app_config` also gives a nested app's modules to the outer app when the nested one is not installed.
4. **Drop a record only when a set-up passes, and tie each call site to the installed apps of the set-up whose loading asked from it.** A set-up with fewer apps that still installs the app that asked never checks it, so it passes where a fresh process with the same apps is refused. A load that stops before set-up also leaves its record to whichever set-up comes next.
5. **Drop a record only when a set-up passes, and tie each call site to the app whose loading step asked from it.** That is the app whose implementation Validate imports and builds, or else the app Django's `Apps.populate` is at: the `INSTALLED_APPS` entry it imports, the app whose models it imports, or the app whose `ready()` it runs. A call site asked from while Django imports an entry is tied to that entry until the set-up of that load runs, and from then on to the app the entry made, so a module, a config class, a re-exported or aliased class and a config with another `name` all name the same app. A set-up checks the call sites tied to an app it installs. A helper, a lazy import and a nested app then belong to the app whose loading ran them, and each set-up decides as a fresh process with the same apps would. It reads local variables of `Apps.populate`, the same at Django 5.2 and 6.1, which the CI matrix covers ([0012](0012-supported-python-and-django-versions.md)).

## Decision

1. **A record belongs to the app whose loading step asked for it, and only a set-up that passes drops it.** Option 5. This narrows one sentence of 0021 Decision 10, "Set-up checks every recorded identity against the table, after Validate has imported the implementation modules, and an unknown one stops the host (Decision 8)", and one sentence of 0026 Decision 1, "The host documents it and checks it at set-up, dropping the identities its table knows, whether that set-up passes or is refused; an unknown one stays, so the next set-up reports it again."

   In their place: the host documents the recorded set. For each call site of `connect()` it keeps the apps whose loading step asked from it, or that it was asked from outside any app's loading step. A call site asked from while Django imports an `INSTALLED_APPS` entry is tied to that entry until the set-up of that load runs, and from then on to the app the entry made. A `connect()` from a thread other than the one loading a later set-up is not recorded: it is checked at once against the table Django restores when that set-up ends. A set-up checks the call sites tied to an app it installs or asked from outside any loading step, after Validate has imported the implementation modules, and an unknown one stops the host (0021 Decision 8). A set-up that passes drops, at each call site it checked, the ties it checked; its table knows each of those identities. A refused set-up drops none. An unknown identity therefore stays, and the next set-up that installs its app reports it again. The rest of 0021 Decision 10 and of 0026 Decision 1 is unchanged.

## Consequences

- After any history of overrides, a set-up decides the recorded identities as a fresh process with the same apps would. `tests/test_host.py` walks overrides that add a superset of the asking app, that app alone, a set without it, a load that stops before set-up and none, each after each, and holds the cases of #94, a helper, a lazy import, a helper two apps share, a nested app, another thread, and each form of `INSTALLED_APPS` entry (spec 05, APP-14).
- A module of one app first imported during another app's loading step belongs to that other app's loading, as a lazy import does.
- The host reads which app Django's `Apps.populate` is at from that function's frame, and notes which thread created its own app config, the thread Django loads the registry in. Both rest on Django internals that the CI matrix runs at both ends of 0012.
- Restoring the first apps after an override that added apps, a nested app among them, is not refused for the added apps' records. Such a record stays until the process ends or a set-up installs its app again. The set holds at most one entry per call site and app.
- 0021 gains `Narrowed by 0029 (Decision 1)` under its status, and so does 0026; nothing else in either changes (CONTRIBUTING, D2).
