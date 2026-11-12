# 0023. A request's principal comes from ordered backends that declare their request inputs, and a Django user is named by a deployer-given issuer and its primary key

- **Status:** Accepted
- **Accepted:** 2026-11-12
- **Date:** 2026-11-12

## Context

[0010](0010-pluggable-principal-resolution.md) puts principal resolution behind a seam of pluggable backends. It fixes what a principal is (an issuer and a subject, both strings and stable across hosts, plus optional claims), how backends combine (in order; the first that recognizes the credentials decides), and that the Django user is resolved "through the same seam, into the same portable value". Its Consequences leave three things for this step:

- backends "declare which inputs of a request they read", so that the host can vary responses on them;
- a project that resolves Django users "needs an identifier for each user that is stable across hosts, which the primary key alone is not";
- the form of the interface itself.

[0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 4 fixes when the host asks: once per request root, at the latest by the root's first connector call, with concurrent first calls sharing the result. A request that makes no call causes no resolution. A failure to resolve is recorded once, and every call from that root then fails with `internal`. A response that the principal may shape varies on the inputs the backend reads.

This record is for PR 9 of roadmap step 4 (#11). It relies on the app host record, [0022](0022-the-app-host-builds-settings-and-checks-before-it-serves.md), for the `NEOLITH` setting, the builder, set-up and the check series.

Step 4 has no network transport, so this record covers one of the seam's two jobs: resolving the principal of an ordinary Django request. Authenticating the calling app of an incoming contract call is step 5 (#12).

Forces:

- **One form per input.** The step's design gate asks for the admitted set first: each setting, attribute and return value in one form, and everything else refused with a message that names it (K2, APP-1).
- **A wrong principal is worse than none.** A principal carries the end user's authority to every app down the call chain (0017 Decision 3). Where the host cannot tell, it should fail the call.
- **No cost that the app does not use** (0017 Decision 4, Cost).
- **Small core** (principle 12) and **Django-native** (principle 10).

What Django does, read at 5.2.19 and 6.1.3. Paths are relative to the `django` package. A trial on both versions confirmed the points marked *(trial)*.

1. `AuthenticationMiddleware.process_request` sets `request.user` to a `SimpleLazyObject` and `request.auser` to a function with its own cache, `request._acached_user` (`contrib/auth/middleware.py`, lines 24 to 41 in 5.2.19, 23 to 40 in 6.1.3). Neither touches the session until used *(trial)*.
2. `login`, `alogin`, `logout` and `alogout` replace `request.user` with a plain instance: the user, or an `AnonymousUser` *(trial)*. In 5.2.19 they assign `request.user` only (`contrib/auth/__init__.py`, lines 195, 238, 258 and 275), so after `alogout` a second `await request.auser()` still returns the user who logged out *(trial)*. In 6.1.3 `_set_auth_user` replaces both (lines 92 to 105), and `auser()` returns the anonymous user *(trial)*.
3. `get_user` reads the session, and can change it: `flush()` when the stored hash does not verify, `cycle_key()` when it verifies against a fallback key, which happens for every logged-in user while `SECRET_KEY_FALLBACKS` is in use (`contrib/auth/__init__.py`, lines 330 and 333 in 5.2.19, 313 and 316 in 6.1.3). `flush()` deletes the stored session at once. `cycle_key()` saves the session's data as it stands under a new key and deletes the old key (`contrib/sessions/backends/base.py`, lines 417 to 440 in 6.1.3; `db.py`, `create`) *(trial: the old row is gone, and the new row holds the user's id and the old hash)*. The updated hash and the new cookie are written only by `SessionMiddleware`'s response phase, which also adds `Vary: Cookie` whenever the session was accessed (`contrib/sessions/middleware.py`, lines 29, 47 and 81 in both).
4. `ModelBackend.get_user` returns nothing for a user whose `is_active` is false (`contrib/auth/backends.py`, lines 230 to 235 in 5.2.19, 235 to 240 in 6.1.3), so `request.user` is then anonymous. `AllowAllUsersModelBackend` keeps such users. Whether an inactive user stays logged in is the project's choice of authentication backend.
5. The session stores the user's primary key through the key field's `value_to_string`, and reads it back with `to_python` (`contrib/auth/__init__.py`, lines 191 and 95 in 5.2.19, 193 and 111 in 6.1.3). Nothing checks that the object a custom authentication backend returns is an instance of the user model.
6. `patch_vary_headers` compares names without regard to case, keeps a name already present as written, and collapses the header to `*` when any name is `*` (`utils/cache.py`, line 299 in 5.2.19, 303 in 6.1.3). Patching `("authorization", "cookie")` onto `Vary: Cookie` gives `Cookie, authorization` *(trial)*.
7. `AutoField`, `BigAutoField` and `SmallAutoField` are `IntegerField` subclasses (`db/models/fields/__init__.py`). On SQLite they are created with `AUTOINCREMENT` (`db/backends/sqlite3/base.py`, `data_types_suffix`), so a deleted user's key is not given to a later user. A plain `IntegerField` key has no such suffix, and SQLite then gives the highest key again after its row is deleted *(trial: key 3 is reused without `AUTOINCREMENT`, and 4 is given with it)*. On the other supported databases an auto field is an identity column or a sequence (PostgreSQL, Oracle) or an auto-increment counter (MySQL, MariaDB), which their documentation describes as not giving a value twice in ordinary use; this was read, not trialled, and a reset of the sequence or counter is in the deployer's hands everywhere. A user model that inherits from another model has a `OneToOneField` as its key. A model with a `CompositePrimaryKey` has a tuple as `pk` (`db/models/fields/composite.py`; `db/models/base.py`, `_get_pk_val`).
8. Django's own pluggable backends give three precedents for form. `AUTHENTICATION_BACKENDS` is a list of dotted paths with no options, and `load_backend` constructs each backend again on every use (`contrib/auth/__init__.py`, `_get_backends`). `STORAGES` entries are dicts with `BACKEND` and an optional `OPTIONS`, keyed by alias, built once per process and reset on `setting_changed` (`core/files/storage/handler.py`; `test/signals.py`). `TEMPLATES` is an ordered list of dicts with `BACKEND`, `OPTIONS` and a `NAME` that defaults from the path and has to be unique (`template/utils.py`, `EngineHandler.templates`). All three are lists or dicts of lists in `conf/global_settings.py`. `BaseBackend.aauthenticate` defaults to running `authenticate` through `sync_to_async` (`contrib/auth/backends.py`, lines 14 and 15 in 5.2.19, 19 and 20 in 6.1.3).
9. A middleware that supports both modes does not always run in the server's mode. `load_middleware` runs it in sync mode under ASGI when the handler inside it is sync, for example when a sync-only middleware follows it in the list (`core/handlers/base.py`, lines 50 to 53 in 6.1.3).
10. Django's cache middleware keys a response on its `Vary` header as it stands when `UpdateCacheMiddleware`'s response phase runs (`middleware/cache.py`; `utils/cache.py`, `learn_cache_key`). It does not store a streaming response, a response with `Vary: *`, or one marked `private`, and it adds `Vary: Authorization` itself when the request bears that header. Django's documentation asks for `UpdateCacheMiddleware` before any middleware that adds to `Vary`. `cache_page` stores at the view, before any middleware's response phase.
11. Django's own check for `AuthenticationMiddleware`, `admin.E408`, admits the class or a subclass (`contrib/admin/checks.py`, `_contains_subclass`).

Outside Django: Django REST framework's `Request.user` setter also assigns the user to the underlying `HttpRequest` (`rest_framework/request.py`), so inside a DRF view the Django request carries a plain user that may have come from `Authorization`. A trial with asgiref 3.13.0 showed that sync code on the thread-sensitive thread, waiting for an asynchronous resolution whose `sync_to_async` step needs that same thread, waits until it times out.

## Options

1. **No interface in step 4: the host reads `request.user` itself.** Less to build. Django's user model would then sit inside the host, where 0010 Decision 8 puts it behind the seam.
2. **Ordered backends that declare their request inputs, with a Django-user backend whose subject is a dedicated, stable field.** It fits 0010 as written. `django.contrib.auth`'s own user model has no such field, so the common case needs a custom user model before its first call.
3. **The same backends, with the user's primary key as the subject under an issuer the deployer gives.** The common case works as it is. It needs 0010 Decision 2 narrowed.

## Decision

Option 3, written as two parts that could be accepted apart. Decisions 1 to 10 are the seam and its Django-user backend. Decision 11 is the narrowing of 0010 that Decision 9 needs, made as a decision of its own. Under option 2, Decision 9 is replaced, together with the admitted set's `key field` and `subject` lines and Decision 10's two rows on the key; Decisions 1 to 8 and the rest of 10 stand.

### The admitted set

This is everything the host admits. Anything else is refused (Decision 10).

```
; the setting
PRINCIPAL_BACKENDS = list of 1 or more entry       ; a key of NEOLITH; absent means no backend
entry              = dict with exactly the keys "BACKEND" and "OPTIONS"
BACKEND            = str, the dotted path of a concrete subclass of PrincipalBackend
OPTIONS            = dict whose keys are str and Python identifiers; passed as keyword arguments

; the middleware, in every host
MIDDLEWARE         lists "neolith.middleware.PrincipalMiddleware" exactly once (position: Decision 6)

; a backend
request_inputs     = tuple of header-name, no name twice   ; may be empty; required, no default
                   / ("*",)                                 ; reads something outside the URL and the headers
header-name        = lower *(lower / DIGIT / "-")           ; "segment" of spec 01 with "-" for "_"
resolve(request)   -> exactly a principal value (0010 Decision 2), or None
aresolve(request)  -> the same, awaited

; the Django-user backend
OPTIONS            = {"issuer": issuer}
issuer             = 1*255 VCHAR                            ; %x21-7E: visible ASCII, no space
key field          = AutoField / BigAutoField / SmallAutoField / UUIDField   ; or a subclass of one
subject            = "0" / ["-"] %x31-39 *DIGIT             ; an int key, as Python's str() writes it
                   / 8HEX "-" 4HEX "-" 4HEX "-" 4HEX "-" 12HEX   ; a UUID key, HEX = DIGIT / "a"-"f"
claims             = none
request_inputs     = ("authorization", "cookie")
```

### 1. This record covers the request side of the seam

A *principal backend*, in this record, answers one question: on whose behalf does this Django request act? The host asks it only for a request root (0017 Decision 4). It is not asked for:

- a command scope or a test scope, whose principal is given when the scope is opened;
- a call that arrives over a transport, whose principal the calling app asserts (0010 Decision 3). Step 5 adds the part of the seam that authenticates the calling app, with its own inputs and its own record.

Alternatives weighed:

- **One interface for both jobs now**, with a method for requests and a method for incoming calls. It would match 0010's single word "backend" most closely. The second method would have no caller, no test and no transport to shape it until step 5, and its form would be fixed by guesswork.
- **No interface in step 4: the host reads `request.user` itself**, and the seam arrives with step 5. Less to build. It would put Django's user model inside the host, where 0010 Decision 8 puts it behind the seam, and a project with another source of identity would have nothing to plug into.

The cost: "backend" names two jobs across two steps. Step 5's record has to say whether one class may do both, and under which setting (Open question 1).

### 2. A backend is a subclass of `PrincipalBackend` with `resolve` and `aresolve`

`PrincipalBackend` is an abstract base class. A backend:

- is constructed once per set-up of the host (0022 Decision 5), which in a serving process is once, with its `OPTIONS` as keyword arguments, and serves every request from every thread. It keeps no per-request state on itself;
- defines `resolve(self, request)`, a plain function. The host calls it when the root's first connector call is synchronous, and from the principal middleware when Django runs that middleware in sync mode;
- inherits `aresolve(self, request)`, which runs `resolve` through `sync_to_async`, as Django's `BaseBackend.aauthenticate` does. A backend with its own async path overrides it. The host awaits it when the first connector call is asynchronous, and from the principal middleware when Django runs it in async mode. The mode is Django's choice per middleware, not the server's (Context, point 9);
- sets `request_inputs` (Decision 3). The base class gives it no value;
- receives the `HttpRequest` and nothing else;
- returns the principal value, or `None` to abstain;
- raises to refuse (Decision 5).

`sync_to_async` is imported from asgiref, which Django requires and whose use Django's async documentation asks of projects. No runtime dependency is added (K5).

**One instance, not one per use.** Django's authentication backends and DRF's authentication classes are constructed with no arguments each time they are used. Here a backend has options, and K2 asks that they are checked before the host serves; the union of `request_inputs` (Decision 6) is also fixed then. So the instance is built once, as Django's storage and template handlers are. Unlike those, it is not rebuilt on `setting_changed`. 0022 refuses a change to a key of `NEOLITH` after set-up unless the key's record says it may change, and this key may not: an `override_settings` that changes `PRINCIPAL_BACKENDS` raises, by that rule. A backend's author therefore writes for shared use across threads, which Django's own backends do not have to.

**Four functions without host state.** Reading the setting's shape (in the builder), building backends from checked entries, running the search of Decision 5 over a list of backends and a request, and the response phase of Decision 6 (decide whether to search, mark the root, and patch `Vary` with a union it is given) are plain functions of their arguments. One backend's constructor is not: the Django-user backend reads Django's settings and the user model when it is built, so its key-field rule is a function of a model class, which a test can hand any model. The principal middleware is the wiring that hands them the host's backends and union. A test that needs a backend list of its own calls these functions with built inputs. The one test host has one backend list for the whole process, and only the cases that need Django's middleware chain run on it; Consequences says which test runs where. So no test needs a second set-up in a process (the design gate's "host set up twice") or `override_settings(NEOLITH=...)`.

