"""The check's rules on models: relations across apps (``neolith.E106``) and
records stored where another app's are (``neolith.E107``) (spec 05, APP-67;
ADR 0030 and ADR 0004).

Each test writes a project under ``tmp_path`` and runs the check in a child
process (``tests/checking.py``). A host that holds undeclared apps, contrib
apps among them, has a real ``default``, so those tests run the settings
module entry, ``manage.py checkapps``.
"""

import re
from pathlib import Path

import pytest

from tests.checking import Project, Ran

pytestmark = pytest.mark.spec("APP-67")

_RELATION_HINT = (
    "keep the other record's identifier in a plain field, and reach that app "
    "through a contract it provides"
)
_PROXY_HINT = (
    "define a model of the app's own, or reach those records through a contract "
    "that app provides"
)
_TABLE_HINT = "give the model a table name of its own"
_SUMMARY = re.compile(
    r"neolith check: (\d+) errors, (\d+) errors in report-only apps, (\d+) warnings"
)

_SHOP = """\
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class Item(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.IntegerField()
    target = GenericForeignKey("content_type", "object_id")


class Link(models.Model):
    left = models.ForeignKey("desk.ViaShop", on_delete=models.CASCADE)
    right = models.ForeignKey("desk.Own", on_delete=models.CASCADE, related_name="+")
"""

_LEGACY_APP = """\
from django.apps import AppConfig


class LegacyConfig(AppConfig):
    name = "legacy"
    default_auto_field = "django.db.models.AutoField"
"""

_LEGACY = """\
from django.db import models


class Thing(models.Model):
    name = models.CharField(max_length=10)
"""

# Every relation kind to each kind of target, and the same kinds inside
# the app, which are admitted.
_DESK = """\
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.contenttypes.fields import GenericForeignKey, GenericRelation
from django.db import models
from legacy.models import Thing
from shop.models import Item


class Own(models.Model):
    content_type = models.ForeignKey(
        "contenttypes.ContentType", on_delete=models.CASCADE, related_name="+"
    )
    object_id = models.IntegerField()
    target = GenericForeignKey("content_type", "object_id")


class Inside(models.Model):
    fk = models.ForeignKey(Own, on_delete=models.CASCADE, related_name="+")
    one = models.OneToOneField(Own, on_delete=models.CASCADE, related_name="+")
    many = models.ManyToManyField(Own, related_name="+")
    generic = GenericRelation(Own, related_query_name="+")


class Child(Own):
    pass


class OwnProxy(Own):
    class Meta:
        proxy = True


class Abstract(models.Model):
    inherited = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="+")

    class Meta:
        abstract = True


class FromAbstract(Abstract):
    pass


class ToShop(models.Model):
    fk = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="+")
    one = models.OneToOneField(Item, on_delete=models.CASCADE, related_name="+")
    many = models.ManyToManyField(Item, related_name="+")
    generic = GenericRelation(Item, related_query_name="+")


class ShopChild(Item):
    pass


class ToLegacy(models.Model):
    fk = models.ForeignKey(Thing, on_delete=models.CASCADE, related_name="+")
    one = models.OneToOneField(Thing, on_delete=models.CASCADE, related_name="+")
    many = models.ManyToManyField("legacy.Thing", related_name="+")


class LegacyChild(Thing):
    pass


class ToUser(models.Model):
    fk = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    one = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+"
    )
    many = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="+")


class UserChild(User):
    pass


class ViaShop(models.Model):
    many = models.ManyToManyField(
        Own, through="shop.Link", through_fields=("left", "right"), related_name="+"
    )


class UserProxy(User):
    class Meta:
        proxy = True


class Copy(models.Model):
    class Meta:
        db_table = "LEGACY_thing"


class Apart(models.Model):
    class Meta:
        db_table = "legacy_things"
"""


def _at(source: str, name: str) -> int:
    """The line of ``class <name>(`` in ``source``."""
    lines = source.splitlines()
    return next(
        n for n, text in enumerate(lines, 1) if text.startswith(f"class {name}(")
    )


def _summary(ran: Ran) -> tuple[int, int, int]:
    found = _SUMMARY.fullmatch(ran.summary)
    assert found is not None, ran.stdout
    errors, marked, warnings = found.groups()
    return int(errors), int(marked), int(warnings)


@pytest.fixture
def mixed(tmp_path: Path) -> Project:
    """Two hosted apps, ``shop`` and ``desk``, beside an undeclared
    first-party app ``legacy`` and the contrib apps ``auth`` and
    ``contenttypes``, on a real ``default``.
    """
    project = Project(tmp_path)
    project.app("shop", models=_SHOP)
    project.app("desk", models=_DESK)
    project.write("legacy/__init__.py")
    project.write("legacy/apps.py", _LEGACY_APP)
    project.write("legacy/models.py", _LEGACY)
    project.settings(
        ["shop", "desk"],
        neolith={
            "DATABASES": project.databases("default", "shop", "desk"),
            "UNDECLARED_APPS": [
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "legacy",
            ],
        },
    )
    return project


