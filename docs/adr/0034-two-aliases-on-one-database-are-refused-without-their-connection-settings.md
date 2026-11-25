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
- `neolith.E012`, `database <alias> cannot be read: <error>`, shows the error the driver raised, within 0022's bound. A server driver's error can name the host or the user it tried, as the same error does under Django's own `migrate`. It is not a value the host prints from `DATABASES`, and it is where the deployer learns why the database cannot be read, such as a refused password or an unknown host. It is left as it is; SQLite's errors name no path. The migrate step carries the same driver's error in two messages of its failure report, `app <label>: database <alias>: the migrate step failed: <error>` and `failed: <label>, whose migrations cannot be read: <error>` (also after `partly migrated:`), and they are left as they are for the same reason.

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

### 2. `HOST` and `PORT` are compared as the backends read them

0009 Decision 5 says: "For server backends, `(ENGINE, HOST, PORT, NAME)`, with a `NAME` that is not empty."

**The narrowing.** That sentence now ends: "when `HOST` and `PORT` are each `None`, a `str`, an `int`, a `float`, or a false `bytes`, `bytearray`, `list`, `tuple`, `dict`, `set`, `frozenset`, `complex` or `Decimal`, a subclass included, and `HOST` is not an `int` or a `float` that is true. A `HOST` or `PORT` that is `None` or false is compared as not set, as an absent key is, and so is a `PORT` from which `int()` reads `0`; any other `PORT` as the number `int()` reads from it, or as its type reads it when `int()` reads none; any other `HOST` as a `str`. An alias with any other `HOST` or `PORT` is not compared, and its backend meets the value when it connects."

What Django 5.2 and 6.1 read, and what the check does with each form:

| Form | Django | The check |
|---|---|---|
| Absent | `ConnectionHandler.configure_settings` sets `""` (`db/utils.py`). | Not set. |
| `None`, or a false value of the types the sentence lists (`""`, `0`, `0.0`, `False`, `b""`, `[]`, `{}`, `0j`, `Decimal(0)`) | PostgreSQL and MySQL skip a false `HOST` and `PORT`, and Oracle a false `PORT` (`if settings_dict["HOST"]` and `if settings_dict["PORT"]` in `postgresql/base.py` and `mysql/base.py`, the latter in `oracle/utils.py`), so the driver connects to its default. MySQL calls `HOST.startswith("/")`, which raises for a `HOST` that is not a `str`, and Oracle calls `HOST.strip()` when `PORT` is set. | Not set, all forms equal. |
| A `str` of any subclass | Each backend passes `HOST` on as it is; MySQL and Oracle read `int(PORT)`, and PostgreSQL passes `PORT` to libpq, which reads the same decimal number. MySQL's client takes port `0` as its default. | `HOST` as an exact `str`. `PORT` as `int()` reads it: `"5432"`, `" 5432 "` and `5432` are equal, and `"0"` is not set; a `str` that `int()` refuses is compared as that `str`. |
| An `int` or `float`, a subclass included (`bool` too) | As above: `int(PORT)` on MySQL and Oracle. | `PORT` as `int()` reads it: `3306`, `3306.0` and `3306.5` are equal, and `0.5` is not set; `nan` and the infinities as the `float`. A `HOST` of these types is not set when it is false, and is not compared otherwise. |
| Anything else: a true value of the other types the sentence lists (`b"db"`, `["db"]`, `Decimal(5432)`), or a value of any other type, such as an object of a project's own | The backend or the driver meets it when it connects. | Not compared; it is never hashed, so the builder does not raise. |

A `HOST` or `PORT` is read through the base type's own methods (`str.__str__`, `int.__int__`, `float.__float__`, and `__len__` or `__bool__` for its truth), so no code of a subclass runs. Forms a driver reads as one value beyond these are not seen: a host name's case, an Oracle `HOST` of spaces, one Oracle ignores when `PORT` is not set (`dsn` gives `NAME` alone), or a `PORT` that names the driver's default port (`5432` against an absent `PORT` on PostgreSQL). A project's own backend may read still other forms. Those are cases the best-effort check of Decision 5 does not see.

Alternative weighed: **refuse a `HOST` that is not a `str` or `None`, as `NAME` is refused.** It fails earlier. It also narrows "Django's own mapping for `DATABASES[alias]`, passed on unchanged", and a project's own backend may read another form.

### 3. `neolith.E018` names the alias and the rule

0022's Boot table gives `neolith.E018` the message "`database <alias>: no database at <path>; the database is in memory, or nothing has created it yet (the migrate step creates the aliases it migrates)`".

**The narrowing.** The message now reads `database <alias>: no database at its NAME; the database is in memory, or nothing has created it yet (the migrate step creates the aliases it migrates)`. No part of `NAME`, or of the path it opens, is shown. What Boot judges, and when, does not change.

## Consequences

- 0009 gains `Narrowed by 0034 (Decisions 1 and 2)` under its status, and 0022 `Narrowed by 0034 (Decision 3)`. Nothing else in either changes.
- The builder, `neolith.E001` and every other caller of the mapping's check show the new message. A deployer finds the shared database by reading the two aliases the message names.
- A test puts a marker string in every value under an alias, changes one place at a time to values of several types, and finds the marker in no message of the mapping's check, of the builder, or of `neolith.E005`. Another puts it in a SQLite `NAME` in each state Boot refuses, and finds it in no message of `neolith.E007` or `neolith.E018`, which `migrateapps --check` prints too.
- The conformance case `lifecycle_a_database_with_no_schema_stops_boot` said the diagnostic names "the file's path". It now says the diagnostic names the alias and says there is no database at its `NAME`, showing no part of the path. No requirement ID changes.

Follows principles 6 (*visible failure over transparent calls*) and 10 (*Django-native over framework-new*).