Alternatives weighed:

- **A structural protocol**, checked by the presence of the methods. Any object could then be a backend without importing neolith. The host could not supply the `aresolve` default, and "is this a backend?" would have two answers: a duck-typed check at startup, and whatever happens on the first call.
- **One method only, sync.** Smaller. In async mode every resolution would cross to a thread, and a backend that awaits an identity provider would block that thread.
- **One method only, async.** A sync view on WSGI would start an event loop to read a session.
- **Passing a narrowed view of the request**, holding only the declared inputs. The declaration of Decision 3 would then be enforced, not trusted; Encore's auth handler does this, taking a struct whose fields are tagged `header`, `query` or `cookie`. The Django-user backend needs `request.user`, which is the product of two other middleware and the session store; it cannot be rebuilt from a header.
- **A third return value for "mine, and there is no principal"**, which stops the search without a failure, as ASP.NET Core's `AuthenticateResult` separates `NoResult` from `Fail`. Without it, a backend that recognizes a bad credential either abstains, and a later backend may answer, or raises, and the root's calls fail. Raising is the fail-closed choice, and it is what 0010 Decision 6 says of a recognizing backend: "it resolves them, or it refuses the call". A third value can be added later without changing existing backends.
- **A `check()` method on the backend**, as Django's fields and admin classes have, for a backend's own startup refusals. A third member for every backend. A constructor that raises does the same job (Decision 10).