def test_each_relation_to_another_app_is_reported_and_none_inside_one(
    mixed: Project,
) -> None:
    ran = mixed.checkapps()
    desk = mixed.root.joinpath("desk/models.py").read_text(encoding="utf-8")
    shop = mixed.root.joinpath("shop/models.py").read_text(encoding="utf-8")

    def relation(model: str, field: str, kind: str, target: str, note: str = "") -> str:
        source, app = (
            (desk, "desk") if model != "Item" and model != "Link" else (shop, "shop")
        )
        other = target.partition(".")[0]
        return (
            f"{app}/models.py:{_at(source, model)}: neolith.E106 app {app}: model "
            f"{app}.{model}: field {field} is {kind} {target}, a model of app "
            f"{other}{note}; {_RELATION_HINT}"
        )

    relations = [
        relation(
            "Item",
            "content_type",
            "a foreign key to",
            "contenttypes.ContentType",
            ", which the GenericForeignKey target rests on",
        ),
        relation("Link", "left", "a foreign key to", "desk.ViaShop"),
        relation("Link", "right", "a foreign key to", "desk.Own"),
        relation(
            "Own",
            "content_type",
            "a foreign key to",
            "contenttypes.ContentType",
            ", which the GenericForeignKey target rests on",
        ),
        relation("FromAbstract", "inherited", "a foreign key to", "shop.Item"),
        relation(
            "ShopChild", "item_ptr", "a multi-table inheritance link to", "shop.Item"
        ),
        relation(
            "LegacyChild",
            "thing_ptr",
            "a multi-table inheritance link to",
            "legacy.Thing",
        ),
        relation(
            "UserChild", "user_ptr", "a multi-table inheritance link to", "auth.User"
        ),
        relation("ViaShop", "many", "a many-to-many relation through", "shop.Link"),
    ]
    for model, target in [
        ("ToShop", "shop.Item"),
        ("ToLegacy", "legacy.Thing"),
        ("ToUser", "auth.User"),
    ]:
        relations += [
            relation(model, "fk", "a foreign key to", target),
            relation(model, "one", "a one-to-one relation to", target),
            relation(model, "many", "a many-to-many relation to", target),
        ]
    relations.append(
        relation("ToShop", "generic", "a generic relation to", "shop.Item")
    )
    stored = [
        f"desk/models.py:{_at(desk, 'UserProxy')}: neolith.E107 app desk: model "
        "desk.UserProxy is a proxy of auth.User, a model of app auth, so its records "
        f"are that app's; {_PROXY_HINT}",
        f"desk/models.py:{_at(desk, 'Copy')}: neolith.E107 app desk: model desk.Copy "
        "has the table 'LEGACY_thing', the table of legacy.Thing, a model of app "
        f"legacy, compared without regard to case; {_TABLE_HINT}",
    ]
    assert sorted(ran.findings) == sorted(relations + stored), ran.stdout + ran.stderr
    assert ran.findings == sorted(
        ran.findings, key=lambda line: (line.split(":")[0], int(line.split(":")[1]))
    )
    assert _summary(ran) == (len(relations) + len(stored), 0, 0)
    assert ran.status == 1
    assert ran.errors == []


def test_records_stored_where_another_hosted_app_s_are_are_reported(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path)
    project.app(
        "shop",
        models="""\
        from django.db import models


        class Item(models.Model):
            pass
        """,
    )
    desk = """\
    from django.db import models
    from shop.models import Item


    class Own(models.Model):
        pass


    class ShopProxy(Item):
        class Meta:
            proxy = True


    class OwnProxy(Own):
        class Meta:
            proxy = True


    class ProxyOfProxy(ShopProxy):
        class Meta:
            proxy = True


    class Upper(models.Model):
        class Meta:
            db_table = "SHOP_ITEM"


    class Unrelated(models.Model):
        class Meta:
            db_table = "shop_items"


    class Unmanaged(models.Model):
        class Meta:
            db_table = "shop_item"
            managed = False


    class Tagger(models.Model):
        tags = models.ManyToManyField(Own, db_table="Shop_Item", related_name="+")
    """
    project.app("desk", models=desk)
    project.deployer("shop", "desk")
    ran = project.check("shop", "desk")
    desk = project.root.joinpath("desk/models.py").read_text(encoding="utf-8")

    def proxy(model: str) -> str:
        return (
            f"desk/models.py:{_at(desk, model)}: neolith.E107 app desk: model "
            f"desk.{model} is a proxy of shop.Item, a model of app shop, so its "
            f"records are that app's; {_PROXY_HINT}"
        )

    def table(model: str, line: int, name: str, other: str) -> str:
        app = model.partition(".")[0]
        return (
            f"{app}/models.py:{line}: neolith.E107 app {app}: model {model} has the "
            f"table {name!r}, the table of {other}, a model of app "
            f"{other.partition('.')[0]}, compared without regard to case; "
            f"{_TABLE_HINT}"
        )

    shop_line = 4
    expected = [
        proxy("ShopProxy"),
        proxy("ProxyOfProxy"),
        table("desk.Upper", _at(desk, "Upper"), "SHOP_ITEM", "shop.Item"),
        table("desk.Unmanaged", _at(desk, "Unmanaged"), "shop_item", "shop.Item"),
        table("desk.Tagger_tags", _at(desk, "Tagger"), "Shop_Item", "shop.Item"),
        table("shop.Item", shop_line, "shop_item", "desk.Upper"),
        table("shop.Item", shop_line, "shop_item", "desk.Unmanaged"),
        table("shop.Item", shop_line, "shop_item", "desk.Tagger_tags"),
    ]
    assert sorted(ran.findings) == sorted(expected), ran.stdout + ran.stderr
    assert _summary(ran) == (len(expected), 0, 0)
    assert ran.status == 1
