# 0034. Two aliases on one database are refused by name and rule, without their connection settings

- **Status:** Accepted
- **Accepted:** 2026-11-25
- **Date:** 2026-11-25
- **Narrows:** [0009](0009-one-database-alias-per-app.md) (the message of its admitted set's row "Two aliases on one database", Decision 1 here, and one sentence of its Decision 5, Decision 2 here) and [0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md) (the message of `neolith.E018` in its Boot table, Decision 3 here). 0009 gains `Narrowed by 0034 (Decisions 1 and 2)` under its status and 0022 gains `Narrowed by 0034 (Decision 3)` (D2). 0022 also adopts "0009's messages" for `NEOLITH["DATABASES"]` without repeating them, and those follow with no change.

## Context

0009's admitted set refuses two aliases on one database with this message: "`databases <a> and <b>: both name <engine> <name> on <host>:<port>`". The host built it from the values it compared: `ENGINE`, `NAME`, `HOST` and `PORT`, or, on SQLite, the path that `NAME` opens. A `file:` URI gave its path, without the query.

[0033](0033-eight-points-of-the-http-transport-settled-for-0-1.md) Decision 7 (Accepted) writes one rule for every settings refusal: "Where the setting admits data, the refusal never shows the input, in whole or in part: a url, a secret, any value in `OPTIONS`, any value in `DATABASES`." This row is the one place where an Accepted record's message shows such a value (#132). A refusal reaches standard error and CI logs. `HOST` and `NAME` can hold what a deployer keeps out of them, such as an internal host name or a path under a home directory.

The comparison also hashed `HOST` and `PORT` as given. A `HOST` that is a `list` made the builder raise `TypeError` instead of refusing. The test that walks every value under an alias found this.

Every other message was checked for the same class: each other row of 0009's table, 0022's rows for `NEOLITH["DATABASES"]`, `neolith.E005` and `setting_changed`, the rows of 0014 and 0023, and each refusal the builder, `neolith/_databases.py` and Boot build. Two of 0022's Boot rows do not name only an alias, a key or a type:

- `neolith.E018`, `database <alias>: no database at <path>; …`, shows the path a SQLite `NAME` opens, or, for a `file:` URI with no path, the whole `NAME`, its query included. That is the case this record gives as its reason. Decision 3 narrows it.
- `neolith.E012`, `database <alias> cannot be read: <error>`, shows the error the driver raised, within 0022's bound. A server driver's error can name the host or the user it tried, as the same error does under Django's own `migrate`. It is not a value the host prints from `DATABASES`, and it is the one place the deployer learns why the database cannot be read, such as a refused password or an unknown host. It is left as it is; SQLite's errors name no path.

## Options

1. **Name the two aliases and the rule.** The deployer reads the values in their own settings module, under the two aliases named.
2. **Show `ENGINE` alone**, a dotted path like a `BACKEND`. It does not say which database the two aliases share. Decision 7 also counts it among the values in `DATABASES`.
3. **Show a masked form**, such as the last characters of each value. Part of a value is still shown, which Decision 7 rules out.

## Decision

Option 1.

### 1. The refusal names the two aliases and the rule

The message of the row "Two aliases on one database" now reads:

- on SQLite: `databases <a> and <b>: both name one SQLite file; each alias needs a database of its own`;
- on any other backend: `databases <a> and <b>: both name one database, with the same ENGINE, HOST, PORT and NAME; each alias needs a database of its own`.

The keys compared are named. No value of them is shown, in whole or in part. What is compared does not change.

### 2. A `HOST` or `PORT` of another type is not compared

0009 Decision 5 says: "For server backends, `(ENGINE, HOST, PORT, NAME)`, with a `NAME` that is not empty."

**The narrowing.** That sentence now ends: "when `HOST` is a `str` and `PORT` a `str`, an `int` or a `float`, a subclass included, each compared as that type reads it. An alias with another `HOST` or `PORT` is not compared, and its backend meets the value when it connects." Django's backends read a `str` of any subclass as `HOST`, and MySQL and Oracle read `int(PORT)` (`mysql/base.py`, `oracle/utils.py`), so these forms are compared. The value is read through the base type's own method (`str.__str__`, `int.__int__`, `float.__float__`), so no code of a subclass runs, and a value of another type is never hashed, so the builder does not raise. A `PORT` of `3306` and one of `3306.0` compare equal, as `int()` reads them. A project's own backend may read still other forms; those are one more case the best-effort check of Decision 5 does not see.

Alternative weighed: **refuse a `HOST` that is not a `str`, as `NAME` is refused.** It fails earlier. It also narrows "Django's own mapping for `DATABASES[alias]`, passed on unchanged", and a project's own backend may read another form.

### 3. `neolith.E018` names the alias and the rule

0022's Boot table gives `neolith.E018` the message "`database <alias>: no database at <path>; the database is in memory, or nothing has created it yet (the migrate step creates the aliases it migrates)`".

**The narrowing.** The message now reads `database <alias>: no database at its NAME; the database is in memory, or nothing has created it yet (the migrate step creates the aliases it migrates)`. No part of `NAME`, or of the path it opens, is shown. What Boot judges, and when, does not change.

## Consequences

- 0009 gains `Narrowed by 0034 (Decisions 1 and 2)` under its status, and 0022 `Narrowed by 0034 (Decision 3)`. Nothing else in either changes.
- The builder, `neolith.E001` and every other caller of the mapping's check show the new message. A deployer finds the shared database by reading the two aliases the message names.
- A test puts a marker string in every value under an alias, changes one place at a time to values of several types, and finds the marker in no message of the mapping's check, of the builder, or of `neolith.E005`. Another puts it in a SQLite `NAME` in each state Boot refuses, and finds it in no message of `neolith.E007` or `neolith.E018`, which `migrateapps --check` prints too.
- No sentence of the specification states this message, and no requirement ID changes.

Follows principles 6 (*visible failure over transparent calls*) and 10 (*Django-native over framework-new*).