The cost: a backend's author who forgets a `return` has written an abstention. The root then has no principal, and an action that needs one refuses with `permission_denied`. That is the safe direction, but it is silent.

### 3. A backend declares its request inputs as lower-case header names

`request_inputs` is an attribute of the backend instance: a tuple of HTTP request header names in lower case, such as `("cookie",)` or `("authorization",)`. It is required. The base class supplies no default, because a default of `()` would be an under-declaration nobody wrote, which is the unsafe direction. The host reads it once, after construction, and keeps its own copy.

- The request's method and URL, path and query string included, need no declaration: every cache keys on them already (RFC 9111, section 4.1; Django's `_generate_cache_key`). A backend that reads a key from the query string and nothing else declares `()`.
- A backend whose answer depends on something outside both the URL and the headers, such as the client's address, a TLS client certificate or the request body, declares `("*",)`.
- A backend whose answer does not depend on the request declares `()`.

Header names are the unit because `Vary` is the only use (0017 Decision 4), and `Vary` lists request header names (RFC 9110, section 12.5.5). A cookie is the `cookie` header. Lower case is one checkable spelling, and it is how HTTP/2 and HTTP/3 write field names. Django's `patch_vary_headers` treats `cookie` and `Cookie` as one name, so a response that `SessionMiddleware` already varied on `Cookie` is not changed. It is a tuple and not a list because it is written in code as a constant of the class, where the setting of Decision 4 is written by a deployer in Django's own style.

For `authorization`, a shared cache already has a rule of its own: it does not store a response to a request with that header unless the response allows it (RFC 9111, section 3.5), and Django's cache middleware adds `Vary: Authorization` itself. The declaration is still made, so that the host's behaviour does not rest on which cache is in front of it.

Alternatives weighed:

- **A structured declaration**, such as `{"cookies": (...), "headers": (...), "query": (...)}`, close to Encore's tagged struct. It reads well, and a cache key could one day be narrowed to one cookie. `Vary` cannot express one cookie, and a query parameter needs no `Vary`, so the extra detail would have no effect today.
- **Any case, normalized by the host.** Friendlier, and two spellings of one input.
- **A method that takes the request and returns the inputs it read.** Exact per request. The host could then not compute the set at startup, and a backend could get it wrong on one path only.
- **No `*`.** Smaller, and step 4 ships no backend that uses it. A backend that reads the client address would then have no truthful declaration, and `()` would let a shared cache serve one user's page to another. It stays, with a test-only backend to exercise it.

The cost: the host trusts the declaration. A backend that reads a header it did not declare gives responses a shared cache may serve to the wrong user. The documentation has to say so, and the test harness should offer a check that resolves twice with every undeclared header changed and compares (Open question 6).

### 4. Backends are configured in `NEOLITH["PRINCIPAL_BACKENDS"]`, and the default is none

The value is a list of one or more dicts, each with exactly `BACKEND`, a dotted path, and `OPTIONS`, a dict. The order of the list is the order of Decision 5. The key is absent when the host has no backend: the host then resolves nothing, every request root has no principal, and no response gains a `Vary` from the host. An empty list is refused, with a message that says to leave the key out, so that "no backend" has one spelling.

A list, because Django's own settings of this shape are lists (Context, point 8) and 0022 writes `NEOLITH["UNDECLARED_APPS"]` and `MIDDLEWARE` as lists. A deployer who copies Django's form is then not refused.

The builder checks the shape of the value and imports nothing, because a backend's module may import models, which Django refuses before the app registry is ready. Set-up imports each path with `import_string` and constructs each backend, after app loading (Decision 10). 0022 owns the `NEOLITH` setting and refuses its unknown keys; this record defines this one key.

The same class may be listed more than once, for example for two identity providers with different options. The host identifies an entry by its position and path, in refusals and in logs.

Alternatives weighed:

- **A list of dotted paths**, as `AUTHENTICATION_BACKENDS`. Options would then need a second setting per backend, such as the issuer of Decision 8, and a misspelt setting name would be an unread global.
- **`OPTIONS` optional**, as in `STORAGES`. Shorter. An omitted key and `{}` would be two spellings of one thing.
- **A `NAME` per entry, unique**, as `TEMPLATES` has, and as Dapr names each component of its middleware pipeline. A name is a handle that survives reordering, in logs and in refusals. As an optional key it would be a second form; as a required one, every entry carries a third key for a use step 4 does not have yet. Position and path serve until a second shipped backend shows the need (Open question 7).
- **No class twice.** The rule would protect against nothing harmful, it would push two instances of one class into a subclass, and comparing paths would miss one class under two import paths.
- **Discovery by packaging entry points.** Nothing to list. An installed package would then join the order without the deployer naming it, and for an authentication seam the order and the activation belong in the deployer's settings, where Django puts every pluggable backend.
- **Backend instances in the setting.** Typed, and no dotted paths. Settings are read before the app registry exists, and a backend module that imports a model at that point is refused by Django. A dotted path also survives whatever form the deployer's input takes.
- **The Django-user backend by default when `django.contrib.auth` is installed.** Nothing to configure for the common case. It has no safe default issuer (Decision 8), and a host would vary and read sessions without anyone having asked.
- **Exactly one backend per host in 0.1.** The smallest set, and step 4 ships only one backend. 0010 Decision 6, which is Accepted, already fixes that several combine in order, so the order is tested here with test-only backends instead of being left for the first project that lists two.

