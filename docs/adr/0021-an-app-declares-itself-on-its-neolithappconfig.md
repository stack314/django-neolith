# 0021. An app declares itself in class attributes of its `NeolithAppConfig`, with contracts named by their declaration classes

- **Status:** Proposed
- **Date:** 2026-11-12

## Context

Spec 05 lists what an app declares: `name`, `provides`, `requires`, `publishes`, `subscribes`, `resources` and `batteries`, "in a form the host can read without importing the app's implementation modules". It does not say what form each field takes. [0002](0002-django-app-is-the-unit-of-deployment.md) puts the declaration on a `NeolithAppConfig`. [0017](0017-call-context-explicit-at-the-action-ambient-for-propagation.md) Decision 6 adds one allow-list per provided contract: the calling apps that may assert a principal. [0003](0003-apps-communicate-through-contracts.md) fixes `connect("<contract>.v<N>")`, which the host resolves.

Three questions follow, and the first three pull requests of roadmap step 4 depend on them (#11; "PR 1" to "PR 9" below are the pull requests of that plan):

1. What is the one form of each field, and what happens to everything else?
2. How does the host get from a declaration to the contract packages and the implementations, without importing an implementation to read the declaration?
3. What does a call get when its contract is not in the calling app's `requires`?

Forces:

- **Fail at startup, with a message that names the cause** (K2, APP-1). A field in an unexpected form, or a misspelt field, should stop the host. It should not produce an app that quietly provides or requires nothing.
- **One form per input.** Each form the host admits is a path to test and to keep working. The step's design gate asks for the admitted set first.
- **The declaration is read early.** `neolith run <app>` builds settings from declarations, so a declaration has to be readable before Django's settings exist and before the app registry is populated. It also has to be the same declaration Django registers later (Decision 2).
- **Parity** (principle 5, [0007](0007-in-process-transport-serializes.md)). What an attached app gets, it gets detached too.
- **No hidden state** (K6) and **no new runtime dependency** (K5).

Two other Proposed records of step 4 are named here: [0009](0009-one-database-alias-per-app.md) as amended, which gives each app a database alias, and the app host record (0022), which sets what the host builds, when set-up runs and how a stop is reported.

What Django does, read at 5.2.19 and 6.1.3 (the cited files are the same in both unless noted):

- `AppConfig.create` imports the app's package and its `apps` submodule, and nothing else of the app (`django/apps/config.py`, lines 109 to 123). `models` is imported later, in the registry's second phase (`config.py` lines 262 to 269; `django/apps/registry.py` lines 114 to 116), and `ready()` runs in the third (`registry.py` lines 122 to 124).
- `AppConfig.name` is the app's Python path. `label` defaults to the last component of `name`, and has to be a Python identifier (`config.py` lines 33 to 38). Labels are unique in a registry (`registry.py` lines 92 to 96), and only there: Django's documentation describes `label` as unique across one project, and as the way to relabel an app when two labels conflict.
- `name`, `label`, `verbose_name`, `path` and `default` are not attributes of the `AppConfig` class. Django reads them with `hasattr` and `getattr` and sets them on the instance (`config.py`, `AppConfig.__init__`). A trial on both versions confirmed that `dir(AppConfig)` holds `default_auto_field`, `ready` and the methods, and none of those five.
- For an `INSTALLED_APPS` entry that is a module path, `create` picks the config class by scanning the `apps` module for `AppConfig` subclasses whose `default` is not false (`config.py` lines 121 to 156). A base class imported into `apps.py` is one of the candidates. With two candidates and none marked `default = True`, Django uses a plain `AppConfig`, with no message. A trial on both versions confirmed it: an `apps.py` that imports a base class and defines one subclass was registered with a plain `AppConfig`. `django.contrib.admin` deals with the same rule by marking `AdminConfig` with `default = True` beside its base `SimpleAdminConfig` (`django/contrib/admin/apps.py`). `default` is read with `getattr`, so a subclass inherits it.
- For an entry that is a path to a config class, an exception raised while `apps.py` is imported is swallowed once (`config.py` lines 160 to 164) and raised again when `create` imports the module a second time (lines 171 to 178). The trial confirmed that the original exception reaches the caller.
- A model class defined before the registry's first phase ends is refused: `ModelBase.__new__` calls `apps.get_containing_app_config` (`django/db/models/base.py` line 131 in 5.2.19, 132 in 6.1.3), which raises `AppRegistryNotReady`, or `ImproperlyConfigured` when settings are not configured (`registry.py` lines 129 to 138).
- `get_wsgi_application` and `get_asgi_application` call `django.setup()` and nothing more (`django/core/wsgi.py` and `django/core/asgi.py`, line 12). System checks run only from management commands (`BaseCommand.execute` in `django/core/management/base.py`, lines 459 to 461 in 5.2.19, 461 to 463 in 6.1.3).
- `AppConfig.create` needs no settings. A trial on both versions called `AppConfig.create("<package>.<app>")` before `settings.configure()` and got the instance of the app's own config class.
- The registry sets `apps_ready` after its first phase, so `apps.is_installed` works inside `import_models` (`registry.py` lines 112 to 116 and 242). In the trial, a registry with one app and without neolith's own app ran that app's `import_models` and its `ready()`, and nothing of neolith's. A check placed in the app's own `import_models` stopped that registry, and also an `override_settings` that left neolith's app out.
- `ready()` runs in `INSTALLED_APPS` order. `override_settings(INSTALLED_APPS=...)` populates the registry again, with new config instances, and runs every `ready()` again; leaving the override restores the stored configs and runs nothing (`registry.py`, `set_installed_apps` and `unset_installed_apps`, lines 339 to 368). The trial confirmed both.
- When a `ready()` raises, the registry stays marked as loading. A second `django.setup()` in that process raises `RuntimeError("populate() isn't reentrant")` (`registry.py` lines 83 to 84), which says nothing about the first failure. The trial confirmed it.

## Options

For the form of the declaration as a whole:

1. **Class attributes of the config class, in a closed namespace.** It reads as Django's own config options do, and a misspelt field is refused. A config class can then hold no other public name.
2. **A nested class that holds the fields**, closed as Django closes `class Meta`. The config class stays open to mixins. The fields move one level down, with rules of their own for inheritance.
3. **One attribute holding a frozen value**, built by a constructor that refuses unknown keywords. It reads less like Django.
4. **A data file beside `apps.py`.** Readable without running Python, and a second place to keep in step with the config class.

For naming a contract: its identity string, a dotted path to its class, or the declaration class itself.

## Decision

Option 1, with contracts named by their declaration classes. Each numbered decision below weighs its own alternatives, these options among them.

### The admitted set

A declaration is the class attributes of one `NeolithAppConfig` subclass. This is everything the host admits. Anything else is refused (Decision 8).

```
declaration  = name label [provides] [requires] [resources] [batteries]
                                                                 ; a field with no entry is left out; an empty tuple is refused
name         = <dotted Python path>                              ; Django's AppConfig.name, unchanged; it never leaves the host
label        = app-name                                          ; always written; spec 05's "name", Decision 1
                                                                 ; 0009 as amended adds: not "default"
app-name     = segment                                           ; "segment" of spec 01, CON-10
provides     = tuple of Provided, no two with one contract identity
Provided     = Provided(<contract class>,
                        implementation=<dotted-path with two or more identifiers>,
                        principal_from=<tuple of app-name, no repeats; may be empty>)
requires     = tuple of <contract class>, no two with one contract identity
resources    = tuple of resource kinds, no repeats; the one kind in 0.1 is "database"
batteries    = tuple of dotted-path, no repeats               ; a Django app name
dotted-path  = identifier *("." identifier)                      ; Python identifiers, each exactly a str
publishes, subscribes: absent in 0.1
```

Every container is exactly a `tuple`, and every string exactly a `str`. A `<contract class>` is a class that carries its own `@contract` mark, as `Contract.of` reads it. In the sketch below, angle brackets stand for the app's own names.

```python
from neolith import NeolithAppConfig, Provided

from <contract package> import <Contract>
from <other contract package> import <OtherContract>


class <App>Config(NeolithAppConfig):
    name = "<package>.<app>"
    label = "<app>"
    provides = (
        Provided(
            <Contract>,
            implementation="<package>.<app>.<module>.<Class>",
            principal_from=("<calling app>",),
        ),
    )
    requires = (<OtherContract>,)
    resources = ("database",)
    batteries = ("django.contrib.humanize",)
```

### 1. The app's name is Django's `label`, always written, narrowed to one segment

Spec 05's `name` is the value that crosses hosts: it is the `iss` of a shared-secret token (spec 03) and the entry in an allow-list. Django's `AppConfig.name` cannot be that value. It is a Python path, and `create` imports it (`config.py` lines 202 to 219). So the two attributes keep Django's meanings:

- **`name`** is the app's dotted Python path, exactly as Django defines it. Neolith reads it and sends it nowhere.
- **`label`** is spec 05's `name`. It is written in the body of the declaring class, and it has to match `segment` of CON-10: a lowercase ASCII letter, then lowercase ASCII letters, digits and underscores. A declaration without its own `label` is refused, also when the last component of `name` would have been admitted.

The label has one spelling per app for that reason. With Django's default, a label could be written, or derived from `name`, and moving a package would then change the app's token issuer and its allow-list entries without any line saying so. Two apps whose paths end in the same component, such as `<package_a>.core` and `<package_b>.core`, would also share a name without either author having chosen it.

A project cannot relabel a neolith app. Django's remedy for two conflicting labels is a project-level subclass of the config class that sets another `label`. A declaring class admits no subclass (Decision 2), because that subclass would change the name the app sends to other hosts and is listed under in their allow-lists.

Alternatives weighed:

- **Django's default, with `label` written only when the derived one is not a segment.** Fewer lines for most apps, and the familiar behaviour. It leaves two spellings and the silent rename described above.
- **Require `name` and `label` to be equal.** One identifier, but an app could then only be a top-level package. Many projects nest their apps.
- **A new attribute, such as `neolith_name`**, as Dapr keeps an `app-id` in deployment configuration, apart from the code. It allows any spelling, and a project could relabel freely. It also gives each app a second identity. Views are already bound to apps through Django's registry ([Django host notes](../django-host.md)), which knows the label, and [0009](0009-one-database-alias-per-app.md) as amended names an app's database alias by it.
- **Admit any Python identifier, as Django does.** Uppercase and non-ASCII labels would then cross hosts. The contract grammar keeps to lowercase ASCII so that one spelling works in a path, a header and a token, and the same reasoning holds for an app's name.

The costs:

- Every neolith app writes one more line than Django asks for, and for a top-level app that line repeats `name`.
- An existing app whose label has an uppercase letter has to change it before it can be a neolith app, and changing a label also changes migration and table names.
- Django keeps labels unique within one registry. Nothing keeps them unique across hosts: two hosts may each hold a different app with the same label, both would send the same `iss`, and both would match the same allow-list entry. No host can see the other's apps, so keeping names unique among all hosts that exchange calls is the deployer's duty. The specification delta says so.
- Two apps with one label cannot share a host, and the deployer cannot relabel either. One of the two apps has to change its own label.
- The name is not a host name. `segment` admits `_` and no `-`; a DNS label admits `-` and no `_`. Where a detached app's address is derived from its name, that mapping is deployment configuration.

### 2. The declaration is class attributes, in a closed namespace, checked when the class is defined

The fields are class attributes, as `default_auto_field` is on `AppConfig`. `NeolithAppConfig.__init_subclass__` checks them, so a declaration in an unexpected form fails when its `apps` module is imported, as a broken contract fails when `@contract` runs. The check needs no settings and no registry.

**Declaring classes and base classes.** A subclass that sets `name` in its own body is a *declaring class*. Any other subclass is a *base class*, as `SimpleAdminConfig` is for `AdminConfig`.

- A declaring class is checked in full. It is given `default = True` unless it sets `default` itself. It admits no subclass.
- A base class is given `default = False`. It holds none of `label`, `provides`, `requires`, `resources` and `batteries`, so a declaration is read in one class body. It may hold `default_auto_field`, `verbose_name` and `ready`.

`NeolithAppConfig` itself has `default = False`. Django then picks the declaring class from an `apps` module that also imports `NeolithAppConfig` or a base class, in place of the plain `AppConfig` described in Context. Two declaring classes in one `apps` module both default to true, and Django refuses the module with its own error, a `RuntimeError` that names them (`config.py` lines 145 to 150). 0022's builder reports that error as the cause of its own refusal.

**The namespace is closed.** The check reads every class in the subclass's method resolution order that `NeolithAppConfig` does not itself inherit from, so a mixin is checked as the class's own body is. A public name defined in any of them is admitted only when it is in one list, kept as one constant beside `NeolithAppConfig`:

- the four fields;
- `name`, `label`, `verbose_name`, `path`, `default` and `default_auto_field`, the options Django's documentation gives for a config class;
- `ready`.

Any other public name is refused, an override of `import_models`, `create`, `get_model` or `get_models` included. Helpers start with an underscore. `@implements` already treats implementation classes the same way. A test compares the list with the public attributes of an `AppConfig` instance and of the class on every supported Django version, so a new Django option fails neolith's own build before it reaches an adopter.

**The host reads a snapshot.** When the check passes, `__init_subclass__` stores the declaration as one frozen value on the class. The host reads only that value. Rebinding a class attribute afterwards changes nothing the host sees.

**The host reaches the class the way Django does.** Before settings exist, the host reads a declaration with `AppConfig.create(<entry>)`, the call Django's registry makes later for the same entry, so there is one rule for choosing the class and it is Django's. Which entries the host admits, and that the same entries become `INSTALLED_APPS`, is 0022's decision. `create` gives the same class for the same entry. 0022 requires a declaration's `name` to be the entry it was read through, and has set-up compare `INSTALLED_APPS` with what the builder gives for the installed declarations, so the class Django registers is the class the host read. An `apps` module that reads Django settings when imported cannot be read before settings exist; 0022 gives that refusal its message.

Alternatives weighed:

- **A nested class that holds the four fields, closed as Django closes `class Meta`** (`Options.contribute_to_class` raises `TypeError` for an unknown name, `django/db/models/options.py`), with the config class left as open as Django leaves it. It refuses a misspelt `requires` just as well, needs no list of Django's options, and lets a third-party config mixin with public attributes, such as django-oscar's, be used unchanged. It was the closest alternative. Against it: 0002, the roadmap and the plan describe fields on the config class; a field written on the outer class out of that habit would need its own refusal; and the nested class would need rules of its own for inheritance. The closed class keeps one level and one rule. It can be revisited without breaking the specification, since the form is django-neolith's and not spec 05's.
- **One attribute holding a frozen value**, such as `declaration = Declaration(provides=..., requires=...)`. A constructor refuses an unknown keyword on its own, which is a real strength. It reads less like Django, for the same reasons.
- **An open namespace**, as Django's `ModelAdmin` and its own config classes have (`SimpleAdminConfig.default_site`, `StaticFilesConfig.ignore_patterns`). A misspelt `requires` would give an app that requires nothing. In a detached provider, a misspelt `provides` would give a host that starts and serves nothing.
- **Fields inherited from a base class.** Python would allow it, and a project could share `resources` among its apps. A declaration would then be spread over several files, and an entry added to a base would change every app below it.
- **Checking at `ready()` only.** The registry is available there, but the failure comes later, and a host that reads declarations before it configures settings would read unchecked values.
- **A module of data beside `apps.py`, or a TOML file.** Readable without running any Python, but a second place to keep in step with the config class, and a format of its own to define.

The costs:

- When a later Django release adds a class-level `AppConfig` option, neolith refuses it until the list admits it. The failure is loud, the fix is one line, and the test above reports it first.
- A neolith app cannot use a config mixin that adds public names, and cannot add a public method to its config class. Such a helper is renamed with an underscore, or lives in a module.
- A config class cannot be extended or relabelled by a project (Decision 1).

### 3. Containers are tuples, strings are `str`, and a repeat is refused

Each field is exactly a `tuple`. A list, a set, a dict, a generator or a subclass of `tuple` is refused, with a message that shows the tuple to write. A repeated entry is refused, not merged.

A field with no entry is left out of the class. An empty tuple on `provides`, `requires`, `resources` or `batteries` is refused, with a message that says to remove the line, so that "none" has one spelling. `principal_from` is the one tuple that may be empty, because it is a required argument and its empty form says something (Decision 6).

A value that is not a container at all gets a message of its own. `batteries = ("<name>")` is a `str` and `requires = (<Contract>)` is a class: in both the trailing comma is missing, and the message says so. Django guards the same mistake in its settings by name ("The INSTALLED_APPS setting must be a list or a tuple", `django/conf/__init__.py`).

Alternatives weighed:

- **Admit lists too**, as `INSTALLED_APPS` does. Friendlier to habit, but two forms, and a value that other code could change in place after the check. A tuple of strings and frozen values cannot be changed in place; rebinding the attribute is covered by the snapshot of Decision 2.
- **Sets or dicts**, since the fields are sets in meaning. A literal with a repeated element or key drops it silently, so a repeat could never be reported.

The cost: an author who writes a list out of habit gets a refusal. The message says what to write.

### 4. `provides` and `requires` name contracts by their declaration classes

An entry of `requires` is the contract's declaration class, imported from its contract package. An entry of `provides` is a `Provided` value whose first argument is such a class. The identity, `<contract>.v<N>`, is read from the class.

This settles how the host finds contract packages: it does not search. The `apps` module imports them, and Python's import system is the lookup. The host builds its table from identity to contract out of the declarations it has read, and `connect("<contract>.v<N>")` is checked against that table (Decision 10). There is no registry that `@contract` writes to, no module the host scans, and no naming convention.

**One rule for which identities a host knows:** an identity is known when any declaration in the host names it, in `provides` or in `requires`. Decisions 9 and 10 both use this rule.

A contract package holds an interface and its types and no implementation (spec 00), so importing one imports no implementation module. Within one host, one identity has one declaration class. A second class with the same identity, in any app's `provides` or `requires`, is refused.

The pair has precedent. A Java module declares `provides <interface> with <implementation>` and `uses <interface>`: the interface is named by type, the implementation by name, and it is loaded late.

Alternatives weighed:

- **Identity strings**, such as `requires = ("<contract>.v1",)`, as the roadmap's sketch shows. The declaration is then plain data and reads like the `connect()` call. But the host needs a second mechanism to find the class behind each string: a path beside every identity, a module it scans by convention, or a process-wide registry filled by importing modules it would first have to find. Each is one more input to specify, and one more way to fail.
- **Dotted paths to contract classes**, as Django's settings name classes. Lazy, but the identity disappears from the declaration, and a type checker cannot see a misspelt path.
- **Entry points** (`importlib.metadata`). They suit installed distributions. Many Django projects keep their apps as plain packages on the path, with no distribution metadata.
- **Admit two classes with one identity in a host.** The in-process transport crosses values as JSON text, so it could carry a call between two compatible copies of a contract, as HTTP does. That is a compatible addition for later. One class per identity is the smaller set for now.

The cost: a declaration cannot be read without importing its contract packages, and `apps.py` gains imports. Those packages import with settings unconfigured, and the tests assert it. The identity is written in `connect()` and not in `requires`, so messages show both the class and its identity.

### 5. An implementation is named by a dotted path, imported at Validate, and belongs to the providing app

`Provided(..., implementation="<path>")` takes the dotted path of the implementation class. The path is a string so that reading the declaration does not import the implementation, which usually imports models. Django names its pluggable classes the same way and imports them late (`AUTHENTICATION_BACKENDS`, `DATABASE_ROUTERS`, `MIDDLEWARE`), as Django REST framework does for `DEFAULT_AUTHENTICATION_CLASSES`.

A class has one admitted path: its own `__module__`, a dot, and its `__qualname__`, where the class is defined at the top level of its module. A path that reaches the class through a re-export in another module is refused, so one class has one spelling. The host imports the path at Validate, once the registry has loaded models, and checks that:

- it is a class, and the path is that class's own path;
- the class's `__module__` is inside the providing app, judged by `apps.get_containing_app_config` (`registry.py` line 251), which handles nested apps;
- that module is neither the app's package nor the module that holds its config class, wherever that class is kept, since both are imported at Declare;
- it passes the same check `@implements` runs.

The host then builds one instance, with no arguments, and keeps it for as long as that set-up of the host lasts (Decision 8). A failure in any of these stops the host. `@implements` stays available for the earlier, import-time check and for type checkers; the host's check does not depend on it.

Alternatives weighed:

- **The implementation class itself in the declaration.** Reading the declaration would import the implementation. That is the thing spec 05 rules out. Django would refuse it too, once the module defined a model.
- **Any path `import_string` resolves.** It is the form Django's settings admit. One class would then have several spellings, and "inside the providing app" would have two readings for a re-exported class.
- **A module found by convention**, such as `<app>.implementations`, scanned as the admin's `autodiscover` scans `admin` modules. Nothing to write, but the host would have to settle two implementations of one contract, or an implementation imported from elsewhere, by rule and not by reading.
- **A registry that `@implements` fills.** It needs something to import the modules first, and it is process-wide state outside the host (K6).
- **A new instance per call**, as Django builds a class-based view per request. It protects against state kept between calls. It also runs `__init__` on every call, and a failing `__init__` would surface on a call and not at startup. One instance is what gRPC servicers use. The documentation has to say that the instance is shared across threads.
- **Checking `sys.modules` to prove that no implementation was imported at Declare.** Django already refuses the harmful case, a model defined too early. An early import that defines no model does no harm, and the check would misfire in test processes.

The cost: the implementation's `__init__` runs in every process that loads apps, under every management command too, `migrate` included. It must not read the database and cannot make a contract call (Decision 10). The documentation has to say so.

### 6. The allow-list is a required argument of each `Provided`

`principal_from` is a tuple of app names: the calling apps that may assert a principal on calls to this contract (0017 Decision 6). It is keyword-only and has no admitted default: leaving it out is refused. An empty tuple means that no calling app may assert one. Each entry matches `app-name`. There is no wildcard. The providing app gets no exception for itself: a call it makes to its own contract with a principal needs its own name in the list.

The host does not check that a listed name is an app it knows. A caller may live in another host, and the provider's host has no list of those.

Alternatives weighed:

- **A separate field**, such as a map from identity to callers. Two structures can disagree: a list for a contract the app does not provide, or a provided contract with no list. One value per provided contract cannot.
- **A default of the empty tuple.** It fails closed too, but an omitted argument and an empty tuple would be two spellings of one thing, and the question of who may speak for an end user would be answered without the author seeing it.
- **A wildcard**, such as `"*"`. Convenient for a contract with many callers, but any app that authenticates could then act as any user. Adding one later is a compatible change. Removing one would not be.
- **Refusing names the host does not know.** It would catch a misspelt name in a shared process and refuse every caller of a detached provider.

The cost: a misspelt name is not caught at startup. It shows as `permission_denied` on the first call that carries a principal. The error's message names the calling app and the contract.

### 7. `resources` and `batteries` take names only, and `publishes` and `subscribes` are refused in 0.1

This record fixes the form of these fields, which PR 1 checks at class definition. What a host does with an entry belongs to the records that supply it.

- **`resources`** is a tuple of resource kinds. The one kind admitted in 0.1 is `"database"`. The other kinds spec 05 lists are refused until a host can supply them. The app names the kind only: which alias, server or file it gets is the host's and the deployer's concern. Spec 05 says "named resources"; in 0.1 that is read as one resource per kind, named by its kind, and the specification delta says so. That an app with models declares `"database"` is a rule of 0009 as amended, checked by the host once models are loaded. This record sets no such rule.
- **`batteries`** is a tuple of Django app names, each the dotted path Django keeps as `AppConfig.name`, such as `django.contrib.humanize`. An app does not list itself. What the host does with an entry is 0022's decision: it installs each listed battery, refuses one that is itself a hosted app, and refuses one with tables in a host that has no database for them.
- **`publishes` and `subscribes`** are refused when present, even as empty tuples. They arrive with spec 04.

Alternatives weighed:

- **A map from kind to name**, such as `{"database": "<app>"}` in the roadmap's sketch. The name would repeat the app's name, or differ from it, and then two apps could claim one alias from inside their own code. Placement is deployment configuration (principle 3).
- **A name scoped to the app for each resource**, as Encore declares a database by name in the service's code and Dapr apps refer to components by name. It lets an app hold two resources of one kind, and the scope removes the clash between apps. No host in 0.1 could supply the second one.
- **Admitting every kind spec 05 lists.** An app would declare a cache or a queue that no host supplies, and learn it at first use.
- **A fixed vocabulary of batteries**, such as `"staticfiles"`. The host would know how to enable each one, but neolith would keep a list of Django's and third-party apps. An app name is the form Django already checks.
- **A path to a config class as a battery.** `INSTALLED_APPS` admits it. It is a second form, and choosing a config class for another package is not a declaration's part.
- **Admitting empty `publishes` and `subscribes`.** An attribute that can only be empty is a second way to say nothing.

The cost: a tuple of kinds cannot say "two databases". When a host can supply a second resource of one kind, a later record has to choose between a second form of entry and a change to this one. That choice is deferred, not avoided.

### 8. What is refused, when, and with which message

Every refusal names the app and the field (APP-1, K2). At class definition the app is named by its config class, and from the registry on by its label. Every refusal here is a `neolith.DeclarationError`, a subclass of Django's `ImproperlyConfigured`, which is what Django raises for an entry of `INSTALLED_APPS` it cannot use.

**At Declare, when the class is defined.** Python stops at the first error, so these come one at a time.

| Refused | The message says |
|---|---|
| A public name outside the closed namespace, in the class or a mixin | the name, the class that defines it, the admitted names, and that helpers start with `_` |
| A declaring class without its own `label`, or a `label` outside `app-name` | the grammar, and the line to write |
| A base class that holds `label` or one of the four fields | the name, and that a declaration is written in the class that sets `name` |
| A subclass of a declaring class | both classes, and that a declaration cannot be extended or relabelled |
| `publishes` or `subscribes` | that events arrive with spec 04, and to remove the attribute |
| A field that is a `str` or a class | the field, and that the trailing comma of a one-element tuple is missing |
| A field that is otherwise not exactly a `tuple` | the field, the type found, and the tuple to write |
| A field that is an empty tuple | the field, and to remove the line |
| A `provides` entry that is not a `Provided` | the entry's position and type |
| A contract argument that is missing, or one, or a `requires` entry, that is not a contract class | the value, and that the class carries `@contract` |
| An `implementation` that is missing or is not a `str` in dotted-path form | the contract's identity and the value |
| A `principal_from` that is missing, not a tuple, or holds a non-name, a repeat or `"*"` | the contract's identity and the entry |
| One identity twice in `provides` or in `requires`, or with two classes in one declaration | the identity and both classes |
| A `resources` entry other than `"database"`, or a repeat | the entry, and the kinds admitted |
| A `batteries` entry that is not a `str` in dotted-path form, a repeat, or the app itself | the entry |

**Where a `Provided` is checked.** `Provided(...)` runs in the class body, before the config class exists, so it cannot name the app. It therefore checks nothing: it keeps what it was given, with a private marker for an argument left out, and `__init_subclass__` checks every `Provided` with the rest of the declaration. A missing contract, `implementation` or `principal_from` is then a `DeclarationError` that names the config class, like every other row. Only an argument the signature does not have is refused by Python itself, with its `TypeError` at the call.

`Contract.of` raises `ContractDefinitionError`, a `TypeError`, for a class that is not a contract. The check catches it and raises `DeclarationError` naming the config class and the field, with the original error as its cause, so one error type covers the table.

**On the app's own config, while the registry loads.** One refusal cannot wait for neolith's own app, because it is about that app being absent.

| Refused | The message says |
|---|---|
| A declaring class in a registry where neolith's own app is not installed | the app, and the entry to add |

`NeolithAppConfig.import_models()` makes this check and then calls Django's method. The registry calls it for every config, in its second phase, and the closed namespace of Decision 2 refuses an override, so no declaring class skips it. An override of `ready()` without `super()` does not matter here. The trial in Context covers a registry without neolith's app, and an `override_settings` that leaves it out.

**At set-up: Resolve and Validate, once the registry has loaded models.** These checks need every declaration, so the host runs them once per set-up, from neolith's own app config. The host collects every refusal of this table and raises one `DeclarationError` that lists them, sorted, each naming its app and field, as Django's system checks and Spring Modulith's verification report every problem in one failure.

| Refused | The message says |
|---|---|
| One identity with two declaration classes in the host | the identity, both classes and both apps |
| Two apps that provide one identity | the identity and both apps |
| A `requires` entry with no provider (in step 4, an app in this host) | the requiring app and the identity |
| An `implementation` that does not import, is not a class, is named by another path than its own, is outside the app, or is in the app's package or the module of its config class | the app, the identity, the path and the cause |
| An implementation that fails the contract's check, or whose construction raises | the app, the identity, and the action or the cause |
| A `connect()` made before set-up for an identity the host does not know (Decision 10) | the identity, the file and line of the `connect()`, and the identities the host knows |

Four things about set-up are fixed here, and the rest is 0022's:

- **It runs in every process that loads apps.** Django runs system checks only from management commands, so a WSGI or ASGI server would otherwise start a host that had never been checked. 0022 proposes the mechanism: set-up runs from the `ready()` of neolith's own app, which is installed last, and raises the refusals of this table as one `DeclarationError`, beside the host's own system checks. This record needs only that they run in every process and stop the host. #11's plan words PR 1 as "checked when the app registry is ready"; under this record the form is checked at class definition, and the rest at set-up.
- **The host owns the state, and one set-up owns one copy.** The table from identity to contract and provider, and the implementation instances, are created by a set-up and belong to it. Nothing in them is kept at module level, and a connector holds no part of them (Decision 10). A second set-up in one process, which `override_settings(INSTALLED_APPS=...)` causes, therefore never reuses a table or an instance of the first. 0022 proposes which later set-up is admitted: one with the same hosted apps builds a fresh copy, and one with a changed set of hosted apps is refused. The tests cover both with `override_settings`, entering and leaving.
- **Calls wait for it.** Until set-up has finished without a refusal, every connector call is refused (Decision 10), so a host whose checks never ran fails closed.
- **The first failure is logged.** Before the host raises, it logs the refusals at error level on a `neolith` logger. A server that retries loading then shows Django's reentrancy error (Context), and the log still names the cause.

Alternatives weighed:

- **System checks as the only gate.** They list every problem at once, and `neolith check` can report them in step 7. They cannot be the only gate, for the reason above.
- **The first `NeolithAppConfig.ready()` runs all the checks.** No entry to add to `INSTALLED_APPS`, but an app that overrides `ready()` without calling `super()` would skip them. 0022 needs neolith installed in any case, for its management command and its checks.
- **Stopping at the first refusal.** Less code. An author with three mistakes would start the host three times to see them.

### 9. A call to a contract outside `requires` is a mistake in the calling code

`connect()` does not know which app is calling; the call's scope does (0017 Decision 3). So the check that binds an app to its own `requires` runs on each call, on the caller's side, in every mode.

**The check.** This record adds one step to the caller's order of 0017 Decision 9, numbered here as **step 1a**. It comes inside that record's step 1, after the context is found and before the principal is resolved, so a refused call reads no session and no user:

> 1a. When the scope has an app and that app's `requires` does not hold the contract, the call is a usage error.

0017's text is not edited. Its sentence "a call counts as made once it passes steps 1 and 2" reads, with this record, as steps 1, 1a and 2: a call refused at 1a is not sent and does not count as made, no key is derived and no count changes. The specification delta for PR 3 adds the step to spec 02's order of checks as a new `TRN` requirement.

**The error.** It is `neolith.UsageError`, a subclass of `RuntimeError`. It is neither a `ContractError` (CON-29) nor a `LookupError`. Python raises `RuntimeError` for a call made in the wrong state, and step 3's design gate for the call context (#10) reports a connector call with a missing or closed scope, the usage error of 0017 Decision 5, as a `RuntimeError`. A named subclass keeps code written against step 3 working. From PR 3 the missing and the closed scope raise `UsageError` as well, so one class covers the usage errors that are about the host: its state, such as a scope that is missing or closed or a host not yet set up, and what it holds, such as an identity or an app it does not know. Django names such conditions too (`AppRegistryNotReady`, `SynchronousOnlyOperation`), and Java raises `IllegalAccessError`, not a lookup failure, for access outside a module's `requires`. The message names the calling app, the identity and the app's `requires`. Since the scope's app can differ from the app whose code is running, it also says that code running in another app's root opens its own scope (0017 Decision 4). The same class serves the usage errors of Decision 10 that are about the host. A value that is mistaken in itself, whatever the host holds, keeps the builtin class step 3's connector raises for it: `AttributeError` for an action the contract does not have, `TypeError` for a wrong argument (TRN-16) and `ValueError` for a request that fails the request check (TRN-14).

**Which scopes and connectors.**

- An app with no declaration requires nothing, so every call from it is refused this way. The message says that the app becomes a caller with a declaring class that holds `name`, `label` and `requires`.
- A scope opened by name, such as the command scope, names a hosted app. One that names anything else is refused when it is opened; 0022 gives that refusal its form. A request root is bound by Django's registry and can belong to an app with no declaration, which is the case of the first bullet.
- A scope with no app has no `requires` to check. Its call goes on, and the provider's host refuses it with `unauthenticated`, as 0010 Decision 7 says.
- The check sits in the connector's call path and reads the scope, so it applies to every connector, however it was built. Step 3's connector is built from a declaration class and a transport, and its tests build it that way; from step 4 those calls pass step 1a like any other, in a scope whose app requires the contract.
- An app may list a contract it provides in its own `requires`, and then calls it like any other.

**The runtime check is the backstop.** A call outside `requires` on a rarely taken path is found only when that path runs. Systems that bind a module to its declared dependencies check before the program runs: Spring Modulith in a test, Encore and Java at compile time. The identity in `connect("<contract>.v<N>")` is a literal, so step 7's static pass (#14) compares each such literal in an app's modules with that app's `requires`, in the shape of the import check 0003 already plans. PR 3's specification delta states this as a recommendation, not a requirement, in neutral terms: an implementation offers a check, runnable before deployment, that reports a call site naming a contract outside the calling app's `requires`. Its requirement ID is assigned in that pull request, since IDs are assigned in `spec/` (P5).

Alternatives weighed:

- **`permission_denied`, or another contract error.** A contract error is an outcome of a call (0008 Decision 6, CON-29). Here nothing was called, the cause is the caller's own declaration, and code that handles contract errors would swallow a bug that a deploy would then expose.
- **The builtin `LookupError`**, as Django's registry raises for an unknown app (`registry.py` lines 150 to 165). It needs no new class. It is also the base of `KeyError` and `IndexError`, so an `except LookupError:` written for a dict around code that makes a call would swallow the mistake, which is the reason given above against a contract error.
- **A plain `RuntimeError`**, as step 3 raises for a missing scope. It needs no new class either. Calling code and tests could then tell a neolith usage error from any other `RuntimeError` only by its message.
- **A class of its own under `Exception`**, apart from `RuntimeError`. It cannot be caught by accident through a builtin base. The missing scope would then either keep a second class or change what code written against step 3 catches.
- **No check on a call: the declaration only drives Resolve.** An attached app could then call any contract that another app in its host requires. The call would work until the app is detached, and then fail. 0017 Decision 6 closes the same gap for principals.
- **Checking in `connect()`, by finding the calling module from the stack.** It would fail earlier, at the `connect()` line, but frame inspection is fragile under wrappers and decorators, and it is the kind of hidden input K6 asks to avoid.

### 10. `connect()` is safe at import, and set-up checks every identity

`connect("<contract>.v<N>")` returns a connector that holds the identity and nothing else. It keeps no reference to the host's table, a provider or an instance; each call finds the current host's table by the identity. Step 3's connector class is private and holds its contract and its one transport from construction; PR 2 puts the lookup in front of it, and the public behaviour is the table below. When `connect()` runs decides how the identity is checked:

| When | `connect()` | A call through the connector |
|---|---|---|
| Before the host's table exists: at import of any module, in any app's `ready()`, in an implementation's `__init__` | returns a connector, and records the identity with the host, with the file and line of the `connect()` for the message | `UsageError`: the host is not set up yet |
| After the table exists | checks at once: an identity the host does not know (Decision 4) is a `UsageError` naming it and the identities the host knows | as above until set-up has finished, then admitted |
| An identity that is not a `str` in the grammar of CON-10, at any time | `TypeError` or `ValueError` at the `connect()` line, as for any argument mistaken in itself (Decision 9) | none |

Set-up checks every recorded identity against the table, after Validate has imported the implementation modules, and an unknown one stops the host (Decision 8). The recorded set is host-owned state of the kind K6 permits: the host documents it and empties it at set-up. The file and line are read for the message only and decide nothing.

So a module-level `connect()` works in `models`, in an implementation module and in a URLconf alike, and a misspelt identity in any module imported by the end of set-up stops the host. The position of neolith's app in `INSTALLED_APPS` does not change what `connect()` does. It changes only when calls begin, and 0022 puts neolith's app last, so no app's `ready()` can make a call.

**What is still found late.** A module first imported after set-up runs its `connect()` then, and an unknown identity raises there.

- The root URLconf and the modules it imports: 0022's Boot imports `ROOT_URLCONF` before Serve, so these fail before the first request under any server.
- A module imported inside a function, on some request: its `connect()` fails on that request. This is a known gap against APP-1 for unknown identities. The static pass of Decision 9 reads the same literals and closes it before deployment.

Alternatives weighed:

- **Eager only: `connect()` is refused until the host has resolved.** It is the simplest rule. A `connect()` at module level, as the roadmap's sketch writes it, would then fail in every module the registry imports while loading, and in a lazily imported module the eager form is the one that fails latest. Django relaxed the same rule for `get_user_model()` in 1.11, which may be called at import since then.
- **Lazy only: the identity is checked on the first call.** No recorded state. An unknown identity would surface at first use, which K2 rules out. Django's lazy handles (`reverse_lazy`, `django.db.connections`) and a gRPC channel accept that for their cases.
- **The host imports every module of every app at set-up**, to run every `connect()` line. It would close the gap above. It would also run import side effects Django leaves lazy, and change which process pays for them.

The cost: the host holds one more piece of documented state, and an unknown identity is reported in one of two ways, by set-up or at the `connect()` line, depending on when the module was imported.

## Consequences

- A declaration is plain to read and has one spelling. A misspelt field, a list, a missing comma, a wildcard or an events field stops the host with a message naming the app and the field.
- The host has no discovery mechanism for contracts. Its table from identity to contract comes from the declarations, so it holds nothing an app did not declare.
- An `apps` module imports the contract packages the app provides and requires. A contract package has to import with settings unconfigured. A test for PR 1 imports an `apps` module in a fresh process with no settings.
- The label is the name that crosses hosts. Renaming an app later changes tokens, allow-lists and, in Django, migration and table names. Keeping names unique among the hosts that exchange calls is the deployer's duty, and no host can check it. The documentation has to say both.
- **Adoption cost.** 0002 calls a conventional Django project a host, and a running project adopts neolith one app at a time. Under Decision 9 a plain Django app in that project cannot call a newly declared contract until it has a declaring class of its own. The smallest step is a `NeolithAppConfig` with `name`, `label` and `requires`, and the refusal's message says so. Code that runs in a third-party app's root, such as a signal receiver or a template tag reached through that app's view, cannot be given a declaration by the project. It opens its own scope naming a declared app (0017 Decision 4). The Django host's test case "a view from another installed app calls as that app" therefore uses a declared app.
- An app that uses a config mixin with public names, or a project that extends another package's config class, has to change before the app can be a neolith app (Decisions 1 and 2).
- `connect()` can be written at module level in any module. A call cannot be made before set-up has finished: not at import, not in a `ready()`, and not in an implementation's `__init__`, which also runs under every management command.
- An unknown identity in a module imported after set-up, other than through the root URLconf, is found when that module is imported. The static pass in `neolith check` (step 7, #14) is what reports it earlier, together with calls outside `requires`.
- The check of Decision 9 adds one set lookup per call on the caller's side, and each call finds the host's table by identity. No speed is claimed, so K10 asks for no benchmark.
- An allow-list is not checked against known apps, so a misspelt caller shows on the first call that carries a principal.
- One implementation instance serves every call, from every thread. Implementations keep no per-call state.
- A resource kind the host cannot supply is refused at class definition. The set of kinds grows only with a host that supplies them, and a second resource of one kind needs a later record.
- A failed set-up is logged before it is raised, because a server that retries loading sees Django's "populate() isn't reentrant" and not the refusals.
- Decision 9 adds step 1a to the caller's order of 0017 Decision 9, as this record's own addition. It does not change that record's text, its existing checks or their order (D1, D2).
- `neolith.UsageError`, `neolith.DeclarationError`, `NeolithAppConfig`, `Provided` and `connect` join the public surface. The usage errors of step 3 that Python already names (`TypeError`, `ValueError`, `AttributeError` for a wrong argument or an unknown action) stay as they are, and its `RuntimeError` for a missing or closed scope narrows to `UsageError`, which code that catches `RuntimeError` still catches.
- #11's plan differs from this record in two places, and follows it if the record is accepted: PR 1's "checked when the app registry is ready" becomes "checked when the class is defined", and the design gate's "system checks for startup failures" covers the host's own rules, with Resolve and Validate reported as 0022 proposes.
- Specification changes for PRs 1 to 3, in neutral terms and with new `APP` and `TRN` requirement IDs: an app's name follows `segment`; names are unique among the apps of all hosts that exchange calls, which is a duty of deployment and has no test; a resource is named by its kind in 0.1; each provided contract carries its list of calling apps that may assert a principal; a declaration outside the admitted form stops the host before Serve, and a host reports the refusals of one phase together; within a host, a contract version has one provider and one declaration; a call to a contract the calling app does not require is a mistake in the calling code, in every mode, is not sent, and is checked at the place step 1a names; an implementation is recommended to offer the pre-deployment check of Decision 9.
- Tests for PR 1 draw declarations from the grammar above with Hypothesis, and mutate one element at a time to produce each row of the first table. They cover a base class beside a declaring class in one `apps` module, a mixin with a public name, and the list of admitted Django options on each supported version. PR 2 covers a registry without neolith's app, a `connect()` at import for a known and an unknown identity, a call before set-up has finished, several refusals reported in one error, and `override_settings` in and out.

## Open questions

1. **A typed `connect()`.** `connect("<contract>.v<N>")` returns a connector whose actions a type checker cannot see. With declaration classes already in `requires`, a form that takes the class could be typed. 0003 fixes the string form, so this needs its own record.
2. **Remote providers.** In step 4 a requirement resolves only to an app in the host. Step 5 (#12) adds configured remote providers, and decides whether the "one provider per identity" rule of Decision 8 becomes "one in-host provider" under 0016.
3. **A length bound on an app's name.** The grammar sets none, and 0009 as amended, which uses the name as a database alias, adds none. Spec 03 caps the token at 4096 bytes, which bounds it in practice. 63 has precedent on both sides the name may be mapped to: a DNS label, and a PostgreSQL identifier. A bound would be a change to this record's grammar.
4. **What this record leaves to 0022.** That record makes `neolith` an installed app, runs set-up from its `ready()`, decides which later set-up is admitted, names the entries `neolith run` takes, installs batteries, imports the root URLconf at Boot, and refuses a scope for an app the host does not hold. If it is not accepted in its present form, the sentences here that lean on it have to be restated.
5. **The nested-class form.** Decision 2 keeps the fields on the config class and names what that costs. If adopters turn out to need third-party config mixins, the `class Meta` form weighed there is the alternative to return to.
6. **A wildcard or a group in an allow-list.** Left out of 0.1. A project with many callers of one contract may make the case for it.

## References

- Django: [applications](https://docs.djangoproject.com/en/5.2/ref/applications/) (`AppConfig.label`, `default`, `ready`, and the note that `ready` can run more than once); [1.11 release notes](https://docs.djangoproject.com/en/5.2/releases/1.11/) (`get_user_model()` at import time); [`reverse_lazy`](https://docs.djangoproject.com/en/5.2/ref/urlresolvers/#reverse-lazy); [system checks](https://docs.djangoproject.com/en/5.2/topics/checks/); [`AUTHENTICATION_BACKENDS`](https://docs.djangoproject.com/en/5.2/ref/settings/#authentication-backends).
- Django REST framework: [settings](https://www.django-rest-framework.org/api-guide/settings/), classes named by dotted path.
- Java: [module declarations](https://docs.oracle.com/javase/specs/jls/se21/html/jls-7.html#jls-7.7), `requires`, `provides ... with` and `uses`; [`ServiceLoader`](https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/util/ServiceLoader.html).
- Spring Modulith: [verification](https://docs.spring.io/spring-modulith/reference/verification.html), every violation in one failure; [fundamentals](https://docs.spring.io/spring-modulith/reference/fundamentals.html), module names local to one program.
- Encore: [API calls](https://encore.dev/docs/go/primitives/api-calls) checked at compile time; [databases](https://encore.dev/docs/go/primitives/databases) named in the service's code.
- Dapr: [access lists](https://docs.dapr.io/operations/configuration/invoke-allowlist/), an `app-id` from deployment configuration; [components](https://docs.dapr.io/concepts/components-concept/).
- django-oscar: [`OscarConfigMixin`](https://github.com/django-oscar/django-oscar/blob/master/src/oscar/core/application.py), a config mixin with public attributes.
- Kubernetes: [object names](https://kubernetes.io/docs/concepts/overview/working-with-objects/names/), the DNS label form.
- [gRPC Python](https://grpc.github.io/grpc/python/grpc.html): a channel is created without connecting.
- [Python entry points](https://packaging.python.org/en/latest/specifications/entry-points/).