The cost: an entry always carries `"OPTIONS": {}`, even when it has none. Two conventions meet in one setting: `OPTIONS` is always written, and the key itself is left out when empty, which is 0022's rule for every key of `NEOLITH`.

### 5. The first backend to return a principal decides, and one that raises ends the search

For one request root, the host calls the backends in order:

- a backend that returns a principal decides. No later backend is called;
- a backend that returns `None` abstains, and the next is called;
- when every backend abstains, or none is configured, the root has no principal. This is the anonymous request of 0017 Decision 1;
- a backend that raises an `Exception`, or returns anything else, ends the search. The failure is recorded on the root, and every contract call from it fails with `internal` at the caller's step 1 (0017 Decisions 4 and 9). The host logs the cause with the trace identifier. A principal that is not valid in 0017's sense is handled the same way, by the check 0017 already defines.

This is 0010 Decision 6 applied to requests, with one difference that follows from the setting. On an incoming contract call, credentials that no backend recognizes fail with `unauthenticated`. On an ordinary request, nothing recognized is a visitor who is not logged in.

**How concurrent first calls share.** The root holds one outcome, written once, behind a lock. The rule is that no thread ever blocks waiting for another caller's search:

- a synchronous caller that finds no outcome runs the search itself, with `resolve`, on its own thread;
- asynchronous callers on one event loop share one in-flight asynchronous search: the first runs it, with `aresolve`, in its own task, and the others await it. A caller on another loop, such as a thread of its own inside `async_to_sync`, cannot await that task, so it runs its own search, as a synchronous caller does;
- the first search to complete, with a principal, with none or with a failure, writes the outcome. A search that completes later discards its own result and uses the recorded one.

So every call from one root sees one principal (0017 Decision 4), and a backend may be called more than once for one request; it has to give the same answer each time. Letting a synchronous and an asynchronous first call wait on one search was tried. The trial in Context showed that a sync caller on the thread-sensitive thread then waits for a resolution that needs that thread, and nothing ends the wait.

**Cancellation.** A search cut short by an exception that is not an `Exception`, such as a cancelled task, records nothing. Callers awaiting that search are not cancelled with it: one of them runs the search again from the first backend, and the rest await that one. A later first call does the same.

**The latest point.** The search does not start after the principal middleware's response phase has run for the root (Decision 6). A first call made later, from the response phase of a middleware placed outside the principal middleware, is recorded as a failure to resolve and fails with `internal`. This adds one cause to the failure to resolve that 0017 Decisions 4 and 9 already define, with the outcome they fix; 0017's text is not edited. The log names the cause and says that such code opens its own scope, as 0017 Decision 4 already asks of middleware that calls on its own app's account.

Alternatives weighed:

- **Skip a backend that raises and try the next**, as Spring Security's `ProviderManager` does after a failure. A request would more often get some principal. A weaker backend could then answer for a request whose stronger credential was just found bad, which is the case 0010 Decision 6 sets out to avoid.
- **Treat a raise as "no principal".** The call would go on as anonymous. A view that showed a logged-in page would then call on nobody's behalf, and an action open to anonymous callers would run for the wrong audience.
- **Call every backend and require agreement.** Strict, and it would make every request pay for every backend.
- **One shared search for all callers, sync and async.** At most one backend call per request. It needs a wait across the two kinds, which is the deadlock above, or a time limit, which this record does not have (Open question 3).
- **Every caller runs its own search.** Simplest to state. A fan-out of async calls would then read the session once per call.
- **Let a late first call resolve.** Nothing new to refuse. For the session it can log the user out (Consequences), and the `Vary` patch would have to move outward past the deployer's cache middleware (Decision 6).
- **A usage error for a late first call**, instead of `internal`. The message would reach the developer directly. Whether the call is late depends on whether the view called first, so it is not a property of the calling code alone, and 0017 already gives a failure to resolve one fixed outcome.

The cost: a bad credential and a bug in a backend look the same to the caller, `internal`, and differ only in the host's log (Open question 4). A view that wants to answer a bad credential with a 401 checks it before calling a contract, as it does today. Middleware outside the principal middleware cannot make a root's first call from its response phase.

### 6. The principal middleware bounds resolution and patches `Vary`

The host's second middleware is `neolith.middleware.PrincipalMiddleware`. It is sync- and async-capable, and it is listed exactly once in every host, with or without backends, so that `MIDDLEWARE` has one shape and adding the first backend is a change to one setting. With no backend configured it does nothing per request.

**Position.** The root middleware is first (0022 Decision 6), so the principal middleware is always inside it. Beyond that:

- it sits after every middleware whose work a backend reads or whose response phase has to see what a backend did. The host can check this only for the backend it ships: with the Django-user backend, `SessionMiddleware` and `AuthenticationMiddleware`, each matched as the class or a subclass, as `admin.E408` matches, are listed before it;
- with any backend configured, Django's `UpdateCacheMiddleware`, or a subclass, is not listed after it, so the cache stores a response only after the patch below. Django's own ordering rule for that middleware asks the same of every middleware that adds to `Vary`.

**Where each rule is checked.** That the class is listed, and listed once, is checked in the three places 0022 Decision 6 gives the root middleware: by the builder, at set-up, and by the middleware's own constructor on the list its handler was built from. The cache rule is the host's: it is a check at set-up, and the constructor runs it again. The rule for `SessionMiddleware` and `AuthenticationMiddleware` is the Django-user backend's, checked when that backend is built at set-up (Decision 10). A `MIDDLEWARE` changed under `override_settings` after set-up is therefore seen for the host's rules and not for a backend's own.

**In its response phase**, for a bound root in a host with a backend. Step 1 is a search like any caller's and follows Decision 5's rules, so it runs outside the root's lock; steps 2 and 3 read and mark the root under the lock, and the header is patched after it is released:

1. when the response is a streamed one whose body the root middleware keeps inside the root (so not a `FileResponse` with `file_to_stream`, which runs no app code), and the root has no outcome yet, it runs the search now (0017 Decision 4), with `resolve` in sync mode and `aresolve` in async mode;
2. it marks the root, and no search starts for it afterwards (Decision 5);
3. when a search has started for the root, whatever its outcome and whether or not it has completed, it patches the response's `Vary` with the union of the `request_inputs` of all configured backends. With `*` in the union the header becomes `Vary: *`. When no search started, it adds nothing.

Because of step 2 there is one place where `Vary` is patched, and it is inside the deployer's cache middleware and inside `SessionMiddleware`, whose response phase then saves whatever `get_user` changed in the session. The [Django host notes](../django-host.md) on `main` patch in the root middleware, which is outermost. That suits a host with no cache middleware; with `UpdateCacheMiddleware` in the list, that middleware would key and store the response before the patch. PR 9 moves the patch here.

The union is fixed at set-up. It is a superset of what one request needed: when the first backend decides, the inputs of the second were not read. A cache keyed on more headers than needed stores more entries.

**What the host cannot keep.** A cache between the calling code and this middleware stores before the patch. `cache_page` is one: it stores at the view. Plain Django has the same exposure for a cached view that reads `request.user`, and gives `vary_on_cookie` and `vary_on_headers` for it. The documentation has to say that a view under `cache_page` that makes a contract call adds `vary_on_headers` for the declared inputs, or is not cached.

Alternatives weighed:

- **Required only when a backend is configured**, and refused otherwise. No idle class in a host without backends. The rule for `MIDDLEWARE` would then depend on a second setting.
- **Only the inputs of the backends that were called.** Tighter keys. Correct too, but the set changes per response, and a test has to cover each prefix of the list.
- **Vary whenever a backend is configured**, call or no call. Simplest. It would end caching of every anonymous page on a host with session users, against 0017's cost rule.
- **Leave `Vary` to the backend.** `SessionMiddleware` already does this for cookies. A backend has no response to patch when it runs inside a connector call.
- **`Cache-Control: private` as well**, when a principal was resolved or the union holds `*`. Django's documentation recommends it for per-user pages beside `vary_on_cookie`, and Spring Security goes further and marks every response `no-store` by default. It would also protect against a wrong declaration. It overrides a deployer's explicit `public` (Django drops `public` when `private` is set), it ends shared caching of pages that `Vary` already keys per user, and 0017 Decision 4 fixes `Vary` and nothing more. `Vary` alone is chosen for 0.1, and the directive is Open question 8.

The cost: on a host with a cookie backend and a header backend, every page that makes a call varies on both. The host states the middleware order for its own backend only; the author of another backend states, in that backend's documentation, which middleware it has to follow.

### 7. The Django-user backend reads `request.user`, and tells a replaced user by its type

`DjangoUserBackend` resolves the user that `AuthenticationMiddleware` provides:

1. A request with no `user` attribute is refused (it raises).
2. When `request.user` is not a `SimpleLazyObject`, the backend uses it as it is. `login`, `logout`, `alogin` and `alogout` leave a plain instance there, in both Django versions, so this path needs no I/O and is safe in the event loop.
3. Otherwise `resolve` uses `request.user`, and `aresolve` uses `await request.auser()`.
4. A user whose `is_authenticated` is not `True` gives `None`: the backend abstains.
5. Any other user gives a principal with the issuer of Decision 8, the subject of Decision 9 and no claims.

Step 2 is what keeps a view that calls `alogout` and then a contract from calling as the user who just logged out on Django 5.2, where `auser()` still returns that user (Context, point 2). The host notes in `docs/django-host.md` describe an identity check against an object the host keeps at `process_view`. The type check gives the same result for Django's four functions and needs nothing the host keeps per request; PR 9 updates those notes.

Step 2 also means the backend trusts whoever put a plain user on the request, and does not know which request input that code read. The most common such code is Django REST framework: inside a DRF view, the user that DRF authenticated, from a session, a token or a JWT, is on the Django request, and that is the user the backend resolves.

The backend therefore declares `("authorization", "cookie")`: the session is found by a cookie, and `Authorization` carries the credentials of DRF's token, basic and JWT schemes. Declaring a header that was not read only adds to the cache key, and requests without `Authorization` share one value of it.

The backend does not look at `is_active`. Django's `ModelBackend` already makes an inactive user anonymous, and a project that chose `AllowAllUsersModelBackend` chose otherwise.

Alternatives weighed:

- **The host keeps the object from `process_view` and passes it to the backend.** It matches the notes as written. Every backend's interface would carry an argument only this one uses.
- **Abstain when `request.user` is missing.** A host whose middleware order is wrong would call as anonymous instead of failing. The startup check should catch the order; this is the second line.
- **Declare `("cookie",)` only.** Exact for a project with sessions and nothing else. Inside a DRF token view the response would vary on the wrong header, and the declaration would be true or false depending on which views a project has.
- **`request_inputs` as a required option of this backend.** The deployer would state it. One more thing to write for the common case, and an easy place to under-declare.
- **Claims from the user**, such as `is_staff`, `is_superuser`, group names or the e-mail address. Actions often want them. They would cross hosts as the calling app's assertion and be stale by the next request, they would publish personal data to every app in the call chain, and they would begin a permission model in the core (0010 Decision 4, principle 12). A project that wants them subclasses the backend, or serves them from an identity app behind a contract.
- **Refusing an inactive user.** Stricter than the project's own login.

The cost:

- A plain user set from something other than a cookie or `Authorization` is under-declared: a custom header, or `RemoteUserMiddleware`, whose `REMOTE_USER` is not a header at all. Such a project subclasses the backend and sets `request_inputs`, to the header or to `("*",)`. The documentation has to name DRF, `RemoteUserMiddleware` and custom middleware.
- An authenticated user that is not an instance of the user model, such as simplejwt's `TokenUser` from stateless authentication, is refused, and every call from that view fails with `internal`. A project with such users lists a backend of its own before this one.
- A middleware that replaces `request.user` with a lazy object of its own is read through `auser()` on the async path, which may not agree with it.

### 8. The issuer names the user table, and the deployer supplies it

`issuer` is a required option with no default: 1 to 255 visible ASCII characters. It names the set of users the subject is unique in, which for this backend is one user table in one database. Hosts that read the same user table are configured with the same issuer. Hosts with different user tables are configured with different ones. The documentation recommends a URL the project controls, as OpenID Connect uses for `iss`, and the backend does not require one. The bound of 255 is the one OpenID Connect gives `sub` (OIDC Core, section 2), applied here to the issuer as well; OpenID Connect gives `iss` none. Issuers are compared code point by code point (spec 03), and the pair of issuer and subject as one identifier is RFC 9493's `iss_sub` format (section 3.2.3).

The deployer's part, which the host cannot check:

- one issuer per user table, and one user table per issuer;
- a key is never given to a second person under the same issuer. A restore that renumbers users, a reset sequence or counter, or a UUID key assigned by hand from an old value each break this, and each calls for a new issuer.

Alternatives weighed:

- **A default, such as a fixed word or the Django project's name.** Nothing to configure. Two hosts with separate user tables, which [0004](0004-each-app-owns-its-data.md) and the repository's [open question 4](../open-questions.md) make likely, would then both issue the same issuer with subject `7` for two different people, and a provider that stores principals could not tell them apart.
- **Derived from the database's settings**, such as a hash of host and name. Automatic, and it changes when the database moves to another server, which is when stored principals most need to stay equal.
- **Derived from `SECRET_KEY`.** Stable per project, but it changes on key rotation, and it ties an identifier that is logged and stored to a secret.
- **Requiring an `https` URL**, as OpenID Connect does for `iss`. One familiar form. A local user table has no URL of its own, so deployers would invent one, and the check would add nothing that the comparison uses: issuers are compared code point by code point (spec 03).
- **Any non-empty string.** Spec 03 allows it. Visible ASCII rules out a trailing space, a line break and look-alike letters, each of which makes two issuers that read the same and compare unequal.

The cost: the host cannot detect two user tables under one issuer, or one table under two. The first confuses two people; the second splits one person in two. The documentation has to state both.

### 9. The subject is the user's primary key in one text form

The subject is the user's primary key, written as text:

- an `int` as Python's `str()` writes it, in decimal with no leading zero;
- a `uuid.UUID` as 36 lower-case characters with hyphens.

The user model's key field is an `AutoField`, a `BigAutoField`, a `SmallAutoField` or a `UUIDField`, or a subclass of one. No other key is admitted in 0.1. The auto fields are the ones for which the database hands out each value once (Context, point 7), and a UUID key is not given twice in ordinary use; a plain `IntegerField` key can be reused by the database, which has the same effect as a reused username, and is refused. So is the `OneToOneField` key of a user model that inherits from another model, although its value is an `int`. The backend checks the key field at set-up, and the value on each resolution: the user is an instance of the user model, and its key is exactly an `int` or a `UUID`, so not a `bool`, not `None` and not a tuple.

With these bounds the sent form of spec 03 is at most a few hundred bytes, so a principal from this backend is always valid in 0017's sense.

This is the choice existing practice makes: django-oauth-toolkit's OpenID Connect provider gives the user's key, as text, as `sub`, and OpenID Connect asks of `sub` what Decision 8 asks of the deployer, that it is unique within its issuer and never reassigned. It is also a choice that 0010, as written, does not admit, and Decision 11 says so.

Alternatives weighed:

- **The username**, or the value of `USERNAME_FIELD`. Readable in logs. It can be changed, and a freed name can be taken by another person, who would then own whatever a provider stored under it.
- **A dedicated field**, such as a UUID column, named by an option. It survives a restore that renumbers keys, it hides the row count, and it fits 0010 as written. `django.contrib.auth`'s own user model has no such field, so the common case would need a custom user model before the first call.
- **A configurable field, with the primary key as its default.** Both of the above. The admitted set would gain an option, a uniqueness check and a rule for each field type.
- **Any `IntegerField` key.** It admits more projects, the inherited user model among them. Non-reuse would then rest on the deployer alone, for a property the field type can show.
- **String keys as they are.** A compatible addition for later. A string key raises questions an integer does not: the empty string, length, and Unicode forms that compare unequal.
- **A random identifier kept in a table of neolith's own.** Stable and opaque. It would give the core a table, which the repository's open question 4 has not settled for `neolith run`.

The cost: a subject reveals the key, and with integer keys roughly how many users exist. A project with a plain integer, inherited, string or composite key cannot use this backend in 0.1. A project that wants another subject subclasses the backend or writes its own.

### 10. What is refused, where, and with which message

Refusals follow 0022's two places. The **builder** refuses what it can see in the deployer's mapping without importing anything, with `ImproperlyConfigured`. **Set-up**, in the `neolith` app's `ready()`, builds the backends once and records every refusal. A system check under the tag `neolith` reports what was recorded, all together, each with an id from the host's `neolith.E` series, so a second run of the checks under a management command builds nothing again. The ids are assigned when PR 9 is opened, after the ids of the PRs before it. Each message begins with the setting and the entry's position, such as `NEOLITH["PRINCIPAL_BACKENDS"][1]`.

| Where | Refused | The message says |
|---|---|---|
| Builder | A value that is not exactly a `list`, or an empty list | the type found; for an empty list, to leave the key out |
| Builder | An entry that is not a `dict` with exactly `BACKEND` and `OPTIONS` | the keys found, and the two keys |
| Builder | A `BACKEND` that is not a `str` in dotted-path form | the value |
| Builder | An `OPTIONS` that is not a `dict`, or a key that is not an identifier | the key |
| Builder | `MIDDLEWARE` without `neolith.middleware.PrincipalMiddleware`, or with it more than once | the entry to add, or the count |
| Check | A `BACKEND` that does not import | the path and the cause |
| Check | A `BACKEND` that is not a class, is `PrincipalBackend` itself, is abstract, or is not a subclass of it | the path, and the base class to subclass |
| Check | A `resolve` that is a coroutine function, or an `aresolve` that is not | the path and the method |
| Check | A constructor that raises, an unknown option included | the path, the options' names and the cause |
| Check | No `request_inputs` on the instance | the path, and that the attribute has no default |
| Check | A `request_inputs` that is not a tuple of header names with no repeat, or `*` together with other names | the path and the value |
| Check | With a backend configured, `UpdateCacheMiddleware` or a subclass listed after the principal middleware | the order to use |

The Django-user backend refuses its own configuration in its constructor, in one message that lists every problem found, and the host reports it under the constructor's row:

| Refused | The message says |
|---|---|
| A missing `issuer`, or one outside the grammar | the grammar, and that hosts sharing a user table share the issuer |
| Any option other than `issuer` | the option |
| `django.contrib.auth` not installed | the entry to add |
| A user model whose key field is not one of the four admitted, an inherited (`OneToOneField`) key included | the model, the field's type, and the four admitted |
| `SessionMiddleware` or `AuthenticationMiddleware`, as the class or a subclass, missing or listed after the principal middleware | the order to use |

**On a request.** Each is a failure of Decision 5: recorded on the root, logged with the trace identifier, and seen by callers as `internal` with 0017's fixed message.

| Refused | The log says |
|---|---|
| A return value that is not exactly a principal value or `None` | the backend's position and path, and the type returned |
| A first call after the principal middleware's response phase | where the call came from, and that such code opens its own scope |
| A request with no `user` attribute | that `AuthenticationMiddleware` did not run for this request |
| An authenticated user that is not an instance of the user model | the user's type and the model |
| A key that is not exactly an `int` or a `UUID` | the key's type |

Alternatives weighed:

- **`ImproperlyConfigured` per entry, raised at the first problem.** It needs no check ids, and it would be the form to choose if system checks ran only from commands, as they do in plain Django. 0022 has the host run its own checks in `ready()`, in every process, so the checks can carry these refusals and report them together.
- **Checking the user model's key only on a request.** No startup work. A project with a string key would learn of it on the first call from a logged-in user.

### 11. A separate decision: 0010 Decision 2 is narrowed, so that a primary key may be a subject under a deployer-given issuer

This decision was written so that it could be taken or left apart from the rest, and what rests on it is named at the end of this section. It changes what an Accepted record, [0010](0010-pluggable-principal-resolution.md), says, and how one sentence of Accepted 0004 is read, and it was made as that change (D1), not as a reading of 0010.

**What 0010 says.** Decision 2: "a primary key on its own is never a subject". Its Consequences: a project that resolves Django users "needs an identifier for each user that is stable across hosts, which the primary key alone is not". "On its own" could be read as satisfied by the issuer. That reading would leave the sentence without effect, since every subject travels with an issuer (spec 03: "A subject is unique only within its issuer"). So Decision 9 does not fit 0010 as written.

**The narrowing.** With this record Accepted:

- 0010 Decision 2's sentence is narrowed to: a primary key is admitted as a subject only when the principal's issuer is given by the deployer to name the one table the key belongs to, and when the key's field type is one Decision 9 admits, whose values are not given twice in ordinary use; in every other case it is never a subject;
- 0010's Consequence is read with it: the identifier that is stable across hosts is the pair of that issuer and the key, and its stability rests on the deployer's part in Decision 8;
- a Consequence of [0004](0004-each-app-owns-its-data.md) is read with it too: "A database primary key is not a principal ID, because it means nothing outside its database." The key alone is still not a principal ID. The stored principal ID is the pair of issuer and subject, and under this narrowing its subject may be the key's text, with the issuer naming the table that gives the key its meaning;
- the pull request that marks this record Accepted adds one line under the status of 0010 and of 0004, `Narrowed by 0023 (Decision 11)`, with a link here. Nothing else in either changes.

**The rule this relies on.** D2 and the README of the records let an Accepted record change in three ways: a typo, a broken link, and `Superseded by NNNN` for a reversal. A narrowing of one sentence is none of them, and superseding 0010 as a whole is not what is meant. So the pull request that adds this record also amends D2 and the README's status line with a fourth way: "To narrow part of it, the new record quotes the sentences it narrows, and the old record gains a line under its status, `Narrowed by NNNN (Decision k)`; nothing else in it changes." The line stands on its own, not in `Status`, so the template's status values stay as they are. The sentences narrowed are the three quoted above: one in 0010 Decision 2, one in 0010's Consequences and one in 0004's Consequences.

**What is traded.** 0010 as written gives a provider a guarantee from the value alone, that no subject is a bare row number. With the narrowing, the guarantee depends on configuration the host cannot verify. What is gained: a project with `django.contrib.auth`'s own user model can make its first call without a custom user model or a new table.

**What rests on it.** While this record was Proposed, 0010 bound as written, and the Django-user backend of PR 9 could not merge with a key as its subject. Decision 9, the admitted set's `key field` and `subject` lines and Decision 10's two rows on the key rest on this narrowing; Decisions 1 to 8 and the rest of 10 do not. Without it, the dedicated field of Decision 9's alternatives would have taken their place.

## Consequences

- A host with no backend configured lists the principal middleware and does nothing per request for principals. `neolith run`, which in step 4 admits no battery with tables, runs this way, and every request root there is anonymous.
- A project with Django users adds one entry and one issuer. The same issuer on every host that shares the user table is what makes a principal compare equal across them (0010).
- Another source of identity, such as a bearer token checked against an identity provider, is a subclass with three members: `request_inputs`, `resolve` and, where it has an async path, `aresolve`. It sits before or after the Django-user backend, and the order decides.
- The Django-user logic moves out of the host and into the backend. The host keeps no user at `process_view`. `docs/django-host.md` changes with PR 9: its "Principal and trace" paragraph describes the type check and the sharing rule of Decision 5, the `Vary` patch moves from the root middleware to the principal middleware and becomes the union of Decision 6, and the notes gain the latest point for a first call. Their "Middleware" paragraph changes as well: the principal middleware follows `AuthenticationMiddleware` when the Django-user backend is configured, not in every host. Their conformance case for a streamed body asserts `Vary: Cookie`; with this backend the header names `Cookie` and `Authorization`, a superset of the `Cookie` that 0017 Decision 4 gives as its example, and the case is worded to match.
- Every resolution on the host's account now starts inside `SessionMiddleware`'s response phase, so a session change made by `get_user` is saved and its cookie set. Without the latest point of Decision 5, a first call from a middleware outside `SessionMiddleware` could reach `cycle_key()` during a key rotation: the old session would be deleted, and the new key's cookie never sent, so the user's next request would name a session that no longer exists. That path is now a recorded failure for the call instead.
- A synchronous first call made directly in the event loop evaluates `request.user` there, and Django raises `SynchronousOnlyOperation`. That is a failure of Decision 5, fixed for the root. The documentation has to say that async code makes async calls.
- A backend can be called more than once for one request, when a synchronous and an asynchronous first call overlap, or after a cancellation. A backend whose resolution has a side effect has to tolerate that.
- A deployer who uses Django's cache middleware places it before the principal middleware, which the host checks, and one who uses `cache_page` on a view that calls adds the declared inputs with `vary_on_headers`, which the host cannot check.
- A backend's declaration is trusted, not enforced, and two user tables under one issuer cannot be detected. Both are stated in the documentation as the deployer's and the backend author's part.
- The host does not time a backend. A backend that waits on a remote identity provider holds the first call for as long as it waits (Open question 3). Since no thread waits on another caller's search, such a wait holds only the caller that runs it and the async callers on its loop that chose to await it. The lock on the root is held only to read and write the outcome and the mark, never across a search.
- Step 4's public surface gains `neolith.middleware.PrincipalMiddleware`, `PrincipalBackend`, `DjangoUserBackend`, the `PRINCIPAL_BACKENDS` key and this record's check ids. The module that holds the two classes is named in PR 9.
- Specification changes for PR 9, in neutral terms and with new `APP` requirement IDs (the plan's APP-new-j and APP-new-k): a host resolves a request's principal through its configured backends in order, the first to resolve one decides, and a failure is fixed for the request; when a request's principal resolution has started, the host adds the request inputs its backends declare to the response's `Vary` before the response leaves the host, and it adds nothing to any other response. The second is worded as what the host adds, not as what every cache then does, because a cache placed inside the host's patch is outside its reach.
- Tests for PR 9, beyond cases 9 to 11 of the step's test plan. They run in two places, and each bullet says which.
  - **As plain functions, with built inputs and no host** (Decision 2's four functions). Any list of backends can be given here, so every case that needs a list other than the test host's is in this group:
    - Hypothesis draws settings from the grammar above and mutates one element at a time to produce each builder row and each check row; the shape and build functions report them;
    - for every admitted key value, the principal's sent form passes spec 03's check, and two Django-user backends built with one issuer give equal principals for one user; the key-field rule, called with test model classes, refuses a plain `IntegerField` key and an inherited key;
    - the search and the response phase with no backend: no backend is called, and the response gains no `Vary`;
    - the search and the response phase with two test-only backends in each order, with each outcome of the first (a principal, `None`, a raise, a wrong type), asserting which backends were called and the `Vary` header the response phase writes for the union given. A list in which one backend declares `("*",)` is a case of its own, and its one assertion on the header is `Vary: *`;
    - the search with a synchronous first call on the thread-sensitive thread overlapping an asynchronous one: both complete, without a wait on each other, and see one principal; the same for two asynchronous first calls on two event loops, and for a thread's first call that overlaps the response phase's search for a streamed response;
    - the search with a cancelled first call and a second caller already awaiting it: the second is not cancelled, and resolves; and a cancelled first call followed by a later one;
    - the response phase with a test-only backend that declares a custom header, followed by Django's `UpdateCacheMiddleware` and `FetchFromCacheMiddleware` called directly on the same request and response: two principals never share an entry. No backend in this list declares `("*",)`, since Django stores no response with `Vary: *` and the case would then pass without testing anything;
    - the check for `UpdateCacheMiddleware` listed after the principal middleware, given a built `MIDDLEWARE` list.
  - **On the one test host**, whose backend list is the Django-user backend alone, so its union is `("authorization", "cookie")` and holds no `*`. These are the cases that need Django's own middleware chain:
    - `logout`, `login`, `alogout` and `alogin` followed by a call, on both Django versions, on WSGI and ASGI, and on ASGI with a sync-only middleware placed after the principal middleware, so that it runs in sync mode;
    - a first call from the response phase of a middleware outside the principal middleware fails with `internal`, and with a fallback key in use the session's row and key are left as they were;
    - a view that assigns a plain user to `request.user` from `Authorization`, as DRF's setter does: the call carries that user, and the response's `Vary` names `authorization` and `cookie`. Whether DRF itself joins the development dependencies for this case is PR 9's call, with its one-line reason (K5);
    - `UpdateCacheMiddleware` and `FetchFromCacheMiddleware` in the host's list, before the principal middleware: two logged-in users never share an entry.

## Open questions

1. **The call side of the seam.** Step 5 (#12) adds the backend that authenticates the calling app. Its record decides whether that is a second base class or a second pair of methods, and whether both kinds share `PRINCIPAL_BACKENDS`. There is a naming risk: the key's name is public from step 4, and if step 5 puts call-side backends elsewhere, 0010's own word would name only half of the seam.
2. **Another subject for Django users.** A project with a string key, a plain integer key or an inherited user model, or one that wants an opaque subject, writes a subclass in 0.1. If several do, a `subject_field` option with a uniqueness check is the candidate.
3. **A time limit on resolution.** The root has a deadline (0017 Decision 4), and resolution runs inside the first call. Whether the host should bound a backend by that deadline, and how on the sync path, is not decided here.
4. **Telling a refused credential from a broken backend.** Comparable systems all give this a named signal: DRF's `AuthenticationFailed` beside a `None` return, Django's `PermissionDenied` in `authenticate()`, Spring Security's exception types in `ProviderManager`, ASP.NET Core's `NoResult` and `Fail`, and Encore's `Unauthenticated` beside any other error. A named exception here would let the host log a refusal as a warning and anything else as an error; callers would still see `internal`, as 0017 fixes. It is left open because no backend shipped in step 4 refuses a credential, and step 5's backend will, on the side where a refusal is `unauthenticated`: one exception should serve both sides, and naming it now would guess at step 5. Adding it later leaves a backend that raises a plain `Exception` logged as an error, as today.
5. **Users of several hosts** (the repository's [open question 4](../open-questions.md)). Whether each host has its own `django.contrib.auth` decides how many issuers a project ends up with. This record works either way, and does not settle it.
6. **A shared check for declared inputs.** The harness check named in Decision 3 is named, not specified. It belongs with `neolith.testing` when a second backend exists to try it on.
7. **A name per entry.** If a second shipped backend, or adopters' logs, show that position and path are not enough, a `NAME` key as in `TEMPLATES` is the candidate, and it would have to be required to stay one form.
8. **`Cache-Control: private`.** Whether the host should also mark a response private when a principal was resolved, as Decision 6 weighs, would benefit from adopters' cache set-ups.

## References

- [RFC 9110](https://www.rfc-editor.org/rfc/rfc9110#section-12.5.5), section 12.5.5: `Vary` lists request header names. [RFC 9111](https://www.rfc-editor.org/rfc/rfc9111), sections 3.5 and 4.1: storing responses to requests with `Authorization`, and the cache key of method, target URI and `Vary`.
- [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html#IDToken), section 2: `iss`, and `sub` as locally unique, never reassigned and at most 255 ASCII characters. [RFC 9493](https://www.rfc-editor.org/rfc/rfc9493#section-3.2.3), section 3.2.3: the `iss_sub` identifier format.
- [django-oauth-toolkit](https://github.com/jazzband/django-oauth-toolkit/blob/master/oauth2_provider/oauth2_validators.py), `get_claim_dict`: the user's key as `sub`.
- Django: [order of middleware for the cache](https://docs.djangoproject.com/en/5.2/topics/cache/#order-of-middleware), [`Vary` headers](https://docs.djangoproject.com/en/5.2/topics/cache/#using-vary-headers) and [other cache headers](https://docs.djangoproject.com/en/5.2/topics/cache/#controlling-cache-using-other-headers); [`TEMPLATES`](https://docs.djangoproject.com/en/5.2/ref/settings/#templates) and [`AUTHENTICATION_BACKENDS`](https://docs.djangoproject.com/en/5.2/ref/settings/#authentication-backends); [writing an authentication backend](https://docs.djangoproject.com/en/5.2/topics/auth/customizing/#writing-an-authentication-backend).
- Django REST framework: [custom authentication](https://www.django-rest-framework.org/api-guide/authentication/#custom-authentication), and [`Request.user`](https://github.com/encode/django-rest-framework/blob/master/rest_framework/request.py). simplejwt: [stateless user authentication](https://django-rest-framework-simplejwt.readthedocs.io/en/latest/stateless_user_authentication.html).
- Spring Security: [`ProviderManager`](https://github.com/spring-projects/spring-security/blob/main/core/src/main/java/org/springframework/security/authentication/ProviderManager.java) and the default [cache-control headers](https://docs.spring.io/spring-security/reference/servlet/exploits/headers.html#servlet-headers-cache-control). ASP.NET Core: [`AuthenticateResult`](https://learn.microsoft.com/en-us/dotnet/api/microsoft.aspnetcore.authentication.authenticateresult). Encore: [auth handlers](https://encore.dev/docs/go/develop/auth). Dapr: [middleware components](https://docs.dapr.io/operations/components/middleware/).
- Python packaging: [entry points](https://packaging.python.org/en/latest/specifications/entry-points/), the discovery mechanism Decision 4 weighs and does not use.
